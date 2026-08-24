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
            "closed": True, "checkout_token": None,
            "gwsr": None, "auth_code": None}
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
    """已關帳送 R（退刷），當天剛付款、還沒關帳送 N（放棄授權）。

    注意判斷依據是**關帳時間**而不只是 `closed` 欄位 —— 那個欄位一開始
    永遠是 false（先前沒有任何地方寫入它），只靠它就會永遠送 N。
    """
    from datetime import datetime, timedelta
    from app.refunds import TAIPEI
    fake_settings.do_action_available = True
    seen = {}
    monkeypatch.setattr(router_mod.ec, "do_action",
                        lambda **kw: seen.update(kw) or {"RtnCode": "1"})
    monkeypatch.setattr(router_mod.store, "set_closed", lambda *a: None)
    monkeypatch.setattr(router_mod.store, "add_refund",
                        lambda oid, amt, fully: _order(refunded_amount=amt,
                                                       status="refunded"))
    now = datetime.now(TAIPEI)

    rows["o1"] = _order(closed=True, paid_at=now - timedelta(days=2))
    assert client.post("/v1/orders/o1/refund", headers=H,
                       json={}).json()["refund_action"] == "R"

    # 剛剛才付款 —— 一定還沒到今天的關帳時段
    rows["o1"] = _order(closed=False, paid_at=now - timedelta(minutes=1))
    assert client.post("/v1/orders/o1/refund", headers=H,
                       json={}).json()["refund_action"] == "N"

    # 兩天前付款、closed 還沒被寫入 —— 這就是先前會壞掉的情境
    rows["o1"] = _order(closed=False, paid_at=now - timedelta(days=2))
    assert client.post("/v1/orders/o1/refund", headers=H,
                       json={}).json()["refund_action"] == "R"


def test_other_callers_order_is_404_not_403(client, rows):
    """403 會洩漏「該資源存在」。對呼叫者來說「不存在」與「不屬於你」不該有分別。"""
    r = client.get("/v1/orders/nope", headers=H)
    assert r.status_code == 404


def test_no_api_key_is_401(client):
    assert client.get("/v1/orders/o1").status_code == 401


def test_rejects_amount_below_method_floor(client):
    """各付款方式有金額下限，綠界**沒有公布**這些數字（實測：超商代碼 27、
    超商條碼 16、ATM 2、信用卡無下限）。不擋的話 caller 拿得到 checkout_url，
    但使用者到綠界只會看到「因交易金額低於下限」的死路，訂單永遠停在 created。"""
    r = client.post("/v1/orders", headers=H, json={
        "reference_id": "r-cvs", "amount": 13, "item_name": "x",
        "choose_payment": "CVS"})
    assert r.status_code == 400
    d = r.json()["detail"]
    assert d["field"] == "amount" and "27" in d["message"]


def test_allows_amount_at_the_floor(client, monkeypatch):
    """邊界值本身要放行 —— 實測 27 可以、26 不行。

    讓 get_by_reference 回既有訂單，走冪等那條路就能證明「通過了驗證」
    而不必真的接資料庫。
    """
    monkeypatch.setattr(router_mod.store, "get_by_reference",
                        lambda cid, ref: _order(choose_payment="CVS"))
    r = client.post("/v1/orders", headers=H, json={
        "reference_id": "r-cvs2", "amount": 27, "item_name": "x",
        "choose_payment": "CVS"})
    assert r.status_code == 200


def test_no_floor_configured_means_no_check(client, monkeypatch, fake_settings):
    """沒設定就不擋 —— 預設行為不變，也不會因為猜錯數字誤擋合法訂單。"""
    fake_settings.min_amounts = {}
    monkeypatch.setattr(router_mod.store, "get_by_reference",
                        lambda cid, ref: _order(choose_payment="CVS"))
    r = client.post("/v1/orders", headers=H, json={
        "reference_id": "r-any", "amount": 1, "item_name": "x",
        "choose_payment": "CVS"})
    assert r.status_code == 200


