from app import db


def record(dedupe_key, event_type, caller_id, subject_kind, subject_id,
           payload_json: str):
    """回新事件的 id；綠界重送造成的重複回 None（視為 no-op）。

    dedupe_key 是自己造的 —— 綠界沒有全域 event id。
    """
    row = db.query(
        "INSERT INTO events (dedupe_key, event_type, caller_id,"
        " subject_kind, subject_id, payload)"
        " VALUES (%s,%s,%s,%s,%s, %s::jsonb)"
        " ON CONFLICT (dedupe_key) DO NOTHING RETURNING id",
        (dedupe_key, event_type, caller_id, subject_kind, subject_id,
         payload_json), fetch="one")
    return row["id"] if row else None


def list_after(caller_id: str, after: int, limit: int):
    # caller_id IS NULL 的事件永遠不匹配任何 caller —— 這正是要的效果
    return db.query(
        "SELECT id, dedupe_key, event_type, subject_kind, subject_id,"
        " payload, received_at FROM events"
        " WHERE caller_id = %s AND id > %s ORDER BY id LIMIT %s",
        (caller_id, after, limit))
