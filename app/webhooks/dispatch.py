"""排程、投遞、補漏。這一層不碰 FastAPI，也不碰 async。

## 併發模型

**這個模組底下全部是同步程式碼。** 它由回呼的 threadpool 路徑呼叫
（見 app/routers/callbacks.py 的說明），出站 HTTP 用同步的 `httpx.post`。
不要在這裡引入 async —— 混用正是「四支 async 回呼跑同步 pg8000」那個
阻塞事件迴圈的 bug 的來源。

## 三個入口

- `schedule()` 事件剛落地，排一次投遞
- `ensure()`   `record()` 回了 None（另一個入口先到，或綠界重送）——
               本地狀態已經對了，但推送可能還沒有人排
- `sweep()`    每小時補漏、重排、標死信
"""
import json
import logging
from datetime import datetime, timezone

import httpx
from fastapi.encoders import jsonable_encoder

from app.config import get_settings
from app.store import deliveries as deliveries_store
from app.store import events as events_store
from app.store import webhook_endpoints as endpoints_store
from app.webhooks import signing, targets, tasks

log = logging.getLogger("webhooks.dispatch")

USER_AGENT = "payment-ecpay/1"
PING_EVENT_TYPE = "ping"

# sweep 一輪抓幾筆、最多處理幾筆。
# ⚠️ 撞到 _MAX_PER_RUN 一定要 log —— 靜默截斷從外面看起來就像「已經全部補完了」。
_BATCH = 500
_MAX_PER_RUN = 5000
# 讀不到 queue 設定時的退路。刻意抓寬（真實窗口是 12 小時）：
# 抓寬在兩個方向都安全 —— 不會誤判還在重試的，真死信最晚 24 小時內也看得到。
_DEFAULT_WINDOW_SECONDS = 12 * 3600


def internal_url(base_url: str, delivery_id) -> str:
    return f"{base_url.rstrip('/')}/internal/deliveries/{delivery_id}"


def event_payload(row: dict) -> dict:
    """推送的 body **就是** `GET /v1/events` 回應裡 items[] 的一個元素，逐欄相同。

    caller 因此只要寫一份 parser，兩條路都能吃。兩個形狀就是兩份程式碼、
    兩組 bug，而其中一份平常不會執行 —— 那是最糟的一種程式碼。
    """
    return {
        "id": row["id"],
        "event_type": row["event_type"],
        "subject_kind": row["subject_kind"],
        "subject_id": row["subject_id"],
        "payload": row["payload"],
        "received_at": row["received_at"],
    }


def ping_payload() -> dict:
    """⚠️ `id` 固定是 0。caller 照原則 3 用 id 去重的話，第二次 ping 會被
    自己的去重擋掉、看起來像沒送到 —— 所以 README 必須寫
    「`event_type == "ping"` 要在去重之前就 return」。"""
    return {
        "id": 0,
        "event_type": PING_EVENT_TYPE,
        "subject_kind": None,
        "subject_id": None,
        "payload": {},
        "received_at": datetime.now(timezone.utc),
    }


def encode(body: dict) -> bytes:
    """序列化一次，**簽的與送的是同一份 bytes**。

    分成「簽一份、送另一份」是這類系統的經典 bug：重新 json.dumps 出來的
    字串跟原文不保證逐位元組相同，而且只在有中文的 payload 上才發作。
    """
    return json.dumps(jsonable_encoder(body), ensure_ascii=False,
                      separators=(",", ":")).encode()


# --- 排程 -------------------------------------------------------------

def _enqueue(delivery: dict, base_url: str) -> None:
    """建 task；失敗就把那一列標成 failed/attempts=0 讓 sweep 撿回去。"""
    try:
        tasks.enqueue_delivery(delivery["caller_id"],
                               internal_url(base_url, delivery["id"]))
    except Exception as exc:                    # noqa: BLE001
        # ⚠️ 排程失敗**不可以**讓綠界的回呼回非 1|OK。上游的重送是為了
        # 「事件沒收到」，不是為了「我們沒轉給 caller」——
        # 而且我們一旦回過 1|OK，綠界就再也不會給第二次機會。
        log.error("排程失敗 delivery=%s caller=%s：%s: %s",
                  delivery["id"], delivery["caller_id"],
                  type(exc).__name__, exc)
        deliveries_store.mark_failed(
            delivery["id"], None, f"enqueue: {type(exc).__name__}: {exc}")


