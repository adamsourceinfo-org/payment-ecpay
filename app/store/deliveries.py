"""投遞紀錄 —— 「那筆到底送出去沒有」的答案。

狀態機（完整說明見設計文件）：

    schedule()  INSERT pending ──task 建成──► pending
                            └──task 建失敗──► failed (attempts = 0)
                                                  └── sweep 重排 ──► pending

    /internal/deliveries/{id}   attempts += 1
        ├── caller 回 2xx ────► delivered    （端點回 200，佇列停手）
        └── 其他 ─────────────► failed       （端點回 502，佇列重試）

    sweep（每小時）  pending / failed 且超過 2 × queue 窗口 ──► dead + ERROR

⚠️ `dead` **只由 sweep 標記**。投遞當下不判死信 —— 那需要知道「這是不是最後一次」，
而那個知識只有 queue 有。詳見 app/webhooks/dispatch.py 的 sweep()。
"""
from app import db

_COLUMNS = ("id, event_id, endpoint_id, caller_id, url, status, attempts,"
            " last_status, last_error, created_at, updated_at, delivered_at")


def create(event_id, endpoint_id, caller_id: str, url: str, tx=None):
    """event_id 為 None 代表這是 ping（見 POST /v1/webhook-endpoint/test）。"""
    return db.query(
        "INSERT INTO deliveries (event_id, endpoint_id, caller_id, url)"
        f" VALUES (%s,%s,%s,%s) RETURNING {_COLUMNS}",
        (event_id, endpoint_id, caller_id, url), fetch="one", tx=tx)


def get(delivery_id: str, tx=None):
    return db.query(f"SELECT {_COLUMNS} FROM deliveries WHERE id = %s",
                    (delivery_id,), fetch="one", tx=tx)


def get_for_caller(delivery_id: str, caller_id: str, tx=None):
    """別人的一律當作不存在 —— 跟 app/errors.py 的 not_found() 慣例一致。"""
    return db.query(
        f"SELECT {_COLUMNS} FROM deliveries WHERE id = %s AND caller_id = %s",
        (delivery_id, caller_id), fetch="one", tx=tx)


def exists_for_event(event_id, endpoint_id, tx=None) -> bool:
    row = db.query(
        "SELECT 1 AS x FROM deliveries WHERE event_id = %s AND endpoint_id = %s"
        " LIMIT 1", (event_id, endpoint_id), fetch="one", tx=tx)
    return bool(row)


def begin_attempt(delivery_id: str, tx=None):
    """遞增 attempts 並回更新後的那一列。

    ⚠️ `X-Delivery-Attempt` 取自這裡，**不是** Cloud Tasks 的
    `X-CloudTasks-TaskRetryCount`。sweep 重排過的 delivery 會拿到一個全新的
    task，那個 header 從 0 重新算 —— 用它的話 caller 會看到「第 1 次嘗試」
    出現在已經失敗二十次的 delivery 上。
    """
    return db.query(
        "UPDATE deliveries SET attempts = attempts + 1, updated_at = now()"
        f" WHERE id = %s RETURNING {_COLUMNS}",
        (delivery_id,), fetch="one", tx=tx)


def mark_delivered(delivery_id: str, http_status: int, tx=None):
    return db.query(
        "UPDATE deliveries SET status = 'delivered', last_status = %s,"
        " last_error = NULL, delivered_at = now(), updated_at = now()"
        f" WHERE id = %s RETURNING {_COLUMNS}",
        (http_status, delivery_id), fetch="one", tx=tx)


def mark_failed(delivery_id: str, http_status, error: str, tx=None):
    return db.query(
        "UPDATE deliveries SET status = 'failed', last_status = %s,"
        " last_error = %s, updated_at = now()"
        f" WHERE id = %s RETURNING {_COLUMNS}",
        (http_status, (error or "")[:2000], delivery_id), fetch="one", tx=tx)


