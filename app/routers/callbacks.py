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

## 併發模型：外層 async 只取 body，其餘全部丟 threadpool

pg8000 是**同步** driver。同步呼叫寫在 `async def` 裡會卡住整個事件迴圈 ——
那個實例上所有請求跟著排隊，包括 caller 正在同步查詢的 `GET /v1/orders/{id}`。
一筆回呼六次 DB round trip 就是幾十毫秒的全實例停擺，而行銷活動集中付款
正好是最不能停擺的時候。

所以：route function 只做 `await request.body()`，其餘交給
`run_in_threadpool`。**不要在這個模組裡引入 async DB 或 async httpx** ——
混用正是這個 bug 的來源。

## 兩個入口，一份處理

`/ecpay/return`（幕後）與 `/ecpay/order-result`（使用者的瀏覽器）
收到的參數集相同、都由綠界簽章。所以驗簽通過的導回**一樣可信**，
可以拿它更早把狀態弄對 —— 誰先到誰生效，靠同一個 dedupe_key。

⚠️ 但**推送只由 `/ecpay/return` 負責**：導回路徑要快，而幕後回呼一定會到。
"""
import html
import json
import logging
from urllib.parse import parse_qsl

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from app import db, ids
from app.config import get_settings
from app.ecpay import checkmac
from app.ecpay import client as ec_client
from app.ecpay import orders as ec_orders
from app.store import attempts as attempts_store
from app.store import events as events_store
from app.store import orders as orders_store
from app.store import subscriptions as subs_store

log = logging.getLogger("callback")

router = APIRouter(prefix="/ecpay", tags=["ecpay-callbacks"])

# 綠界要求特店收到通知後回這個字串。回別的（包括 200 但內容不同）
# 都會被當成沒收到而重送。
ACK = "1|OK"
SUCCESS = "1"


def _resolve(trade_no: str, tx=None):
    """單號 → (訂閱, 訂單)，其中一個是 None。

    先查 trade_attempts（涵蓋所有換過的單號），查不到再退回直接比對 ——
    後者是為了讓 migration 之前建立的資料仍然解析得到。
    """
    if not trade_no:
        return None, None
    hit = attempts_store.resolve(trade_no, tx=tx)
    if hit:
        sid = str(hit["subject_id"])
        if hit["subject_kind"] == "subscription":
            return subs_store.get_by_id(sid, tx=tx), None
        return None, orders_store.get_by_id(sid, tx=tx)
    sub = subs_store.get_by_trade_no(trade_no, tx=tx)
    return sub, (None if sub else orders_store.get_by_trade_no(trade_no, tx=tx))


def _parse(raw: bytes) -> dict:
    """自己解析 body，**不要用 `request.form()`**。

    Starlette 的 urlencoded 解析器用 latin-1 解碼 body。綠界成功通知的
    `RtnMsg` 是中文（「付款成功」），latin-1 解出來是亂碼，
    拿亂碼去算 CheckMacValue 必定對不上 —— 症狀是綠界說「沒收到 1|OK」
    而我們這邊看起來只是驗簽失敗，兩邊都指不出真正的原因。

    這個 bug 只有在收到**真實的**綠界回呼時才會出現：自己造的測試資料
    如果全是 ASCII，latin-1 與 UTF-8 的結果一模一樣，測試會全過。
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # 綠界舊介面用 Big5。AIO V5 是 UTF-8，但退路成本很低。
        text = raw.decode("big5", errors="replace")
    return dict(parse_qsl(text, keep_blank_values=True))