def schedule(event_id, caller_id: str, base_url: str) -> None:
    """事件剛落地，排一次投遞。**永遠不對外拋例外。**"""
    try:
        s = get_settings()
        if not s.push_configured or event_id is None or not caller_id:
            return
        endpoint = endpoints_store.get_active(caller_id)
        if not endpoint:
            return
        delivery = deliveries_store.create(
            event_id, endpoint["id"], caller_id, endpoint["url"])
        _enqueue(delivery, base_url)
    except Exception as exc:                    # noqa: BLE001
        log.error("schedule 失敗 event=%s caller=%s：%s: %s",
                  event_id, caller_id, type(exc).__name__, exc)


def ensure(dedupe_key: str, base_url: str) -> None:
    """`record()` 回了 None 時走這裡。**永遠不對外拋例外。**

    ⚠️ None 有兩個意思：綠界重送，或者這一筆已經被 `/ecpay/order-result`
    那條路處理掉了。第二種情況下**沒有人排過推送** —— 照舊早退的話，
    它會靜默退化成 sweep 的一小時延遲，而且正好發生在活動期間。

    所以這裡的工作是「確保 delivery 列存在」，不是「什麼都不做」。
    """
    try:
        s = get_settings()
        if not s.push_configured:
            return
        event = events_store.get_by_dedupe_key(dedupe_key)
        if not event or not event["caller_id"]:
            return
        endpoint = endpoints_store.get_active(event["caller_id"])
        if not endpoint:
            return
        if deliveries_store.exists_for_event(event["id"], endpoint["id"]):
            return                              # 另一個入口已經排過了
        delivery = deliveries_store.create(
            event["id"], endpoint["id"], event["caller_id"], endpoint["url"])
        _enqueue(delivery, base_url)
    except Exception as exc:                    # noqa: BLE001
        log.error("ensure 失敗 dedupe=%s：%s: %s",
                  dedupe_key, type(exc).__name__, exc)


def send_test_ping(caller_id: str, base_url: str):
    """`POST /v1/webhook-endpoint/test`。回那一列 delivery。

    ⚠️ 走**真的**佇列，不是同步直送。同步直送會跳過 Cloud Tasks、內部端點、
    X-Internal-Key、重試 —— 而那四樣正好是最會壞的部分，
    「在沒有任何真實金流的情況下驗完整條路」如果驗不到它們就沒有意義。
    `deliveries.event_id` 可為 NULL 就是為了這件事。
    """
    endpoint = endpoints_store.get_active(caller_id)
    if not endpoint:
        return None
    delivery = deliveries_store.create(
        None, endpoint["id"], caller_id, endpoint["url"])
    _enqueue(delivery, base_url)
    return deliveries_store.get(delivery["id"])


# --- 投遞 -------------------------------------------------------------

def deliver(delivery_id) -> tuple:
    """真正打 caller 的地方。回 (結果, http 狀態)。

    結果是 `delivered` / `failed` / `done`（已經是終態，佇列重複派送了）
    / `missing`。呼叫端據此決定回給 Cloud Tasks 的狀態碼。
    """
    row = deliveries_store.get(delivery_id)
    if not row:
        return "missing", None
    if row["status"] in ("delivered", "dead"):
        # Cloud Tasks 是至少一次的。已經是終態就直接停手，不要再送一次給 caller。
        return "done", row["last_status"]

    row = deliveries_store.begin_attempt(delivery_id)
    s = get_settings()

    if row["event_id"] is None:
        body = ping_payload()
    else:
        event = events_store.get(row["event_id"])
        if not event:
            deliveries_store.mark_failed(delivery_id, None, "事件不見了")
            return "failed", None
        body = event_payload(event)

    raw = encode(body)
    t = int(datetime.now(timezone.utc).timestamp())
    secret = signing.secret_for(row["caller_id"])
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "X-Signature": signing.header(secret, t, raw),
        # header 是為了讓 caller 在**還沒驗簽之前**就記得下 log。
        # ⚠️ 它不可信，別拿它當去重鍵 —— 去重要用驗過簽的 body 裡的 id。
        "X-Event-Id": str(body["id"]),
        "X-Event-Type": str(body["event_type"]),
        "X-Delivery-Id": str(row["id"]),
        "X-Delivery-Attempt": str(row["attempts"]),
    }

    try:
        # 註冊時只擋得掉字面上的內網位址；「公開網域指到 169.254.169.254」
        # 只有在這裡（解析之後）擋得掉。redirect 一律不跟 —— 跟了就繞過這一關。
        targets.assert_public(row["url"])
        resp = httpx.post(row["url"], content=raw, headers=headers,
                          timeout=s.webhook_timeout_seconds,
                          follow_redirects=False)
    except Exception as exc:                    # noqa: BLE001 — timeout 也算失敗
        deliveries_store.mark_failed(
            delivery_id, None, f"{type(exc).__name__}: {exc}")
        return "failed", None

    if 200 <= resp.status_code < 300:
        deliveries_store.mark_delivered(delivery_id, resp.status_code)
        return "delivered", resp.status_code

    deliveries_store.mark_failed(delivery_id, resp.status_code,
                                 (resp.text or "")[:500])
    return "failed", resp.status_code