def test_refund_falls_back_to_the_other_action(client, rows, monkeypatch,
                                               fake_settings):
    """第一個動作被綠界拒絕時要自動改送另一個。

    這是先前的真缺陷：`orders.closed` 從來沒被寫入過，退款永遠送 N，
    而正式商店開著每日自動關帳 —— 隔天以後的退款一律會失敗。
    """
    from app.errors import ECPayError
    fake_settings.do_action_available = True
    tried = []

    def fake_do_action(**kw):
        tried.append(kw["action"])
        if len(tried) == 1:
            raise ECPayError("10200052", "訂單已關帳，請使用退刷")
        return {"RtnCode": "1"}

    monkeypatch.setattr(router_mod.ec, "do_action", fake_do_action)
    monkeypatch.setattr(router_mod.store, "set_closed", lambda *a: None)
    monkeypatch.setattr(router_mod.store, "add_refund",
                        lambda oid, amt, fully: _order(refunded_amount=amt,
                                                       status="refunded"))
    rows["o1"] = _order(closed=False, paid_at=None)
    r = client.post("/v1/orders/o1/refund", headers=H, json={})
    assert r.status_code == 200
    assert len(tried) == 2 and tried[0] != tried[1]
    assert r.json()["refund_action"] == tried[1]


def test_refund_reports_both_failures(client, rows, monkeypatch, fake_settings):
    """兩個都失敗時要把兩次的原文都帶回去 —— 只給一個，沒人查得出是哪一步錯。"""
    from app.errors import ECPayError
    fake_settings.do_action_available = True

    def always_fail(**kw):
        raise ECPayError("10200047", f"{kw['action']} 不允許")

    monkeypatch.setattr(router_mod.ec, "do_action", always_fail)
    rows["o1"] = _order(paid_at=None)
    r = client.post("/v1/orders/o1/refund", headers=H, json={})
    assert r.status_code == 502
    attempts = r.json()["detail"]["attempts"]
    assert len(attempts) == 2
    assert {a["action"] for a in attempts} == {"R", "N"}


def test_order_exposes_credit_authorisation_ids(client, rows):
    """信用卡的授權單號與授權碼要回給 caller —— 對帳與跟綠界客服查詢都要用。"""
    rows["o1"] = _order(gwsr="14563813", auth_code="R05013")
    d = client.get("/v1/orders/o1", headers=H).json()
    assert d["gwsr"] == "14563813" and d["auth_code"] == "R05013"


def test_refresh_never_zeroes_out_known_totals(client, rows, monkeypatch):
    """對帳讀不到欄位時要維持原狀，不能寫 0。

    實測踩過的：綠界的查詢回應被誤判成無結構字串，`TotalSuccessTimes`
    讀不到就被寫成 0，**把回呼存下來的正確數字蓋掉**。
    對帳把資料弄丟，比不對帳更糟。
    """
    from app.routers import subscriptions as sub_router
    saved = {}
    monkeypatch.setattr(sub_router.store, "get",
                        lambda cid, sid: {"id": "s1", "caller_id": "c1",
                                          "reference_id": "r", "merchant_trade_no": "S1",
                                          "period_amount": 5, "period_type": "M",
                                          "frequency": 1, "exec_times": 12,
                                          "status": "active", "total_success_times": 3,
                                          "total_success_amount": 15,
                                          "created_at": None, "checkout_token": None})
    monkeypatch.setattr(sub_router.store, "charges", lambda sid: [])
    monkeypatch.setattr(sub_router.store, "set_totals",
                        lambda sid, t, a: saved.update(times=t, amount=a))
    # 綠界只回了 RtnCode，沒有 TotalSuccessTimes
    monkeypatch.setattr(sub_router.ecsub, "query", lambda tn: {"RtnCode": "1"})

    d = client.get("/v1/subscriptions/s1?refresh=true", headers=H).json()
    assert saved == {}                       # 完全沒去改
    assert d["total_success_times"] == 3     # 原本的數字還在
