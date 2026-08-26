from fastapi import APIRouter, Depends, Query

from app.auth import Caller, require
from app.store import events as store

router = APIRouter(prefix="/v1/events", tags=["events"])

MAX_LIMIT = 500


@router.get("")
def list_events(after: int = Query(default=0, ge=0),
                limit: int = Query(default=100, ge=1, le=MAX_LIMIT),
                caller: Caller = Depends(require("events:read"))):
    """游標式增量拉取。caller 記住最後一筆的 id 當下次的 after。

    傳 after=0 從頭拉，這也是對帳的路徑。

    **這是兩條出口之一。** 另一條是主動推送（見 app/webhooks/），
    而推送的 body 就是底下 items[] 的一個元素、逐欄相同 ——
    caller 因此只要寫一份 parser。改這裡的形狀時，
    app/webhooks/dispatch.py 的 event_payload() 要一起改。

    ⚠️ 即使接了推送，這支端點仍然是安全網：有界的重試不等於保證送達。
    """
    rows = store.list_after(caller.caller_id, after, limit)
    return {
        "items": [{"id": r["id"], "event_type": r["event_type"],
                   "subject_kind": r["subject_kind"],
                   "subject_id": r["subject_id"],
                   "payload": r["payload"],
                   "received_at": r["received_at"]} for r in rows],
        "next_cursor": rows[-1]["id"] if rows else after,
    }
