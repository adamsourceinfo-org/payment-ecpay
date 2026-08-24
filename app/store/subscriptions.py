from app import db


def get_by_reference(caller_id, reference_id):
    return db.query(
        "SELECT * FROM subscriptions WHERE caller_id = %s AND reference_id = %s",
        (caller_id, reference_id), fetch="one")


def get(caller_id, sub_id):
    return db.query(
        "SELECT * FROM subscriptions WHERE caller_id = %s AND id = %s",
        (caller_id, sub_id), fetch="one")


def get_by_trade_no(merchant_trade_no):
    """回呼用。**這是分辨訂閱首期與一次性付款的唯一方法** ——
    綠界首期回呼的欄位跟一次性付款一模一樣。"""
    return db.query(
        "SELECT * FROM subscriptions WHERE merchant_trade_no = %s",
        (merchant_trade_no,), fetch="one")


def get_by_checkout_token(token):
    return db.query("SELECT * FROM subscriptions WHERE checkout_token = %s",
                    (token,), fetch="one")


def create(*, caller_id, reference_id, merchant_trade_no, period_amount,
           period_type, frequency, exec_times, status, checkout_token,
           return_url, checkout_fields_json):
    return db.query(
        "INSERT INTO subscriptions (caller_id, reference_id, merchant_trade_no,"
        " period_amount, period_type, frequency, exec_times, status,"
        " checkout_token, return_url, checkout_fields)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING *",
        (caller_id, reference_id, merchant_trade_no, period_amount, period_type,
         frequency, exec_times, status, checkout_token, return_url,
         checkout_fields_json), fetch="one")


def mark_active(sub_id, ecpay_trade_no):
    """首期授權成功。綠界的規則是首期失敗整張單就不進排程，
    所以首期成功等於訂閱真的開始了。"""
    return db.query(
        "UPDATE subscriptions SET status = 'active',"
        " ecpay_trade_no = COALESCE(%s, ecpay_trade_no),"
        " first_charged_at = COALESCE(first_charged_at, now()),"
        " used_checkout_token = COALESCE(checkout_token, used_checkout_token),"
        " checkout_token = NULL, updated_at = now()"
        " WHERE id = %s RETURNING *", (ecpay_trade_no, sub_id), fetch="one")


def set_status(sub_id, status, cancelled=False):
    return db.query(
        "UPDATE subscriptions SET status = %s, updated_at = now(),"
        " cancelled_at = CASE WHEN %s THEN COALESCE(cancelled_at, now())"
        "                     ELSE cancelled_at END"
        " WHERE id = %s RETURNING *", (status, cancelled, sub_id), fetch="one")


def set_totals(sub_id, times: int, amount: int):
    return db.query(
        "UPDATE subscriptions SET total_success_times = %s,"
        " total_success_amount = %s, updated_at = now()"
        " WHERE id = %s RETURNING *", (times, amount, sub_id), fetch="one")


def record_charge(sub_id, *, gwsr, amount, rtn_code, auth_code, process_date):
    """回新的扣款 id；綠界重送造成的重複回 None。gwsr 是天然的去重鍵。"""
    row = db.query(
        "INSERT INTO subscription_charges (subscription_id, gwsr, amount,"
        " rtn_code, auth_code, process_date) VALUES (%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (gwsr) DO NOTHING RETURNING id",
        (sub_id, gwsr, amount, rtn_code, auth_code, process_date), fetch="one")
    return row["id"] if row else None


def charges(sub_id):
    return db.query(
        "SELECT gwsr, amount, rtn_code, auth_code, process_date, created_at"
        " FROM subscription_charges WHERE subscription_id = %s ORDER BY id",
        (sub_id,))


def list_(caller_id, status=None, limit=50, offset=0):
    if status:
        return db.query(
            "SELECT * FROM subscriptions WHERE caller_id = %s AND status = %s"
            " ORDER BY created_at DESC LIMIT %s OFFSET %s",
            (caller_id, status, limit, offset))
    return db.query(
        "SELECT * FROM subscriptions WHERE caller_id = %s"
        " ORDER BY created_at DESC LIMIT %s OFFSET %s",
        (caller_id, limit, offset))


def rotate_trade_no(sub_id, merchant_trade_no: str, fields_json: str):
    return db.query(
        "UPDATE subscriptions SET merchant_trade_no = %s,"
        " checkout_fields = %s::jsonb, updated_at = now()"
        " WHERE id = %s RETURNING *",
        (merchant_trade_no, fields_json, sub_id), fetch="one")


def get_by_id(sub_id):
    """不帶 caller_id —— 只給回呼路徑用。"""
    return db.query("SELECT * FROM subscriptions WHERE id = %s", (sub_id,),
                    fetch="one")


def get_by_used_token(token):
    """已經用掉的付款連結。只為了讓「已付款」與「連結不存在」分得出來。"""
    return db.query("SELECT id FROM subscriptions WHERE used_checkout_token = %s",
                    (token,), fetch="one")
