"""orders 的 SQL。每個 caller 端查詢都強制帶 caller_id ——
隔離是這一層的預設值，不是呼叫端的責任。"""
from app import db


def get_by_reference(caller_id: str, reference_id: str):
    return db.query(
        "SELECT * FROM orders WHERE caller_id = %s AND reference_id = %s",
        (caller_id, reference_id), fetch="one")


def get(caller_id: str, order_id: str):
    return db.query("SELECT * FROM orders WHERE caller_id = %s AND id = %s",
                    (caller_id, order_id), fetch="one")


def get_by_trade_no(merchant_trade_no: str):
    """回呼用：這時還不知道是哪個 caller，由這筆資料告訴我們。"""
    return db.query("SELECT * FROM orders WHERE merchant_trade_no = %s",
                    (merchant_trade_no,), fetch="one")


def get_by_checkout_token(token: str):
    """導轉頁用：不驗 API key，靠這個高熵 token。"""
    return db.query("SELECT * FROM orders WHERE checkout_token = %s",
                    (token,), fetch="one")


def create(*, caller_id, reference_id, merchant_trade_no, amount,
           choose_payment, status, checkout_token, return_url,
           checkout_fields_json):
    return db.query(
        "INSERT INTO orders (caller_id, reference_id, merchant_trade_no,"
        " amount, choose_payment, status, checkout_token, return_url,"
        " checkout_fields)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING *",
        (caller_id, reference_id, merchant_trade_no, amount, choose_payment,
         status, checkout_token, return_url, checkout_fields_json),
        fetch="one")


def mark_paid(order_id, ecpay_trade_no: str, payment_type: str):
    return db.query(
        "UPDATE orders SET status = 'paid', ecpay_trade_no = %s,"
        " payment_type = %s, paid_at = COALESCE(paid_at, now()),"
        " checkout_token = NULL,"            # 付完就讓導轉頁失效
        " updated_at = now() WHERE id = %s RETURNING *",
        (ecpay_trade_no, payment_type, order_id), fetch="one")


def set_status(order_id, status: str, ecpay_trade_no: str = None):
    return db.query(
        "UPDATE orders SET status = %s,"
        " ecpay_trade_no = COALESCE(%s, ecpay_trade_no),"
        " updated_at = now() WHERE id = %s RETURNING *",
        (status, ecpay_trade_no, order_id), fetch="one")


def add_refund(order_id, amount: int, fully: bool):
    return db.query(
        "UPDATE orders SET refunded_amount = refunded_amount + %s,"
        " status = CASE WHEN %s THEN 'refunded' ELSE 'partially_refunded' END,"
        " updated_at = now() WHERE id = %s RETURNING *",
        (amount, fully, order_id), fetch="one")


def set_closed(order_id, closed: bool):
    return db.query(
        "UPDATE orders SET closed = %s, updated_at = now()"
        " WHERE id = %s RETURNING *", (closed, order_id), fetch="one")


def save_payment_info(order_id, info: dict, raw_json: str):
    db.query(
        "INSERT INTO order_payment_info (order_id, bank_code, v_account,"
        " payment_no, expire_date, raw) VALUES (%s,%s,%s,%s,%s,%s::jsonb)"
        " ON CONFLICT (order_id) DO UPDATE SET bank_code = EXCLUDED.bank_code,"
        " v_account = EXCLUDED.v_account, payment_no = EXCLUDED.payment_no,"
        " expire_date = EXCLUDED.expire_date, raw = EXCLUDED.raw",
        (order_id, info.get("BankCode"), info.get("vAccount"),
         info.get("PaymentNo"), info.get("ExpireDate"), raw_json), fetch="none")


def payment_info(order_id):
    return db.query(
        "SELECT bank_code, v_account, payment_no, expire_date"
        " FROM order_payment_info WHERE order_id = %s", (order_id,), fetch="one")


def list_(caller_id: str, status=None, limit: int = 50, offset: int = 0):
    if status:
        return db.query(
            "SELECT * FROM orders WHERE caller_id = %s AND status = %s"
            " ORDER BY created_at DESC LIMIT %s OFFSET %s",
            (caller_id, status, limit, offset))
    return db.query(
        "SELECT * FROM orders WHERE caller_id = %s"
        " ORDER BY created_at DESC LIMIT %s OFFSET %s",
        (caller_id, limit, offset))


def rotate_trade_no(order_id, merchant_trade_no: str, fields_json: str):
    """換一個新的綠界單號並重簽表單。舊單號留在 trade_attempts 裡仍可解析。"""
    return db.query(
        "UPDATE orders SET merchant_trade_no = %s, checkout_fields = %s::jsonb,"
        " updated_at = now() WHERE id = %s RETURNING *",
        (merchant_trade_no, fields_json, order_id), fetch="one")


def get_by_id(order_id):
    """不帶 caller_id —— 只給回呼路徑用（那時還不知道是誰的）。
    caller 端的查詢一律走 get()，那支強制帶 caller_id。"""
    return db.query("SELECT * FROM orders WHERE id = %s", (order_id,), fetch="one")
