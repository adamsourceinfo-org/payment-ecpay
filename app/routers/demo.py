"""付款流程的示範商店。**只在 dev 開，prod 一律 404。**

## 這支存在的理由

推送、事件、投遞紀錄那幾支端點都可以用 curl 驗，但**付款者實際會走的那條路**
不行 —— 它包含瀏覽器導轉、綠界收銀台、導回、以及「導回與幕後回呼誰先到」
的競態。那條路只有用真的瀏覽器走一遍才驗得到。

## 為什麼做在服務裡面，而不是一個外部的靜態頁

試過了，外部頁面走不通：

- **沒有 CORS。** 服務刻意不發 `Access-Control-*`，所以跨來源的頁面連
  `POST /v1/orders` 都打不到。為了一個示範頁在收錢的服務上開跨來源不划算。
- **就算開了 CORS 也還是不行。** 託管的 artifact 跑在
  `sandbox="allow-scripts allow-same-origin allow-forms"` 的 iframe 裡 ——
  沒有 `allow-top-navigation` 也沒有 `allow-popups`，導不去綠界，
  而且它的來源是每個頁面獨立的網域，`return_url` 也填不了。

做在同源就三個問題一起消失：導轉是一般導覽、`return_url` 是穩定的真實網址、
**而且 API key 完全不用進瀏覽器** —— 建單在伺服器端用 demo caller 的身分做，
就跟真的 caller 的後端一樣。

## 安全邊界

- `DEMO_CALLER_ID` 沒設就整組 404。它只寫進 `.cicd/env.dev`，prod 沒有。
- 這裡不驗 API key（沒有 key 可驗），所以**能做的事被寫死**：
  只能用 demo caller 的身分建單與查自己的東西。改不了推送端點、
  看不到別的 caller 的任何資料。
- demo caller 的資料跟真 caller 一樣受 caller_id 隔離 —— 走的是同一套 store。
"""
import hmac
import json
import logging
import secrets
import time
from collections import deque
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.auth import Caller
from app.config import get_settings
from app.models import OrderCreate, SubscriptionCreate
from app.routers import orders as orders_router
from app.routers import subscriptions as subs_router
from app.store import attempts as attempts_store
from app.store import deliveries as deliveries_store
from app.store import events as events_store
from app.store import orders as orders_store
from app.store import subscriptions as subs_store
from app.urls import base_url
from app.webhooks import signing

log = logging.getLogger("demo")

router = APIRouter(prefix="/demo", tags=["demo"])

_PAGE = Path(__file__).resolve().parent.parent / "demo" / "storefront.html"

# demo caller 能做的事寫死在這裡。少一個字都不給 ——
# 這支端點沒有 API key 可驗，所以權限只能來自程式碼。
_SCOPES = frozenset({
    "orders:read", "orders:write",
    "subscriptions:read", "subscriptions:write",
    "events:read", "webhooks:read",
})


def _caller() -> Caller:
    s = get_settings()
    if not s.demo_caller_id:
        # prod 沒設 DEMO_CALLER_ID，整組端點等於不存在
        raise HTTPException(status_code=404, detail="not found")
    return Caller(caller_id=s.demo_caller_id, scopes=_SCOPES)


@router.get("", response_class=HTMLResponse)
def storefront():
    _caller()
    return HTMLResponse(_PAGE.read_text(encoding="utf-8"))


