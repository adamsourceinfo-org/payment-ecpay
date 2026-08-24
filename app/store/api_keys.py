"""api_keys 的 SQL。SQL 只住在 store/ 這一層。"""
from app import db


def lookup(key_hash: str):
    return db.query(
        "SELECT id, caller_id, scopes, active FROM api_keys WHERE key_hash = %s",
        (key_hash,), fetch="one")


def touch(key_id) -> None:
    db.query("UPDATE api_keys SET last_used_at = now() WHERE id = %s",
             (key_id,), fetch="none")
