"""服務打給自己的端點。Cloud Tasks 與 Cloud Scheduler 是唯二的呼叫方。

服務是 `allow_unauthenticated: true`（綠界的回呼必須打得到），所以這幾支
對公網開著，必須在**應用層**擋：建 task 時帶 `X-Internal-Key`，這裡比對。

**為什麼不是 OIDC**：要引入 `google-auth` 驗簽。這個 repo 連 DB driver 都挑
`pg8000` 是為了不編譯，為一支內部端點拉進整包驗證函式庫不划算。而且
Cloud Tasks 用 OIDC 時 runtime SA 對自己**沒有**隱含的 actAs，那個 IAM
授權還要另外補進 runbook。靜態 header 跟 API key 是同一個安全模型
（一個共享機密，只存在服務與呼叫方之間），而這裡的呼叫方就是服務自己。

⚠️ INTERNAL_KEY 會出現在 Cloud Scheduler 的 job 設定裡，任何有
`scheduler.viewer` 的人看得到。它靠專案的 IAM 保護，不是靠對專案成員保密。

## 這些 handler 是同步 def

FastAPI 會把同步 handler 丟進 threadpool —— 投遞會打 caller 的端點，
最長 WEBHOOK_TIMEOUT_SECONDS 秒。寫成 async def 就會卡住事件迴圈，
那正是 app/routers/callbacks.py 開頭那段說明在講的 bug。
"""
import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, Request, Response

from app.config import get_settings
from app.urls import base_url
from app.webhooks import dispatch

log = logging.getLogger("internal")

router = APIRouter(prefix="/internal", tags=["internal"])


def _check(key: str) -> None:
    s = get_settings()
    if not s.push_configured:
        raise HTTPException(status_code=503, detail="推送尚未設定")
    # ⚠️ 一定要 encode 再比。`hmac.compare_digest` 的 str 版本只吃 ASCII，
    # 而 HTTP header 可以帶 latin-1 字元 —— 隨便一個亂打的 key 就會拋
    # TypeError，讓這支端點回 500 加一份 stack trace，而不是設計上的 401。
    if not key or not hmac.compare_digest(key.encode("utf-8", "replace"),
                                          s.internal_key.encode()):
        # 跟 API key 一樣：沒帶、錯的一律同一個回應，不幫攻擊者縮小範圍
        raise HTTPException(status_code=401, detail="invalid internal key")


# ⚠️ sweep 要註冊在 /{delivery_id} **前面**，否則 "sweep" 會被當成一個 id。
@router.post("/deliveries/sweep")
def sweep(request: Request, x_internal_key: str = Header(default=None)):
    """Cloud Scheduler 每小時打一次。

    存在的理由是綠界的一個事實：**一旦我們落地並回了 `1|OK`，它就不再重送。**
    所以「事件收了但沒排出去」無法靠上游自癒 —— 只能自己補。
    """
    _check(x_internal_key)
    return dispatch.sweep(base_url(request))


@router.post("/deliveries/{delivery_id}")
def deliver(delivery_id: str, response: Response,
            x_internal_key: str = Header(default=None)):
    """Cloud Tasks 派送到這裡，這裡才真的 POST 給 caller。

    ⚠️ **回應碼是給 Cloud Tasks 看的，不是給人看的。**
    投遞失敗必須回非 2xx，佇列才會重試；已經是終態（delivered / dead）
    就回 200 讓佇列停手 —— Cloud Tasks 是至少一次的，重複派送很正常。
    """
    _check(x_internal_key)
    outcome, http_status = dispatch.deliver(delivery_id)

    if outcome == "failed":
        # 502 是說給佇列聽的「請再試一次」。delivery 的 DB 更新已經做完了。
        response.status_code = 502
    elif outcome == "missing":
        # 那一列不見了，重試沒有意義 —— 回 200 讓佇列停手。
        log.warning("delivery %s 不存在，停止重試", delivery_id)

    return {"delivery": delivery_id, "outcome": outcome,
            "caller_status": http_status}
