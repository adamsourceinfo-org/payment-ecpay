"""caller 面對的推送設定與查詢。

端點用**單數**路徑（`/v1/webhook-endpoint`）—— 今天是一個 caller 一個端點。
資料表已經是多端點的形狀（uuid PK + caller_id 唯一索引），日後放寬時
會是一組新的**複數**路徑，不是把這幾支改語意。

⚠️ **這個模組的回應 body 永遠不進 log。** `PUT`／`GET` 回的是**明文簽章密鑰**，
而 app/main.py 的 _RedactFilter 遮不掉它（逐 caller 推導出來的是無界集合）。
所以規則是：這裡不要有任何 log.debug(response) 之類的東西。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.auth import Caller, require
from app.config import get_settings
from app.errors import FieldError, bad_request, not_found
from app.models import WebhookEndpointPut
from app.store import deliveries as deliveries_store
from app.store import events as events_store
from app.store import webhook_endpoints as endpoints_store
from app.urls import base_url
from app.webhooks import dispatch, signing, targets

log = logging.getLogger("webhooks")

router = APIRouter(tags=["webhooks"])

MAX_LIMIT = 500


def _require_push() -> None:
    """沒有簽章密鑰就算不出 secret。回一個沒有 secret 的物件只會讓 caller
    拿著空字串去驗簽 —— 那比誠實回 503 難查得多。"""
    if not get_settings().push_configured:
        raise HTTPException(
            status_code=503,
            detail="推送尚未設定（缺 WEBHOOK_SIGNING_KEY 或 INTERNAL_KEY）")


def _endpoint_out(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "url": row["url"],
        # 密鑰**每次都給** —— 它是推導出來的，隨時算得回來。
        # 所以不需要「只顯示一次」那套儀式，也不存在「密鑰弄丟了」這條路。
        "secret": signing.secret_for(row["caller_id"]),
        "active": row["active"],
        "updated_at": row["updated_at"],
    }


def _delivery_out(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "event_id": row["event_id"],
        "endpoint_id": str(row["endpoint_id"]),
        "url": row["url"],
        "status": row["status"],
        "attempts": row["attempts"],
        "last_status": row["last_status"],
        "last_error": row["last_error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "delivered_at": row["delivered_at"],
    }


@router.put("/v1/webhook-endpoint")
def put_endpoint(body: WebhookEndpointPut, request: Request,
                 caller: Caller = Depends(require("webhooks:write"))):
    """註冊或更新推送網址。upsert，**保留既有的 id**。

    一律回 200（不是 201）—— caller 不需要分辨這次是建立還是更新。
    """
    _require_push()
    try:
        url = targets.validate(body.url)
    except FieldError as e:
        raise bad_request(e)

    before = endpoints_store.get(caller.caller_id)
    row = endpoints_store.upsert(caller.caller_id, url)
    # 誰、什麼時候、從哪改到哪。網址是出款通知的去向，改動要留痕。
    log.info("webhook 端點變更 caller=%s 從 %s 改成 %s",
             caller.caller_id, (before or {}).get("url"), url)
    return _endpoint_out(row)


@router.get("/v1/webhook-endpoint")
def get_endpoint(caller: Caller = Depends(require("webhooks:read"))):
    _require_push()
    row = endpoints_store.get(caller.caller_id)
    if not row:
        raise not_found("webhook endpoint")
    return _endpoint_out(row)


@router.delete("/v1/webhook-endpoint")
def delete_endpoint(caller: Caller = Depends(require("webhooks:write"))):
    """**停用**推送，不刪列。事件照樣落地，`GET /v1/events` 照樣拉得到。

    回 200 帶更新後的物件（`active: false`），不是 204 ——
    caller 因此不必再打一次 GET 確認。
    """
    _require_push()
    row = endpoints_store.deactivate(caller.caller_id)
    if not row:
        raise not_found("webhook endpoint")
    log.info("webhook 端點停用 caller=%s", caller.caller_id)
    return _endpoint_out(row)


@router.post("/v1/webhook-endpoint/test", status_code=202)
def test_endpoint(request: Request,
                  caller: Caller = Depends(require("webhooks:write"))):
    """送一筆合成的 `ping`。**events 表不會多出任何一列。**

    它走的是**真的**佇列與內部端點 —— 這支存在的意義就是
    「在沒有任何真實金流的情況下驗完整條路」，跳過最會壞的那四樣就沒有意義了。
    """
    _require_push()
    row = dispatch.send_test_ping(caller.caller_id, base_url(request))
    if not row:
        raise bad_request("尚未註冊推送端點，或端點已停用")
    return _delivery_out(row)


@router.get("/v1/deliveries")
def list_deliveries(event_id: int = Query(default=None),
                    status: str = Query(default=None),
                    limit: int = Query(default=100, ge=1, le=MAX_LIMIT),
                    caller: Caller = Depends(require("webhooks:read"))):
    """「那筆到底送出去沒有」的答案。只看得到自己的。"""
    _require_push()
    rows = deliveries_store.list_for_caller(
        caller.caller_id, event_id=event_id, status=status, limit=limit)
    return {"items": [_delivery_out(r) for r in rows]}


@router.post("/v1/events/{event_id}/redeliver", status_code=202)
def redeliver(event_id: int, request: Request,
              caller: Caller = Depends(require("webhooks:write"))):
    """重新排一次投遞。建**新的一列**，舊的不動 —— 投遞史要看得到。

    ⚠️ `events.id` 是全域 bigserial，所有 caller 共用同一個序號空間。
    不擋的話 caller 可以拿別人的 id 去試探。**別人的事件（含 caller_id IS NULL）
    一律回 404，不是 403** —— 403 會洩漏「該資源存在」。
    """
    _require_push()
    event = events_store.get(event_id)
    if not event or event["caller_id"] != caller.caller_id:
        raise not_found("event")
    row = dispatch.redeliver(event_id, caller.caller_id, base_url(request))
    if not row:
        raise bad_request("尚未註冊推送端點，或端點已停用")
    return _delivery_out(row)