@router.post("/api/checkout")
def checkout(body: dict, request: Request):
    """建一筆單，回 checkout_url。

    直接呼叫 /v1 的建單函式 —— **不重寫一份**。那兩支帶著金額驗證、
    付款方式白名單、最低金額、冪等鍵的全部邏輯，示範商店要走的就是同一條路，
    不然示範出來的東西跟 caller 真的會遇到的不一樣。
    """
    caller = _caller()
    kind = body.get("kind")
    # 每次都要是**新的** reference_id —— 撞到冪等的話回的是前一筆的
    # checkout_url，而那個 token 已經用掉了，使用者會看到「這筆已經完成付款」。
    # ⚠️ 只用毫秒不夠：連點兩下可以落在同一毫秒（測試就抓到了）。
    ref = f"demo-{kind}-{int(time.time() * 1000)}-{secrets.token_hex(3)}"
    base = base_url(request)
    ret = f"{base}/demo"

    if kind == "order":
        out = orders_router.create_order(
            OrderCreate(
                reference_id=ref,
                amount=int(body.get("amount") or 0),
                item_name=str(body.get("item_name") or "示範商品"),
                choose_payment=str(body.get("choose_payment") or "Credit"),
                return_url=ret,
            ), request, caller)
    elif kind == "subscription":
        out = subs_router.create_subscription(
            SubscriptionCreate(
                reference_id=ref,
                amount=int(body.get("amount") or 0),
                item_name=str(body.get("item_name") or "示範訂閱"),
                period_type=str(body.get("period_type") or "M"),
                frequency=int(body.get("frequency") or 1),
                return_url=ret,
            ), request, caller)
    else:
        raise HTTPException(status_code=400, detail="kind 只能是 order 或 subscription")

    # create_* 在冪等命中時回的是 JSONResponse，其餘是 dict
    if not isinstance(out, dict):
        raise HTTPException(status_code=500, detail="reference_id 撞號了，再試一次")

    log.info("demo 建單 kind=%s ref=%s id=%s", kind, ref, out.get("id"))
    return {"kind": kind, "id": out["id"],
            "merchant_trade_no": out["merchant_trade_no"],
            "checkout_url": out.get("checkout_url")}


@router.get("/api/status")
def status(kind: str = Query(default=None), id: str = Query(default=None),
           trade_no: str = Query(default=None)):
    """查一筆的狀態。

    `trade_no` 是給「導回之後」用的：綠界導回時我們只拿得到單號，
    而且**單號會換**（送出過就不能再用），所以一律透過 trade_attempts 解析 ——
    跟 /ecpay/return 走的是同一條路。
    """
    caller = _caller()

    if trade_no and not id:
        hit = attempts_store.resolve(trade_no)
        if not hit:
            raise HTTPException(status_code=404, detail="查不到這個單號")
        kind, id = hit["subject_kind"], str(hit["subject_id"])

    if kind == "order":
        row = orders_store.get(caller.caller_id, id)
        if not row:
            raise HTTPException(status_code=404, detail="查不到這筆訂單")
        return {"kind": "order",
                **orders_router._out(row, info=orders_store.payment_info(id))}

    if kind == "subscription":
        row = subs_store.get(caller.caller_id, id)
        if not row:
            raise HTTPException(status_code=404, detail="查不到這筆訂閱")
        return {"kind": "subscription",
                **subs_router._out(row, charges=subs_store.charges(id))}

    raise HTTPException(status_code=400, detail="kind 只能是 order 或 subscription")


@router.get("/api/feed")
def feed(after: int = Query(default=0, ge=0)):
    """caller 後台看得到的東西：事件（拉取）與投遞紀錄（推送）。

    這一支存在是為了讓示範頁能並排顯示「付款者看到什麼」與
    「caller 的後台同時發生了什麼」—— 那個對照才是這個示範真正的內容。
    """
    caller = _caller()
    rows = events_store.list_after(caller.caller_id, after, 20)
    dels = deliveries_store.list_for_caller(caller.caller_id, limit=10)
    return {
        "events": [
            {"id": r["id"], "event_type": r["event_type"],
             "subject_kind": r["subject_kind"], "subject_id": r["subject_id"],
             "rtn_code": (r["payload"] or {}).get("RtnCode"),
             "received_at": r["received_at"]}
            for r in rows
        ],
        "next_cursor": rows[-1]["id"] if rows else after,
        "deliveries": [
            {"id": str(d["id"]), "event_id": d["event_id"],
             "status": d["status"], "attempts": d["attempts"],
             "last_status": d["last_status"], "created_at": d["created_at"]}
            for d in dels
        ],
    }


