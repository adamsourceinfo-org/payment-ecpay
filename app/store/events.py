from app import db


def record(dedupe_key, event_type, caller_id, subject_kind, subject_id,
           payload_json: str, tx=None):
    """回新事件的 id；重複回 None（視為 no-op）。

    dedupe_key 是自己造的 —— 綠界沒有全域 event id。

    ⚠️ 回 None 有**兩個**意思，呼叫端必須分辨得出來：
    綠界重送，或者這一筆已經被 /ecpay/order-result 那條路處理掉了
    （見 app/routers/callbacks.py 的雙入口）。兩種情況下本地狀態都已經正確，
    但推送可能還沒有人排 —— 所以 None 不可以只是早退。
    """
    row = db.query(
        "INSERT INTO events (dedupe_key, event_type, caller_id,"
        " subject_kind, subject_id, payload)"
        " VALUES (%s,%s,%s,%s,%s, %s::jsonb)"
        " ON CONFLICT (dedupe_key) DO NOTHING RETURNING id",
        (dedupe_key, event_type, caller_id, subject_kind, subject_id,
         payload_json), fetch="one", tx=tx)
    return row["id"] if row else None


def get_by_dedupe_key(dedupe_key: str, tx=None):
    """回 {"id", "caller_id"} 或 None。

    給「record() 回了 None，但我還是要知道那筆事件的 id」用 ——
    也就是要替它補一次推送的時候。
    """
    return db.query(
        "SELECT id, caller_id FROM events WHERE dedupe_key = %s",
        (dedupe_key,), fetch="one", tx=tx)


def get(event_id: int, tx=None):
    return db.query(
        "SELECT id, event_type, subject_kind, subject_id, payload, received_at,"
        " caller_id FROM events WHERE id = %s", (event_id,), fetch="one", tx=tx)


def list_after(caller_id: str, after: int, limit: int):
    # caller_id IS NULL 的事件永遠不匹配任何 caller —— 這正是要的效果
    return db.query(
        "SELECT id, dedupe_key, event_type, subject_kind, subject_id,"
        " payload, received_at FROM events"
        " WHERE caller_id = %s AND id > %s ORDER BY id LIMIT %s",
        (caller_id, after, limit))
