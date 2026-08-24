"""caller 端點。重點在**退款的各種拒絕分支** ——
退款的成功路徑在 dev 上驗不到（綠界測試環境沒有 DoAction），
所以這些 400 分支是唯一能自動化驗證的部分，必須寫足。"""
import pytest
from fastapi.testclient import TestClient

from app.auth import Caller
from app.main import app
from app.routers import orders as router_mod
from app.store import api_keys


@pytest.fixture
def rows():
    return {}


@pytest.fixture
def client(monkeypatch, rows):
    monkeypatch.setattr(api_keys, "lookup", lambda h: {
        "id": "k1", "caller_id": "c1", "active": True,
        "scopes": ["orders:read", "orders:write", "subscriptions:read",
                   "subscriptions:write", "events:read"]})
    monkeypatch.setattr(api_keys, "touch", lambda i: None)
    monkeypatch.setattr(router_mod.store, "get",
                        lambda cid, oid: rows.get(oid))
    monkeypatch.setattr(router_mod.store, "payment_info", lambda oid: None)
    return TestClient(app)


H = {"X-API-Key": "k"}


def _order(**kw):
    base = {"id": "o1", "reference_id": "r1", "merchant_trade_no": "OABC",
            "ecpay_trade_no": "2608241159001234", "amount": 100,
            "currency": "TWD", "choose_payment": "Credit",
            "payment_type": "Credit_CreditCard", "status": "paid",
            "refunded_amount": 0, "paid_at": None, "created_at": None,
            "closed": True, "checkout_token": None}
    base.update(kw)
    return base


def test_create_validates_before_touching_db(client):
    """金額驗證發生在碰資料庫之前 —— 不合法的請求不該留下半筆訂單，
    也不該浪費一次綠界往返。這條測試沒有接 DB，能過就證明順序是對的。"""
    r = client.post("/v1/orders", headers=H, json={
        "reference_id": "r1", "amount": 0, "item_name": "x",
        "choose_payment": "Credit"})
    assert r.status_code == 400
    assert r.json()["detail"]["field"] == "amount"


def test_create_rejects_non_integer_amount_at_schema(client):
    """pydantic 先擋型別；小數在 schema 層就進不來。"""
    r = client.post("/v1/orders", headers=H, json={
        "reference_id": "r1", "amount": 10.5, "item_name": "x"})
    assert r.status_code == 422


def test_create_rejects_payment_not_enabled_in_this_env(client):
    """每個環境開通的付款方式不同。不該把沒開通的送去綠界等它回看不懂的錯。"""
    r = client.post("/v1/orders", headers=H, json={
        "reference_id": "r1", "amount": 100, "item_name": "x",
        "choose_payment": "TWQR"})
    assert r.status_code == 400
    assert r.json()["detail"]["field"] == "choose_payment"


def test_refund_requires_paid_order(client, rows):
    rows["o1"] = _order(status="created")
    r = client.post("/v1/orders/o1/refund", headers=H, json={})
    assert r.status_code == 400 and r.json()["detail"]["field"] == "status"


def test_refund_rejects_non_credit_payment(client, rows):
    """ATM／超商／WebATM 沒有退款 API，綠界要走人工流程。
    在這裡明說，比讓 caller 收到一個看不懂的上游錯誤好。"""
    rows["o1"] = _order(payment_type="ATM_TAISHIN", choose_payment="ATM")
    r = client.post("/v1/orders/o1/refund", headers=H, json={})
    assert r.status_code == 400
    assert r.json()["detail"]["field"] == "payment_type"


def test_refund_rejects_over_remaining(client, rows):
    rows["o1"] = _order(refunded_amount=80)
    r = client.post("/v1/orders/o1/refund", headers=H, json={"amount": 50})
    assert r.status_code == 400
    assert "只剩 20" in r.json()["detail"]["message"]


def test_refund_blocked_on_stage_with_explanation(client, rows):
    """綠界測試環境沒有 DoAction。與其送出去等一個難懂的失敗，不如明說。"""
    rows["o1"] = _order()
    r = client.post("/v1/orders/o1/refund", headers=H, json={})
    assert r.status_code == 400
    d = r.json()["detail"]
    assert d["field"] == "environment" and "測試環境" in d["message"]


def test_refund_picks_action_by_closed_state(client, rows, monkeypatch,
                                              fake_settings):
    """已關帳走 R（退刷），未關帳走 N（放棄授權）。
    綠界每日 20:15~20:30 自動關帳，所以當天付款的通常還沒關帳。"""
    fake_settings.do_action_available = True
    seen = {}
    monkeypatch.setattr(router_mod.ec, "do_action",
                        lambda **kw: seen.update(kw) or {"RtnCode": "1"})
    monkeypatch.setattr(router_mod.store, "add_refund",
                        lambda oid, amt, fully: _order(refunded_amount=amt,
                                                       status="refunded"))
    rows["o1"] = _order(closed=True)
    assert client.post("/v1/orders/o1/refund", headers=H,
                       json={}).json()["refund_action"] == "R"

    rows["o1"] = _order(closed=False)
    assert client.post("/v1/orders/o1/refund", headers=H,
                       json={}).json()["refund_action"] == "N"


def test_other_callers_order_is_404_not_403(client, rows):
    """403 會洩漏「該資源存在」。對呼叫者來說「不存在」與「不屬於你」不該有分別。"""
    r = client.get("/v1/orders/nope", headers=H)
    assert r.status_code == 404


def test_no_api_key_is_401(client):
    assert client.get("/v1/orders/o1").status_code == 401
