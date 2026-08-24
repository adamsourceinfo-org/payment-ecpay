import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app import ids, refunds
from app.auth import Caller, require
from app.config import get_settings
from app.ecpay import orders as ec
from app.errors import (ECPayError, FieldError, InvalidField, bad_request,
                        not_found, upstream_error)
from app.models import OrderCreate, RefundCreate
from app.money import validate_amount
from app.store import attempts as attempts_store
from app.store import orders as store
from app.urls import base_url

router = APIRouter(prefix="/v1/orders", tags=["orders"])


def _out(row: dict, *, base: str = None, info: dict = None) -> dict:
    d = {
        "id": str(row["id"]),
        "reference_id": row["reference_id"],
        "merchant_trade_no": row["merchant_trade_no"],
        "ecpay_trade_no": row.get("ecpay_trade_no"),
        "amount": row["amount"],
        "currency": row["currency"],
        "choose_payment": row["choose_payment"],
        "payment_type": row.get("payment_type"),
        "status": row["status"],
        "refunded_amount": row["refunded_amount"],
        "paid_at": row.get("paid_at"),
        "created_at": row["created_at"],
    }
    if base and row.get("checkout_token"):
        d["checkout_url"] = f"{base}/ecpay/checkout/{row['checkout_token']}"
    if info:
        # ATM／超商的取號結果。信用卡的單不會有。
        d["payment_info"] = info
    return d


@router.post("", status_code=201)
def create_order(body: OrderCreate, request: Request,
                 caller: Caller = Depends(require("orders:write"))):
    s = get_settings()
    try:
        amount = validate_amount(body.amount)
    except FieldError as e:
        raise bad_request(e)          # 擋在進門，不浪費一次綠界往返

    payment = body.choose_payment.strip()
    if payment not in s.allowed_payments:
        # 每個環境實際開通了什麼不同，程式不寫死 —— 但也不該把沒開通的
        # 送去綠界等它回一個看不懂的錯誤。
        raise bad_request(InvalidField(
            f"這個環境不支援 {payment!r}，可用：{sorted(s.allowed_payments)}",
            field="choose_payment"))

    # 各付款方式有金額下限（綠界不公布，依商店而異，由設定提供）。
    # 不擋的話 caller 拿得到 checkout_url，但使用者到綠界會看到
    # 「因交易金額低於下限，本次交易未提供…」的死路，而訂單永遠停在 created。
    floor = s.min_amounts.get(payment)
    if floor and amount < floor:
        raise bad_request(InvalidField(
            f"{payment} 的最低金額是 {floor}，收到 {amount}", field="amount"))

    existing = store.get_by_reference(caller.caller_id, body.reference_id)
    if existing:
        # 冪等：重複的 reference_id 不是錯誤，回原本那筆
        return JSONResponse(
            status_code=200,
            content=jsonable_encoder(_out(existing, base=base_url(request))))

    # 先簽好表單再落地 —— 存的是「實際會 POST 出去的那一份」，
    # 導轉頁不重新產生（重新產生 MerchantTradeDate 會變，就成了另一份簽章）。
    base = base_url(request)
    # 高熵單號：dev 用的測試商店全球共用，流水號會撞到陌生人的訂單
    trade_no = ids.merchant_trade_no("O")
    fields = ec.checkout_fields(
        merchant_trade_no=trade_no, amount=amount,
        item_name=body.item_name, trade_desc=body.trade_desc or body.item_name,
        choose_payment=payment, base_url=base,
        order_result_url=f"{base}/ecpay/order-result",
        custom_fields={"custom1": body.custom1, "custom2": body.custom2,
                       "custom3": body.custom3, "custom4": body.custom4})

    row = store.create(
        caller_id=caller.caller_id, reference_id=body.reference_id,
        merchant_trade_no=trade_no,
        amount=amount, choose_payment=payment, status="created",
        checkout_token=ids.checkout_token(), return_url=body.return_url,
        checkout_fields_json=json.dumps(fields))

    # 第一次嘗試也要進 trade_attempts —— caller 可能自己 render form，
    # 那條路不會經過導轉頁。
    attempts_store.record(trade_no, "order", row["id"])

    out = _out(row, base=base)
    # form 一併回傳，讓要自己 render 的 caller（App 內嵌 WebView）也有路走。
    out["form"] = {"action": s.aio_checkout_url, "method": "POST",
                   "fields": fields}
    return out


