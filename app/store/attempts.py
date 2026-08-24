"""每一次送去綠界的嘗試。

存在的理由：綠界的 MerchantTradeNo 送出過就不能再用（10300028），
所以「回付款頁再付一次」必須換新單號 —— 但舊單號的回呼還是可能進來，
於是回呼一律透過這張表找回本地紀錄，而不是直接查 orders.merchant_trade_no。
"""
from app import db


def record(merchant_trade_no: str, subject_kind: str, subject_id) -> None:
    db.query(
        "INSERT INTO trade_attempts (merchant_trade_no, subject_kind, subject_id)"
        " VALUES (%s,%s,%s) ON CONFLICT (merchant_trade_no) DO NOTHING",
        (merchant_trade_no, subject_kind, subject_id), fetch="none")


def resolve(merchant_trade_no: str):
    """回 {"subject_kind", "subject_id"} 或 None。"""
    return db.query(
        "SELECT subject_kind, subject_id FROM trade_attempts"
        " WHERE merchant_trade_no = %s", (merchant_trade_no,), fetch="one")
