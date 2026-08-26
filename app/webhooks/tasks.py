"""Cloud Tasks，**直接打 REST，不裝套件**。

官方的 `google-cloud-tasks` 會把 `google-auth` 整包拉進來 —— 這個 repo 連 DB
driver 都挑 `pg8000` 是為了不編譯，為了排一個 task 拉進整包驗證函式庫不划算。

需要的東西全都已經在手上：
- access token → `app.db.iam_token()`（Cloud Run 的 metadata default token
  本來就是 `cloud-platform` scope，**不需要換 scope**）
- 專案 id → 同一個 metadata server
- 出站 HTTP → `httpx`，已經在 requirements.txt 裡

整條路一個新套件都不需要。

## 一個 caller 一個 queue

`max-concurrent-dispatches` 是**每個 queue** 的設定。共用一個 queue 意味著
一個 caller 的端點 timeout 10 秒就能佔滿全部派送槽位，排隊擋住其他所有 caller
—— 行銷活動當天，那等於「A 公司的活動把 B 公司的通知全排隊了」。

queue 由 `scripts/add-caller.sh` 在 caller 上線時建，**不由服務動態建**：
caller 上線本來就是人工步驟，多一行 gcloud 就換到完全隔離，
而且 runtime SA 不需要 `cloudtasks.admin`。對一個 allow_unauthenticated
的服務來說，不給那個權限是值得的。
"""
import hashlib
import logging
import re
import threading
import urllib.request

import httpx

from app import db
from app.config import get_settings

log = logging.getLogger("webhooks.tasks")

_API = "https://cloudtasks.googleapis.com/v2"
_METADATA_PROJECT_URL = (
    "http://metadata.google.internal/computeMetadata/v1/project/project-id")

_project_cache = None
_project_lock = threading.Lock()


class QueueMissing(RuntimeError):
    """指定的 queue 不存在。呼叫端應該退回共用 queue 並吵一聲。"""


def project_id() -> str:
    """向 metadata server 要，不做成環境變數 ——
    跟 db_status() 那條「回音環境變數證明不了任何事」同一個精神。"""
    global _project_cache
    with _project_lock:
        if _project_cache:
            return _project_cache
        req = urllib.request.Request(
            _METADATA_PROJECT_URL, headers={"Metadata-Flavor": "Google"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            _project_cache = resp.read().decode().strip()
        return _project_cache


def build_queue_name(prefix: str, caller_id: str) -> str:
    """`{prefix}-{消毒後的 caller}-{sha256 前 8 碼}`。

    Cloud Tasks 的 queue id 只收 `[A-Za-z0-9-]`、上限 100 字元。
    ⚠️ 尾巴那 8 碼雜湊不是裝飾：消毒會把 `a.b` 與 `a-b` 變成同一個字串，
    沒有雜湊的話兩個不同的 caller 會共用一個 queue —— 隔離就白做了。

    ⚠️ **`scripts/add-caller.sh` 建 queue 時呼叫的就是這個函式**（見那支腳本），
    不是自己抄一份規則。抄一份的話，某天改了消毒規則就會建出一個
    服務永遠找不到的 queue —— 而症狀是「靜靜地退回共用 queue」。
    """
    slug = re.sub(r"[^A-Za-z0-9-]", "-", caller_id)[:40].strip("-") or "caller"
    digest = hashlib.sha256(caller_id.encode()).hexdigest()[:8]
    return f"{prefix}-{slug}-{digest}"


def queue_name(caller_id: str) -> str:
    return build_queue_name(get_settings().tasks_queue_prefix, caller_id)


def shared_queue_name() -> str:
    """退路。per-caller queue 不存在時用它 —— 不會因為誰漏跑一行 gcloud 就掉事件。"""
    return get_settings().tasks_queue_prefix


def _queue_path(queue: str) -> str:
    s = get_settings()
    return (f"projects/{project_id()}/locations/{s.tasks_location}"
            f"/queues/{queue}")


def _auth() -> dict:
    return {"Authorization": f"Bearer {db.iam_token()}",
            "Content-Type": "application/json"}


def create_task(queue: str, url: str, headers: dict, timeout: float) -> str:
    """在 queue 裡排一個 POST task。回 task 名稱。

    task 的 body 是空的 —— 需要的東西全部在 deliveries 那一列裡，
    把 payload 塞進 task 只會多出一份會過期的副本。
    """
    body = {"task": {"httpRequest": {
        "url": url, "httpMethod": "POST", "headers": headers}}}
    resp = httpx.post(f"{_API}/{_queue_path(queue)}/tasks",
                      headers=_auth(), json=body, timeout=timeout)
    if resp.status_code == 404:
        raise QueueMissing(queue)
    resp.raise_for_status()
    return resp.json().get("name", "")


def retry_window_seconds(queue: str, timeout: float = 5.0):
    """queue 的 `retryConfig.maxRetryDuration`（秒）。取不到回 None。

    ⚠️ **這是「死信門檻」的唯一真相。** 設計上刻意不留 `WEBHOOK_MAX_ATTEMPTS`
    之類的環境變數 —— 那會是第二份真相，而且沒有任何東西在守它們一致。
    症狀會是「死信永遠不會被標記」，也就是那個欄位存在的唯一理由消失，
    而且沒有人會發現。
    """
    try:
        resp = httpx.get(f"{_API}/{_queue_path(queue)}",
                         headers=_auth(), timeout=timeout)
        resp.raise_for_status()
        raw = (resp.json().get("retryConfig") or {}).get("maxRetryDuration")
    except Exception as exc:                    # noqa: BLE001 — 取不到就退回常數
        log.warning("讀不到 queue %s 的 retryConfig：%s", queue, exc)
        return None
    if not raw:
        return None
    try:
        return float(str(raw).rstrip("s"))
    except ValueError:
        log.warning("queue %s 的 maxRetryDuration 看不懂：%r", queue, raw)
        return None


def enqueue_delivery(caller_id: str, delivery_url: str) -> None:
    """排一次投遞。per-caller queue 不在就退回共用 queue 並 log ERROR。

    ⚠️ 那一行 ERROR 要吵。不吵的話所有 caller 會靜靜地退化回共用 queue，
    公平性消失而沒有人知道 —— 直到活動當天。
    """
    s = get_settings()
    headers = {"X-Internal-Key": s.internal_key}
    queue = queue_name(caller_id)
    try:
        create_task(queue, delivery_url, headers,
                    s.webhook_enqueue_timeout_seconds)
        return
    except QueueMissing:
        log.error("caller %s 的 queue %s 不存在 —— 退回共用 queue。"
                  "請補跑 scripts/add-caller.sh 的建 queue 步驟", caller_id, queue)
    create_task(shared_queue_name(), delivery_url, headers,
                s.webhook_enqueue_timeout_seconds)