def _verified(params: dict) -> bool:
    """驗簽，失敗時留下足以查案的線索。

    驗簽失敗是最難查的一類問題：綠界只會告訴你「沒收到 1|OK」，
    看不到是哪個欄位讓檢查碼對不上。所以失敗時一定要記下
    **欄位名清單**與雙方的檢查碼；欄位「值」只在 debug 等級記，
    因為那裡面可能有姓名、卡號末四碼之類的資料。
    """
    s = get_settings()
    if checkmac.verify(params, s.hash_key, s.hash_iv):
        return True
    rest = {k: v for k, v in params.items() if k != "CheckMacValue"}
    log.warning(
        "驗簽失敗 trade=%s 欄位=%s 綠界=%s 我們算的=%s",
        params.get("MerchantTradeNo"), sorted(rest),
        params.get("CheckMacValue"),
        checkmac.generate(rest, s.hash_key, s.hash_iv))
    log.debug("驗簽失敗的原始欄位：%r", rest)
    return False


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
        # 付款完成時 token 會被清掉（不清的話，重開這一頁會在綠界建立
        # **另一筆**交易，等於給了重複付款的機會）。但直接回 404 會讓人
        # 以為連結壞了 —— 分辨得出來的情況就說清楚。
        done = (orders_store.get_by_used_token(token)
                or subs_store.get_by_used_token(token))
        if done:
            return HTMLResponse(
                "<!doctype html><meta charset='utf-8'>"
                "<title>已完成付款</title>"
                "<h1>這筆已經完成付款</h1>"
                "<p>不需要再付一次。</p>", status_code=200)
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'>"
            "<title>連結無效</title><h1>付款連結無效</h1>"
            "<p>請向原商店重新取得付款連結。</p>", status_code=404)

    kind = "order" if "choose_payment" in row else "subscription"
    store = orders_store if kind == "order" else subs_store

    fields = row["checkout_fields"]
    if isinstance(fields, str):
        fields = json.loads(fields)

    # 綠界的 MerchantTradeNo **送出過就不能再用** —— 回訪這一頁會拿到
    # 10300028「訂單編號重覆」。而「跳開再回來付」是最常見的行為，
    # 所以每次進來都換一個新單號重簽。舊單號留在 trade_attempts 裡，
    # 它的回呼照樣找得回這筆訂單。
    trade_no = ids.merchant_trade_no("O" if kind == "order" else "S")
    fields = dict(fields)
    fields.pop("CheckMacValue", None)
    fields["MerchantTradeNo"] = trade_no
    fields["MerchantTradeDate"] = ec_orders.now_taipei()
    fields = ec_client.signed(fields)
    attempts_store.record(trade_no, kind, row["id"])
    store.rotate_trade_no(row["id"], trade_no, json.dumps(fields))

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


def return_dedupe_key(trade_no: str, rtn_code: str) -> str:
    """`/ecpay/return` 與 `/ecpay/order-result` **必須**用同一個鍵 ——
    這就是「誰先到誰生效」的全部實作。"""
    return f"return:{trade_no}:{rtn_code}"


def _apply_return(params: dict, trade_no: str, rtn_code: str):
    """落地事件 + 更新本地狀態，**一個交易**。回 (new_id, caller_id)。

    ⚠️ 為什麼一定要同一個交易：`db.query()` 每呼叫一次就 commit 一次，
    所以事件落地與狀態更新原本是兩次獨立的 commit。中間掛掉的話，綠界重送
    會被 dedupe_key 擋掉、走 `new_id is None` 的路徑，**狀態更新永遠不會執行**
    —— 去重鍵一邊做著它該做的事，一邊堵死了唯一的復原路徑。
    突發流量（實例回收、逾時）正是它發作的時候。

    new_id 是 None 代表「已經有人處理過了」：綠界重送，或者另一個入口先到。
    兩種情況下本地狀態都已經正確。
    """
    raw_json = json.dumps(params, ensure_ascii=False)
    with db.transaction() as tx:
        sub, order = _resolve(trade_no, tx=tx)
        caller_id = (sub or order or {}).get("caller_id")
        kind = "subscription" if sub else ("order" if order else None)
        subject_id = str((sub or order)["id"]) if (sub or order) else None

        new_id = events_store.record(
            return_dedupe_key(trade_no, rtn_code), "payment.return", caller_id,
            kind, subject_id, raw_json, tx=tx)
        if new_id is None:
            return None, caller_id

        if sub:
            if rtn_code == SUCCESS:
                # 綠界的規則：首期授權失敗整張單不進排程。
                # 所以首期成功 == 訂閱真的開始了。
                subs_store.mark_active(sub["id"], params.get("TradeNo"), tx=tx)
                subs_store.record_charge(
                    sub["id"], gwsr=str(params.get("gwsr") or f"first:{trade_no}"),
                    amount=int(params.get("TradeAmt") or sub["period_amount"]),
                    rtn_code=rtn_code, auth_code=params.get("auth_code"),
                    process_date=params.get("PaymentDate"), tx=tx)
                subs_store.set_totals(sub["id"], 1,
                                      int(params.get("TradeAmt") or 0), tx=tx)
            else:
                subs_store.set_status(sub["id"], "failed", tx=tx)
        elif order:
            if rtn_code == SUCCESS:
                if order["status"] == "paid":
                    # 同一筆訂單的另一次嘗試也付款成功（例如使用者開了兩個分頁）。
                    # 不重複標記，但事件已落地 —— 這是需要人看一眼的情況。
                    log.warning("訂單 %s 已是 paid，又收到 %s 的成功回呼",
                                order["id"], trade_no)
                else:
                    orders_store.mark_paid(
                        order["id"], params.get("TradeNo"),
                        params.get("PaymentType"), gwsr=params.get("gwsr"),
                        auth_code=params.get("auth_code"), tx=tx)
            else:
                orders_store.set_status(order["id"], "failed",
                                        params.get("TradeNo"), tx=tx)
        else:
            # 同一個商店代號底下可能有別的系統在用（dev 的測試商店更是全球共用）。
            # 落地保留原文，但 caller_id 為 NULL，對每個 caller 都不可見。
            log.info("return 對應不到本地紀錄 MerchantTradeNo=%s", trade_no)

        return new_id, caller_id


