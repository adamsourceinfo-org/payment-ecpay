"""caller 註冊的推送端點。

今天是「一個 caller 一個端點」—— 而那件事的**全部實作**就是
migration 裡的 `webhook_endpoints_caller` 唯一索引。日後要放寬只要刪掉它，
不用改 PK、不用回填，`deliveries.endpoint_id` 從第一天就存在。

刻意沒有 secret 欄位：簽章密鑰由 WEBHOOK_SIGNING_KEY 推導（見 app/webhooks/signing.py）。
"""
from app import db


def upsert(caller_id: str, url: str, tx=None):
    """註冊或更新。**保留既有的 id** —— 投遞紀錄的外鍵不斷，密鑰也不變。

    `DELETE` 之後再 upsert 會把 active 設回 true，這是刻意的：
    停用是暫時的營運動作，不該讓 caller 失去自己的端點身分。
    """
    return db.query(
        "INSERT INTO webhook_endpoints (caller_id, url)"
        " VALUES (%s,%s)"
        " ON CONFLICT (caller_id) DO UPDATE SET url = EXCLUDED.url,"
        "   active = true, updated_at = now()"
        " RETURNING *", (caller_id, url), fetch="one", tx=tx)


def get(caller_id: str, tx=None):
    """含已停用的。`GET /v1/webhook-endpoint` 要看得到 active=false。"""
    return db.query("SELECT * FROM webhook_endpoints WHERE caller_id = %s",
                    (caller_id,), fetch="one", tx=tx)


def get_active(caller_id: str, tx=None):
    """排程時用。停用的一律不推。"""
    if not caller_id:
        return None
    return db.query(
        "SELECT * FROM webhook_endpoints WHERE caller_id = %s AND active",
        (caller_id,), fetch="one", tx=tx)


def deactivate(caller_id: str, tx=None):
    """**軟停用，不刪列。** 三個理由：deliveries.endpoint_id 的外鍵不會斷、
    「這筆當初送去哪」永遠答得出來、caller 停用再啟用時 id 不變。"""
    return db.query(
        "UPDATE webhook_endpoints SET active = false, updated_at = now()"
        " WHERE caller_id = %s RETURNING *", (caller_id,), fetch="one", tx=tx)


def count_active(tx=None) -> int:
    row = db.query(
        "SELECT count(*) AS n FROM webhook_endpoints WHERE active",
        fetch="one", tx=tx)
    return int(row["n"]) if row else 0