@router.get("")
def list_orders(status: Optional[str] = None,
                limit: int = Query(default=50, ge=1, le=200),
                offset: int = Query(default=0, ge=0),
                caller: Caller = Depends(require("orders:read"))):
    rows = store.list_(caller.caller_id, status=status, limit=limit, offset=offset)
    return {"items": [_out(r) for r in rows]}


@router.get("/{order_id}")
def get_order(order_id: str, refresh: bool = False,
              caller: Caller = Depends(require("orders:read"))):
    """預設回本地狀態（回呼是主要的真相來源）。

    `?refresh=true` 才去綠界對帳 —— 每次查詢都打上游會讓對方限流，
    而且回呼已經涵蓋 99% 的狀態變化。refresh 是補償路徑，不是主路徑。
    """
    row = store.get(caller.caller_id, order_id)
    if not row:
        raise not_found("order")

    if refresh:
        try:
            data = ec.query_trade(row["merchant_trade_no"])
        except ECPayError as e:
            raise upstream_error(e)
        mapped = ec.TRADE_STATUS.get(str(data.get("TradeStatus", "")))
        if mapped == "paid" and row["status"] != "paid":
            row = store.mark_paid(row["id"], data.get("TradeNo"),
                                  data.get("PaymentType"))
        elif mapped and mapped != row["status"] and row["status"] != "paid":
            row = store.set_status(row["id"], mapped, data.get("TradeNo"))

    return _out(row, info=store.payment_info(row["id"]))


@router.post("/{order_id}/refund")
def refund(order_id: str, body: RefundCreate,
           caller: Caller = Depends(require("orders:write"))):
    """信用卡退款。

    **只有信用卡可以退。** ATM／超商／WebATM 沒有退款 API，
    綠界要走人工流程 —— 在這裡明說，比讓 caller 收到一個看不懂的上游錯誤好。
    """
    s = get_settings()
    row = store.get(caller.caller_id, order_id)
    if not row:
        raise not_found("order")

    if row["status"] not in ("paid", "partially_refunded"):
        raise bad_request(InvalidField(
            f"訂單狀態是 {row['status']}，只有已付款的訂單能退款", field="status"))

    payment_type = (row.get("payment_type") or row["choose_payment"] or "")
    if not payment_type.lower().startswith("credit"):
        raise bad_request(InvalidField(
            f"付款方式 {payment_type!r} 沒有退款 API，綠界只支援信用卡線上退刷",
            field="payment_type"))

    if not row.get("ecpay_trade_no"):
        raise bad_request(InvalidField(
            "這筆訂單還沒有綠界交易編號，無法退款", field="ecpay_trade_no"))

    remaining = row["amount"] - row["refunded_amount"]
    if body.amount is None:
        amount = remaining
    else:
        try:
            amount = validate_amount(body.amount)
        except FieldError as e:
            raise bad_request(e)
    if amount > remaining:
        raise bad_request(InvalidField(
            f"可退金額只剩 {remaining}，要求 {amount}", field="amount"))

    if not s.do_action_available:
        # 測試環境沒有這支 API。與其送出去等一個難懂的失敗，不如明說。
        raise bad_request(InvalidField(
            "綠界測試環境不提供退款 API（官方：因無法提供實際授權），"
            "退款只能在正式環境執行", field="environment"))

    # 已關帳要送 R（退刷），未關帳要送 N（放棄授權），送錯會失敗。
    # 依綠界的每日自動關帳時間推測先送哪一個，被拒就改送另一個 ——
    # 兩者互斥、失敗沒有部分效果，所以重試是安全的。
    attempts, errors = refunds.actions_for(row.get("paid_at"), row["closed"]), []
    for action in attempts:
        try:
            ec.do_action(merchant_trade_no=row["merchant_trade_no"],
                         trade_no=row["ecpay_trade_no"], action=action,
                         amount=amount)
            break
        except ECPayError as e:
            errors.append((action, e))
    else:
        # 兩個都失敗 —— 把兩次的原文都帶回去，不然沒人查得出是哪一步錯
        raise HTTPException(status_code=502, detail={
            "error": "ecpay_upstream",
            "attempts": [{"action": a, "rtn_code": e.rtn_code,
                          "rtn_msg": e.rtn_msg} for a, e in errors]})

    # 記住這筆到底是不是已關帳，同一筆的後續部分退款就直接命中
    if (action == "R") != bool(row["closed"]):
        store.set_closed(row["id"], action == "R")
    row = store.add_refund(row["id"], amount, fully=(amount == remaining))
    return {**_out(row), "refund_action": action, "refunded_now": amount,
            "attempts": [a for a, _ in errors] + [action]}
