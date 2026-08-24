"""綠界打進來的端點。這裡不驗 API key —— 綠界不會帶 —— 改驗 CheckMacValue。

三個回呼共用的規則：
1. 驗簽不過就 400 且**不落地**。
2. 驗簽通過就**一律回 `1|OK`**，包含重複的那些。不回 `1|OK` 綠界會
   隔 5~15 分鐘重送、當天四次。
3. 綠界**沒有全域 event id**，去重鍵是自己造的（見各處的 dedupe_key）。

以及這個介接最大的陷阱：
**定期定額的首期回呼跟一次性付款逐欄位相同。** PeriodType / Frequency /
ExecTimes / TotalSuccessTimes 只出現在第二期起的 PeriodReturnURL。
所以 /ecpay/return 不能看 payload 判斷型別，只能拿 MerchantTradeNo
查自己的表 —— 而那筆資料在建單當下就寫好了。
"""
import html
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from app.config import get_settings
from app.ecpay import checkmac
from app.store import events as events_store
from app.store import orders as orders_store
from app.store import subscriptions as subs_store

log = logging.getLogger("callback")

router = APIRouter(prefix="/ecpay", tags=["ecpay-callbacks"])

# 綠界要求特店收到通知後回這個字串。回別的（包括 200 但內容不同）
# 都會被當成沒收到而重送。
ACK = "1|OK"
SUCCESS = "1"


async def _form(request: Request) -> dict:
    form = await request.form()
    return {k: str(v) for k, v in form.items()}


def _verified(params: dict) -> bool:
    s = get_settings()
    return checkmac.verify(params, s.hash_key, s.hash_iv)