@router.post("/return")
async def payment_return(request: Request):
    """付款結果通知（含定期定額**首期**）。

    首期與一次性付款的欄位完全相同 —— 靠 MerchantTradeNo 查自己的表分辨。
    """
    raw = await request.body()
    return await run_in_threadpool(_payment_return, raw)


def _payment_return(raw: bytes):
    params = _parse(raw)
    if not _verified(params):
        log.warning("return 驗簽失敗 MerchantTradeNo=%s",
                    params.get("MerchantTradeNo"))
        return PlainTextResponse("0|CheckMacValue verify failed", status_code=400)

    trade_no = params.get("MerchantTradeNo", "")
    rtn_code = str(params.get("RtnCode", ""))
    _apply_return(params, trade_no, rtn_code)
    return PlainTextResponse(ACK)


@router.post("/period-return")
async def period_return(request: Request):
    """定期定額**第二期起**的扣款結果。

    去重鍵用 gwsr（綠界每次授權的交易號）—— 這是唯一每期都不同的識別碼。
    """
    raw = await request.body()
    return await run_in_threadpool(_period_return, raw)


def _period_return(raw: bytes):
    params = _parse(raw)
    if not _verified(params):
        return PlainTextResponse("0|CheckMacValue verify failed", status_code=400)

    trade_no = params.get("MerchantTradeNo", "")
    gwsr = str(params.get("gwsr") or "")
    rtn_code = str(params.get("RtnCode", ""))
    raw_json = json.dumps(params, ensure_ascii=False)

    with db.transaction() as tx:
        sub, _ = _resolve(trade_no, tx=tx)
        caller_id = sub["caller_id"] if sub else None
        subject_id = str(sub["id"]) if sub else None

        new_id = events_store.record(
            f"period:{gwsr or trade_no}:{rtn_code}", "subscription.charge",
            caller_id, "subscription" if sub else None, subject_id, raw_json,
            tx=tx)

        if new_id is not None and sub:
            if gwsr:
                subs_store.record_charge(
                    sub["id"], gwsr=gwsr,
                    amount=int(params.get("amount") or 0), rtn_code=rtn_code,
                    auth_code=params.get("auth_code"),
                    process_date=params.get("process_date"), tx=tx)
            if rtn_code == SUCCESS:
                subs_store.set_totals(
                    sub["id"], int(params.get("TotalSuccessTimes") or 0),
                    int(params.get("TotalSuccessAmount")
                        or sub["total_success_amount"]), tx=tx)
            # 扣款失敗**不改訂閱狀態** —— 綠界會繼續嘗試，
            # 連續六期失敗它才自動終止。單次失敗不等於訂閱結束。

    return PlainTextResponse(ACK)