def requeue(delivery_id: str, tx=None):
    """sweep 重排從未派送成功的那些。"""
    return db.query(
        "UPDATE deliveries SET status = 'pending', updated_at = now()"
        f" WHERE id = %s RETURNING {_COLUMNS}",
        (delivery_id,), fetch="one", tx=tx)


def list_for_caller(caller_id: str, event_id=None, status=None,
                    limit: int = 100, tx=None):
    sql = [f"SELECT {_COLUMNS} FROM deliveries WHERE caller_id = %s"]
    args = [caller_id]
    if event_id is not None:
        sql.append(" AND event_id = %s")
        args.append(event_id)
    if status:
        sql.append(" AND status = %s")
        args.append(status)
    sql.append(" ORDER BY created_at DESC LIMIT %s")
    args.append(limit)
    return db.query("".join(sql), tuple(args), tx=tx)


# --- sweep 用的三個查詢 ----------------------------------------------

def missing(limit: int, tx=None):
    """有 active 端點、事件已落地、卻連一列 deliveries 都沒有的。

    ⚠️ `received_at < now() - 5 minutes` 那一行**不能省**。沒有它，sweep 會跟
    正常路徑（回呼裡剛落地、正要排程的那一筆）賽跑，同一筆事件排兩次。

    ⚠️ 這個查詢會**回填新註冊的端點**，那是刻意的：它只看「有沒有 delivery 列」，
    不看「事件落地當下端點在不在」。所以 caller 第一次 PUT 之後，下一輪 sweep
    會把過去 48 小時的事件補推一次。契約上安全（原則 3 本來就要求用 id 去重），
    而且對新 caller 是功能 —— 接上推送之前那兩天的續期扣款不會憑空消失。
    """
    return db.query(
        "SELECT e.id AS event_id, e.caller_id, w.id AS endpoint_id, w.url"
        " FROM events e"
        " JOIN webhook_endpoints w"
        "   ON w.caller_id = e.caller_id AND w.active"
        " LEFT JOIN deliveries d"
        "   ON d.event_id = e.id AND d.endpoint_id = w.id"
        " WHERE e.caller_id IS NOT NULL"
        "   AND e.received_at >  now() - interval '48 hours'"
        "   AND e.received_at <  now() - interval '5 minutes'"
        "   AND d.id IS NULL"
        " ORDER BY e.id LIMIT %s", (limit,), tx=tx)


def never_dispatched(limit: int, tx=None):
    """`failed` 且 `attempts = 0` —— 唯一地代表「列建了但 task 沒建成」。"""
    return db.query(
        f"SELECT {_COLUMNS} FROM deliveries"
        " WHERE status = 'failed' AND attempts = 0"
        " ORDER BY created_at LIMIT %s", (limit,), tx=tx)


def mark_dead_older_than(seconds: float, limit: int, tx=None):
    """佇列已經放棄的那些。回被標記的列，讓呼叫端逐筆 log ERROR。

    ⚠️ `seconds` 由 sweep 向 **queue 本人**要（`retryConfig.maxRetryDuration` 的
    兩倍），不是環境變數。留一個 env 就是留第二份真相，而沒有任何東西在守它們一致
    —— 症狀會是「死信永遠不會被標記」，也就是這個欄位存在的唯一理由消失。
    """
    return db.query(
        "UPDATE deliveries SET status = 'dead', updated_at = now()"
        " WHERE id IN (SELECT id FROM deliveries"
        "              WHERE status IN ('pending','failed')"
        "                AND created_at < now() - make_interval(secs => %s)"
        "              ORDER BY created_at LIMIT %s)"
        f" RETURNING {_COLUMNS}", (seconds, limit), tx=tx)


def dead_count(hours: int = 24, tx=None) -> int:
    row = db.query(
        "SELECT count(*) AS n FROM deliveries WHERE status = 'dead'"
        "   AND updated_at > now() - make_interval(hours => %s)",
        (hours,), fetch="one", tx=tx)
    return int(row["n"]) if row else 0