@router.get("/checkout/{token}")
def checkout(token: str):
    """付款導轉頁：吐一頁 auto-submit form POST 到綠界。

    這一頁**不驗 API key**（使用者的瀏覽器會打它），所以網址用的是獨立的
    高熵 token，不是 order id —— 否則知道 id 就看得到別人的付款頁。
    付款完成後 token 會被清掉，這一頁就失效。
    """
    s = get_settings()
    row = orders_store.get_by_checkout_token(token) \
        or subs_store.get_by_checkout_token(token)
    if not row:
        # 已付款、已取消、或根本不存在 —— 對外不區分
        return HTMLResponse("<h1>付款連結已失效</h1>", status_code=404)

    fields = row["checkout_fields"]
    if isinstance(fields, str):
        fields = json.loads(fields)

    inputs = "\n".join(
        f'<input type="hidden" name="{html.escape(k)}" '
        f'value="{html.escape(str(v))}">'
        for k, v in fields.items())
    # 不用 JS 也能送出（<noscript> 的按鈕），避免瀏覽器擋 JS 時整頁卡死
    return HTMLResponse(f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<title>前往綠界付款</title></head>
<body onload="document.forms[0].submit()">
<form action="{html.escape(s.aio_checkout_url)}" method="post">
{inputs}
<noscript><p>請按下按鈕前往綠界付款頁</p>
<button type="submit">前往付款</button></noscript>
</form>
<p>正在前往綠界付款頁…</p>
</body></html>""")


@router.post("/return")
async def payment_return(request: Request):
    """付款結果通知（含定期定額**首期**）。

    首期與一次性付款的欄位完全相同 —— 靠 MerchantTradeNo 查自己的表分辨。
    """
    params = await _form(request)
    if not _verified(params):
        log.warning("return 驗簽失敗 MerchantTradeNo=%s",
                    params.get("MerchantTradeNo"))
        return PlainTextResponse("0|CheckMacValue verify failed", status_code=400)

    trade_no = params.get("MerchantTradeNo", "")
    rtn_code = str(params.get("RtnCode", ""))
    raw = json.dumps(params, ensure_ascii=False)

    sub = subs_store.get_by_trade_no(trade_no)
    order = None if sub else orders_store.get_by_trade_no(trade_no)
    caller_id = (sub or order or {}).get("caller_id")
    kind = "subscription" if sub else ("order" if order else None)
    subject_id = str((sub or order)["id"]) if (sub or order) else None

    new_id = events_store.record(
        f"return:{trade_no}:{rtn_code}", "payment.return", caller_id, kind,
        subject_id, raw)

    if new_id is None:
        # 綠界重送 —— 冪等。**還是要回 1|OK**，否則它會繼續重送。
        return PlainTextResponse(ACK)

    if sub:
        if rtn_code == SUCCESS:
            # 綠界的規則：首期授權失敗整張單不進排程。
            # 所以首期成功 == 訂閱真的開始了。
            subs_store.mark_active(sub["id"], params.get("TradeNo"))
            subs_store.record_charge(
                sub["id"], gwsr=str(params.get("gwsr") or f"first:{trade_no}"),
                amount=int(params.get("TradeAmt") or sub["period_amount"]),
                rtn_code=rtn_code, auth_code=params.get("auth_code"),
                process_date=params.get("PaymentDate"))
            subs_store.set_totals(sub["id"], 1,
                                  int(params.get("TradeAmt") or 0))
        else:
            subs_store.set_status(sub["id"], "failed")
    elif order:
        if rtn_code == SUCCESS:
            orders_store.mark_paid(order["id"], params.get("TradeNo"),
                                   params.get("PaymentType"))
        else:
            orders_store.set_status(order["id"], "failed",
                                    params.get("TradeNo"))
    else:
        # 同一個商店代號底下可能有別的系統在用（dev 的測試商店更是全球共用）。
        # 落地保留原文，但 caller_id 為 NULL，對每個 caller 都不可見。
        log.info("return 對應不到本地紀錄 MerchantTradeNo=%s", trade_no)

    return PlainTextResponse(ACK)


@router.post("/period-return")
async def period_return(request: Request):
    """定期定額**第二期起**的扣款結果。

    去重鍵用 gwsr（綠界每次授權的交易號）—— 這是唯一每期都不同的識別碼。
    """
    params = await _form(request)
    if not _verified(params):
        return PlainTextResponse("0|CheckMacValue verify failed", status_code=400)

    trade_no = params.get("MerchantTradeNo", "")
    gwsr = str(params.get("gwsr") or "")
    rtn_code = str(params.get("RtnCode", ""))
    raw = json.dumps(params, ensure_ascii=False)

    sub = subs_store.get_by_trade_no(trade_no)
    caller_id = sub["caller_id"] if sub else None
    subject_id = str(sub["id"]) if sub else None

    new_id = events_store.record(
        f"period:{gwsr or trade_no}:{rtn_code}", "subscription.charge",
        caller_id, "subscription" if sub else None, subject_id, raw)
    if new_id is None:
        return PlainTextResponse(ACK)

    if sub:
        if gwsr:
            subs_store.record_charge(
                sub["id"], gwsr=gwsr,
                amount=int(params.get("amount") or 0), rtn_code=rtn_code,
                auth_code=params.get("auth_code"),
                process_date=params.get("process_date"))
        if rtn_code == SUCCESS:
            subs_store.set_totals(
                sub["id"], int(params.get("TotalSuccessTimes") or 0),
                int(params.get("TotalSuccessAmount")
                    or sub["total_success_amount"]))
        # 扣款失敗**不改訂閱狀態** —— 綠界會繼續嘗試，
        # 連續六期失敗它才自動終止。單次失敗不等於訂閱結束。

    return PlainTextResponse(ACK)


@router.post("/payment-info")
async def payment_info(request: Request):
    """ATM／超商的取號結果。

    這時候**還沒有付款** —— 只有虛擬帳號或繳費代碼與繳費期限。
    訂單進入 awaiting_payment，使用者可能幾天後才去繳。
    """
    params = await _form(request)
    if not _verified(params):
        return PlainTextResponse("0|CheckMacValue verify failed", status_code=400)

    trade_no = params.get("MerchantTradeNo", "")
    raw = json.dumps(params, ensure_ascii=False)
    order = orders_store.get_by_trade_no(trade_no)
    caller_id = order["caller_id"] if order else None
    subject_id = str(order["id"]) if order else None

    new_id = events_store.record(
        f"info:{trade_no}", "payment.info", caller_id,
        "order" if order else None, subject_id, raw)
    if new_id is None:
        return PlainTextResponse(ACK)

    if order:
        orders_store.save_payment_info(order["id"], params, raw)
        # RtnCode 2 = 取號成功（跟付款成功的 1 不同，這裡刻意不共用常數）
        if str(params.get("RtnCode", "")) in ("2", "10100073"):
            orders_store.set_status(order["id"], "awaiting_payment",
                                    params.get("TradeNo"))

    return PlainTextResponse(ACK)


@router.get("/order-result")
@router.post("/order-result")
async def order_result(request: Request):
    """使用者的瀏覽器導回這裡，我們再 302 到 caller 自己的頁面。

    刻意繞這一手：caller 的網址不必寫進綠界，而且我們有機會先看一眼結果。
    **不在這裡改訂單狀態** —— 瀏覽器導回是使用者可以偽造、也可能根本不發生的
    （關掉分頁就沒了）。狀態的真相來源只有幕後的 ReturnURL。
    """
    params = await _form(request) if request.method == "POST" else dict(request.query_params)
    trade_no = params.get("MerchantTradeNo", "")
    verified = _verified(params) if params.get("CheckMacValue") else False

    row = (orders_store.get_by_trade_no(trade_no)
           or subs_store.get_by_trade_no(trade_no)) if trade_no else None
    target = (row or {}).get("return_url")
    if not target:
        status = "success" if str(params.get("RtnCode", "")) == SUCCESS else "failed"
        return HTMLResponse(
            f"<!doctype html><meta charset='utf-8'>"
            f"<h1>付款{'完成' if status == 'success' else '未完成'}</h1>"
            f"<p>訂單編號：{html.escape(trade_no)}</p>")

    sep = "&" if "?" in target else "?"
    rtn = html.escape(str(params.get("RtnCode", "")))
    return RedirectResponse(
        f"{target}{sep}merchant_trade_no={html.escape(trade_no)}"
        f"&rtn_code={rtn}&verified={'1' if verified else '0'}",
        status_code=303)