# =====================================================================
# 示範用的**接收端** —— 這一段是 caller 那一側，不是我們這一側
# =====================================================================
#
# 沒有這支的話，示範只演到「我們送出去了、對方回 200」，而那是
# deliveries 表的視角。付款流程裡真正要驗的是**對方收到什麼、驗簽過不過** ——
# 那一半才是 caller 要寫的程式碼。
#
# 所以這支刻意寫成 caller 可以照抄的樣子：驗簽、防重放、去重、回 2xx。
# 它跑在我們的行程裡只是因為方便，邏輯上它屬於 caller。

_INBOX = deque(maxlen=20)

# ⚠️ 記在記憶體裡，不進資料庫。理由：這是 dev 的示範，
# 為它開一張表會連 prod 都跟著多一張空表。代價要說清楚 ——
# 實例被回收或同時有多個實例時，這個清單會空掉或看起來少一筆。
# 「那筆到底送出去沒有」的權威答案永遠是 deliveries，不是這裡。


@router.post("/hook")
async def demo_hook(request: Request):
    """示範 caller 的接收端。**這段程式碼就是 caller 要寫的東西。**"""
    caller = _caller()
    raw = await request.body()          # ⚠️ 原始 bytes，不要先 parse 再重組
    header = request.headers.get("x-signature", "")

    # 1) 解析 t 與 v1。不要用 split("=", 1) 之外的偷懶寫法 ——
    #    值裡真的出現 "=" 時會被切壞。
    parts = {}
    for kv in header.split(","):
        k, sep, v = kv.partition("=")
        if sep:
            parts[k.strip()] = v.strip()

    now = int(time.time())
    ok, why = False, ""
    try:
        t = int(parts.get("t", ""))
    except ValueError:
        why = "標頭裡沒有可解析的 t"
        t = None

    if t is not None:
        # 2) 防重放：時間戳超出容忍就拒，不要只看簽章對不對
        if abs(now - t) > signing.TOLERANCE_SECONDS:
            why = f"時間戳超出容忍（{abs(now - t)} 秒）"
        else:
            secret = signing.secret_for(caller.caller_id)
            expected = signing.signature(secret, t, raw)
            # 3) constant-time 比較
            ok = hmac.compare_digest(expected, parts.get("v1", ""))
            why = "" if ok else "簽章不符"

    body = {}
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:                   # noqa: BLE001 — 驗不過的東西本來就可能不是 JSON
        pass

    event_id = body.get("id")
    event_type = body.get("event_type")

    # 4) ping 要在去重**之前**就處理掉 —— 它的 id 固定是 0，
    #    照順序去重的話第二次 ping 會被自己擋掉，看起來像沒送到。
    is_ping = event_type == "ping"
    dup = False
    if ok and not is_ping:
        # 5) 用驗過簽的 body 裡的 id 去重（**不是** X-Event-Id，那個沒驗過）
        dup = any(r["event_id"] == event_id and r["ok"] and not r["duplicate"]
                  for r in _INBOX)

    _INBOX.appendleft({
        "at": now,
        "ok": ok,
        "why": why,
        "duplicate": dup,
        "ping": is_ping,
        "event_id": event_id,
        "event_type": event_type,
        "rtn_code": (body.get("payload") or {}).get("RtnCode"),
        "attempt": request.headers.get("x-delivery-attempt"),
        "delivery_id": request.headers.get("x-delivery-id"),
        "bytes": len(raw),
    })

    if not ok:
        # 驗不過就當作沒收到。回 400 讓它重試沒有意義（重試也一樣驗不過），
        # 但回 2xx 等於默默吞掉 —— 所以回 400 並在自己的 log 留痕。
        log.warning("demo 接收端驗簽失敗：%s", why)
        raise HTTPException(status_code=400, detail=why or "驗簽失敗")

    # 6) 處理成功才回 2xx。這裡失敗的話應該回 500 讓我們重送 ——
    #    純拉取沒有這個機制，推送把那個安全網還給 caller。
    return {"received": event_id, "duplicate": dup}


@router.get("/api/inbox")
def inbox():
    """caller 那一側收到了什麼。見 _INBOX 上面那段關於「記在記憶體裡」的代價。"""
    _caller()
    return {"items": list(_INBOX)}