@router.post("/payment-info")
async def payment_info(request: Request):
    """ATM／超商的取號結果。

    這時候**還沒有付款** —— 只有虛擬帳號或繳費代碼與繳費期限。
    訂單進入 awaiting_payment，使用者可能幾天後才去繳。
    """
    raw = await request.body()
    return await run_in_threadpool(_payment_info, raw)


def _payment_info(raw: bytes):
    params = _parse(raw)
    if not _verified(params):
        return PlainTextResponse("0|CheckMacValue verify failed", status_code=400)

    trade_no = params.get("MerchantTradeNo", "")
    raw_json = json.dumps(params, ensure_ascii=False)

    with db.transaction() as tx:
        _, order = _resolve(trade_no, tx=tx)
        caller_id = order["caller_id"] if order else None
        subject_id = str(order["id"]) if order else None

        new_id = events_store.record(
            f"info:{trade_no}", "payment.info", caller_id,
            "order" if order else None, subject_id, raw_json, tx=tx)

        if new_id is not None and order:
            orders_store.save_payment_info(order["id"], params, raw_json, tx=tx)
            # RtnCode 2 = 取號成功（跟付款成功的 1 不同，這裡刻意不共用常數）
            if str(params.get("RtnCode", "")) in ("2", "10100073"):
                orders_store.set_status(order["id"], "awaiting_payment",
                                        params.get("TradeNo"), tx=tx)

    return PlainTextResponse(ACK)


@router.get("/order-result")
@router.post("/order-result")
async def order_result(request: Request):
    """使用者的瀏覽器導回這裡，我們再 302 到 caller 自己的頁面。

    刻意繞這一手：caller 的網址不必寫進綠界，而且我們有機會先看一眼結果。
    """
    if request.method == "POST":
        params = _parse(await request.body())
    else:
        params = dict(request.query_params)
    return await run_in_threadpool(_order_result, params)


def _order_result(params: dict):
    """**第二個入口**：驗簽通過就走跟 /ecpay/return 完全相同的冪等處理。

    原本這裡不改狀態，理由是「瀏覽器導回是使用者可以偽造、也可能根本不發生的」。
    前半段在**驗簽通過時不成立** —— `OrderResultURL` 與 `ReturnURL` 收到的
    參數集相同、用同一把 HashKey/HashIV 簽，一樣可信。後半段仍然成立，
    但那只說明「不能只靠它」，不是「不能用它」。

    這麼做把「錢確定」到「我們的狀態正確」的窗口，從幕後回呼的佇列長度
    縮到零，而且負載天然分散 —— 使用者是陸續回來的。

    ⚠️ 只在 `RtnCode=1` 時處理。ATM／超商取號也會導回這裡（RtnCode=2），
    那筆的事件是 `/ecpay/payment-info` 的 `info:` 鍵，不是這裡的 `return:` 鍵
    —— 不擋的話會憑空多出一筆 payment.return 事件。

    ⚠️ **這裡不排推送。** 導回路徑要快，而幕後回呼一定會到。
    """
    trade_no = params.get("MerchantTradeNo", "")
    rtn_code = str(params.get("RtnCode", ""))
    verified = _verified(params) if params.get("CheckMacValue") else False

    if verified and rtn_code == SUCCESS:
        _apply_return(params, trade_no, rtn_code)

    _sub, _ord = _resolve(trade_no)
    row = _sub or _ord
    target = (row or {}).get("return_url")
    if not target:
        status = "success" if rtn_code == SUCCESS else "failed"
        return HTMLResponse(
            f"<!doctype html><meta charset='utf-8'>"
            f"<h1>付款{'完成' if status == 'success' else '未完成'}</h1>"
            f"<p>訂單編號：{html.escape(trade_no)}</p>")

    sep = "&" if "?" in target else "?"
    return RedirectResponse(
        f"{target}{sep}merchant_trade_no={html.escape(trade_no)}"
        f"&rtn_code={html.escape(rtn_code)}&verified={'1' if verified else '0'}",
        status_code=303)
