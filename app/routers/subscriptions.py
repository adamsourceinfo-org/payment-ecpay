import json
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app import ids
from app.auth import Caller, require
from app.config import get_settings
from app.ecpay import orders as ec
from app.ecpay import subscriptions as ecsub
from app.errors import (ECPayError, FieldError, InvalidField, bad_request,
                        not_found, upstream_error)
from app.models import SubscriptionCreate
from app.money import validate_amount
from app.store import attempts as attempts_store
from app.store import subscriptions as store
from app.urls import base_url

router = APIRouter(prefix="/v1/subscriptions", tags=["subscriptions"])

# 綠界的 ExecStatus。翻成字給 caller 看，省得每個 caller 自己查文件。
EXEC_STATUS = {"0": "terminated", "1": "running", "2": "completed"}


def _out(row: dict, *, base: str = None, charges=None) -> dict:
    d = {
        "id": str(row["id"]),
        "reference_id": row["reference_id"],
        "merchant_trade_no": row["merchant_trade_no"],
        "ecpay_trade_no": row.get("ecpay_trade_no"),
        "amount": row["period_amount"],
        "currency": "TWD",
        "period_type": row["period_type"],
        "frequency": row["frequency"],
        "exec_times": row["exec_times"],
        "status": row["status"],
        "total_success_times": row["total_success_times"],
        "total_success_amount": row["total_success_amount"],
        "first_charged_at": row.get("first_charged_at"),
        "cancelled_at": row.get("cancelled_at"),
        # 綠界端的執行狀態 —— **「下個月還會不會扣款」的權威答案**。
        # 本地的 status 只是我們自己的紀錄；這個欄位是綠界怎麼說。
        # 只有呼叫過 ?refresh=true 才會有值。
        "ecpay_exec_status": row.get("ecpay_exec_status"),
        "ecpay_exec_status_text": EXEC_STATUS.get(
            str(row.get("ecpay_exec_status")), None),
        "created_at": row["created_at"],
    }
    if base and row.get("checkout_token"):
        d["checkout_url"] = f"{base}/ecpay/checkout/{row['checkout_token']}"
    if charges is not None:
        d["charges"] = charges
    return d


@router.post("", status_code=201)
def create_subscription(body: SubscriptionCreate, request: Request,
                        caller: Caller = Depends(require("subscriptions:write"))):
    """建立定期定額。

    **只有信用卡能做定期定額** —— 綠界的其他付款方式沒有排程扣款。
    所以這裡不收 choose_payment，一律 Credit。
    """
    s = get_settings()
    if "Credit" not in s.allowed_payments:
        raise bad_request(InvalidField(
            "這個環境沒有開通信用卡，無法建立定期定額", field="choose_payment"))
    try:
        amount = validate_amount(body.amount)
        # 期數固定，不由 caller 決定 —— 訂閱的語意就是「到取消為止」
        period = ecsub.validate_period(body.period_type, body.frequency,
                                       ecsub.FIXED_EXEC_TIMES)
    except FieldError as e:
        raise bad_request(e)

    existing = store.get_by_reference(caller.caller_id, body.reference_id)
    if existing:
        return JSONResponse(
            status_code=200,
            content=jsonable_encoder(_out(existing, base=base_url(request))))

    base = base_url(request)
    trade_no = ids.merchant_trade_no("S")
    fields = ec.checkout_fields(
        merchant_trade_no=trade_no, amount=amount,
        item_name=body.item_name, trade_desc=body.trade_desc or body.item_name,
        choose_payment="Credit", base_url=base,
        order_result_url=f"{base}/ecpay/order-result",
        custom_fields={"custom1": body.custom1, "custom2": body.custom2},
        period=period)

    row = store.create(
        caller_id=caller.caller_id, reference_id=body.reference_id,
        merchant_trade_no=trade_no,
        period_amount=amount, period_type=period["period_type"],
        frequency=period["frequency"], exec_times=period["exec_times"],
        status="created", checkout_token=ids.checkout_token(),
        return_url=body.return_url, checkout_fields_json=json.dumps(fields))

    # 第一次嘗試也要進 trade_attempts —— caller 可能自己 render form，
    # 那條路不會經過導轉頁。
    attempts_store.record(trade_no, "subscription", row["id"])

    out = _out(row, base=base)
    out["form"] = {"action": s.aio_checkout_url, "method": "POST",
                   "fields": fields}
    return out


@router.get("")
def list_subscriptions(status: Optional[str] = None,
                       reference_id: Optional[str] = None,
                       limit: int = Query(default=50, ge=1, le=200),
                       offset: int = Query(default=0, ge=0),
                       caller: Caller = Depends(require("subscriptions:read"))):
    rows = store.list_(caller.caller_id, status=status, reference_id=reference_id,
                       limit=limit, offset=offset)
    return {"items": [_out(r) for r in rows]}


@router.get("/{sub_id}")
def get_subscription(sub_id: str, refresh: bool = False,
                     caller: Caller = Depends(require("subscriptions:read"))):
    """`?refresh=true` 去綠界的 QueryCreditCardPeriodInfo 對帳。

    那支 API 是**唯一**能確認「這張單真的是定期定額、扣了幾期」的途徑 ——
    首期的回呼跟一次性付款長得一模一樣，看回呼永遠看不出來。
    """
    row = store.get(caller.caller_id, sub_id)
    if not row:
        raise not_found("subscription")

    if refresh:
        try:
            data = ecsub.query(row["merchant_trade_no"])
        except ECPayError as e:
            raise upstream_error(e)
        # **只有綠界真的回了這些欄位才更新。** 讀不到就當作沒查到，
        # 絕不能用 0 覆蓋回呼存下來的正確數字 —— 對帳把資料弄丟比不對帳更糟。
        if data.get("ExecStatus") is not None:
            row = store.set_exec_status(row["id"], str(data["ExecStatus"]))
        if "TotalSuccessTimes" in data:
            times = int(data.get("TotalSuccessTimes") or 0)
            total = int(data.get("TotalSuccessAmount") or 0)
            if (times, total) != (row["total_success_times"],
                                  row["total_success_amount"]):
                row = store.set_totals(row["id"], times, total)
        # 綠界的 ExecLog 是每期扣款明細，補進本地（gwsr 去重，重跑安全）
        for log in (data.get("ExecLog") or []):
            gwsr = str(log.get("gwsr") or "")
            if gwsr:
                store.record_charge(
                    row["id"], gwsr=gwsr, amount=int(log.get("amount") or 0),
                    rtn_code=str(log.get("RtnCode", "1")),
                    auth_code=log.get("auth_code"),
                    process_date=log.get("process_date"))

    return _out(row, charges=store.charges(row["id"]))


@router.post("/{sub_id}/cancel")
def cancel_subscription(sub_id: str,
                        caller: Caller = Depends(require("subscriptions:write"))):
    """終止後續扣款。

    **不可復原**（綠界：終止後無法重新啟用，只能重開一張新單），
    而且**不退已收的錢** —— 取消 ≠ 退款。caller 應該讓權限給滿最後一期。
    """
    row = store.get(caller.caller_id, sub_id)
    if not row:
        raise not_found("subscription")
    if row["status"] == "cancelled":
        return _out(row)          # 冪等：已取消就回現況，不是錯誤
    if row["status"] not in ("active", "created"):
        raise bad_request(InvalidField(
            f"狀態 {row['status']} 無法終止", field="status"))

    try:
        ecsub.cancel(row["merchant_trade_no"])
    except ECPayError as e:
        raise upstream_error(e)

    row = store.set_status(row["id"], "cancelled", cancelled=True)
    return _out(row)