# --- sweep ------------------------------------------------------------

def dead_threshold_seconds() -> float:
    """向 **queue 本人**要，取 `maxRetryDuration` 的兩倍。

    ⚠️ 這是刻意不留 `WEBHOOK_MAX_ATTEMPTS` 之類環境變數的原因：
    那會是第二份真相，而沒有任何東西在守它們一致。改一邊忘另一邊的症狀是
    **死信永遠不會被標記** —— 也就是那個欄位存在的唯一理由消失，而且沒有人發現。

    取兩倍是為了讓誤差在兩個方向都安全：不會誤判還在重試的，
    真死信也最晚在兩倍窗口內看得到。
    """
    window = tasks.retry_window_seconds(tasks.shared_queue_name())
    if window is None:
        log.warning("讀不到 queue 的 maxRetryDuration，退回 %s 秒",
                    _DEFAULT_WINDOW_SECONDS)
        window = _DEFAULT_WINDOW_SECONDS
    return 2 * window


def sweep(base_url: str) -> dict:
    """每小時一次。回一份數字讓端點吐出去，人看得到它有沒有在追進度。"""
    filled = requeued = dead = 0
    truncated = False

    # 1. 補漏：事件在、端點 active、卻連一列 delivery 都沒有。
    #    迴圈掃到當輪回不滿 _BATCH 為止 —— 只掃一批的話，
    #    突發漏掉一萬筆要二十小時才排乾，而且沒有人知道它在追。
    while True:
        if filled >= _MAX_PER_RUN:
            truncated = True
            break
        rows = deliveries_store.missing(_BATCH)
        if not rows:
            break
        for r in rows:
            delivery = deliveries_store.create(
                r["event_id"], r["endpoint_id"], r["caller_id"], r["url"])
            _enqueue(delivery, base_url)
            filled += 1
        if len(rows) < _BATCH:
            break                               # 這一輪掃乾淨了

    # 2. 重排從未派送成功的（failed 且 attempts = 0 = task 根本沒建成）
    for r in deliveries_store.never_dispatched(_BATCH):
        deliveries_store.requeue(r["id"])
        _enqueue(r, base_url)
        requeued += 1

    # 3. 標死信。不標的話「送不出去的事件」只存在於 Cloud Tasks 的統計裡，
    #    服務自己答不出來 —— 而那正是這個欄位存在的唯一理由。
    for r in deliveries_store.mark_dead_older_than(
            dead_threshold_seconds(), _BATCH):
        log.error("死信 delivery=%s event=%s caller=%s url=%s "
                  "attempts=%s last_status=%s last_error=%s",
                  r["id"], r["event_id"], r["caller_id"], r["url"],
                  r["attempts"], r["last_status"], r["last_error"])
        dead += 1

    if truncated:
        log.warning("sweep 補漏撞到單輪上限 %s 筆，還有沒補完的 —— "
                    "下一輪會繼續，但如果一直撞到就要看 queue 或 caller 是不是壞了",
                    _MAX_PER_RUN)

    result = {"filled": filled, "requeued": requeued, "dead": dead,
              "truncated": truncated}
    log.info("sweep 完成 %s", result)
    return result


def redeliver(event_id, caller_id: str, base_url: str):
    """`POST /v1/events/{id}/redeliver`。回**新建的**那一列，或 None（沒有端點）。

    刻意建新的一列而不是重置舊的 —— `GET /v1/deliveries?event_id=` 因此
    看得到完整的投遞史，包括「當初送去哪個網址」。
    """
    endpoint = endpoints_store.get_active(caller_id)
    if not endpoint:
        return None
    delivery = deliveries_store.create(
        event_id, endpoint["id"], caller_id, endpoint["url"])
    _enqueue(delivery, base_url)
    return deliveries_store.get(delivery["id"])
