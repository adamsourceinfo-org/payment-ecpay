"""示範商店。

重點只有兩個：**沒設 DEMO_CALLER_ID 就整組不存在**，
以及**它走的是跟真 caller 完全一樣的建單路徑**（不是自己重寫一份）。
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import demo


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def on(fake_settings):
    fake_settings.demo_caller_id = "demo-storefront"
    return fake_settings


# --- 開關 --------------------------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("get", "/demo"),
    ("post", "/demo/api/checkout"),
    ("get", "/demo/api/status?kind=order&id=x"),
    ("get", "/demo/api/feed"),
])
def test_沒設DEMO_CALLER_ID時整組回404(client, fake_settings, method, path):
    """prod 的 .cicd 刻意沒有那一行 —— 這條測試就是那個保證。

    ⚠️ 這幾支**不驗 API key**（沒有 key 可驗），所以「關得掉」是唯一的防線。
    """
    fake_settings.demo_caller_id = None
    kwargs = {"json": {}} if method == "post" else {}
    r = getattr(client, method)(path, **kwargs)
    assert r.status_code == 404


def test_開啟後首頁吐得出商店(client, on):
    r = client.get("/demo")
    assert r.status_code == 200
    assert "小樹文具" in r.text
    assert "text/html" in r.headers["content-type"]


# --- 建單走的是同一條路 -------------------------------------------------

def test_建單直接呼叫v1的函式而不是重寫一份(client, on, monkeypatch):
    """示範出來的東西要跟 caller 真的會遇到的一樣，所以不能有第二份建單邏輯。

    這裡攔的是 /v1 的那支函式本身 —— 攔得到就證明走的是同一條。
    """
    seen = {}

    def fake_create(body, request, caller):
        seen["reference_id"] = body.reference_id
        seen["amount"] = body.amount
        seen["choose_payment"] = body.choose_payment
        seen["return_url"] = body.return_url
        seen["caller_id"] = caller.caller_id
        seen["scopes"] = caller.scopes
        return {"id": "o-1", "merchant_trade_no": "OABC",
                "checkout_url": "https://svc/ecpay/checkout/tok"}

    monkeypatch.setattr(demo.orders_router, "create_order", fake_create)
    r = client.post("/demo/api/checkout",
                    json={"kind": "order", "amount": 100,
                          "item_name": "測試", "choose_payment": "ATM"})
    assert r.status_code == 200
    assert r.json()["checkout_url"] == "https://svc/ecpay/checkout/tok"
    assert seen["amount"] == 100 and seen["choose_payment"] == "ATM"
    assert seen["caller_id"] == "demo-storefront"
    # 導回自己，這樣使用者付完會回到示範商店
    assert seen["return_url"].endswith("/demo")


def test_每次結帳都用新的reference_id(client, on, monkeypatch):
    """不換的話第二次點下去會撞到冪等，回的是第一筆的 checkout_url ——
    而那個 token 已經用掉了，使用者會看到「這筆已經完成付款」。"""
    refs = []
    monkeypatch.setattr(
        demo.orders_router, "create_order",
        lambda body, request, caller: refs.append(body.reference_id) or {
            "id": "o", "merchant_trade_no": "O", "checkout_url": "u"})
    for _ in range(2):
        client.post("/demo/api/checkout", json={"kind": "order", "amount": 100})
    assert len(set(refs)) == 2


def test_訂閱走的是訂閱那支(client, on, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        demo.subs_router, "create_subscription",
        lambda body, request, caller: seen.update(
            period_type=body.period_type, amount=body.amount) or {
            "id": "s-1", "merchant_trade_no": "SABC", "checkout_url": "u"})
    r = client.post("/demo/api/checkout",
                    json={"kind": "subscription", "amount": 100,
                          "period_type": "Y", "frequency": 1})
    assert r.status_code == 200 and r.json()["kind"] == "subscription"
    assert seen == {"period_type": "Y", "amount": 100}


def test_kind只能是那兩個(client, on):
    r = client.post("/demo/api/checkout", json={"kind": "轉帳", "amount": 100})
    assert r.status_code == 400


# --- 隔離 --------------------------------------------------------------

def test_只查得到demo自己的東西(client, on, monkeypatch):
    """走的是同一套 store，所以 caller_id 隔離跟真 caller 一模一樣。"""
    asked = {}
    monkeypatch.setattr(demo.orders_store, "get",
                        lambda cid, oid: asked.update(caller_id=cid) or None)
    r = client.get("/demo/api/status?kind=order&id=別人的")
    assert r.status_code == 404               # 查不到就是 404，不是 403
    assert asked["caller_id"] == "demo-storefront"


def test_demo身分沒有webhooks_write(client, on, monkeypatch):
    """能建單、能看自己的東西，但**改不了推送端點** ——
    這幾支沒有 API key 可驗，所以權限只能靠這份寫死的清單。"""
    seen = {}
    monkeypatch.setattr(
        demo.orders_router, "create_order",
        lambda body, request, caller: seen.update(scopes=caller.scopes) or {
            "id": "o", "merchant_trade_no": "O", "checkout_url": "u"})
    client.post("/demo/api/checkout", json={"kind": "order", "amount": 100})
    assert "orders:write" in seen["scopes"]
    assert "webhooks:write" not in seen["scopes"]


# --- 導回之後用單號找回那一筆 -------------------------------------------

def test_導回時用單號解析回本地紀錄(client, on, monkeypatch):
    """綠界導回只給單號，而單號會換（送出過就不能再用）——
    所以一律透過 trade_attempts 解析，跟 /ecpay/return 同一條路。"""
    monkeypatch.setattr(demo.attempts_store, "resolve",
                        lambda tn: {"subject_kind": "order", "subject_id": "o-9"})
    monkeypatch.setattr(demo.orders_store, "get",
                        lambda cid, oid: {"id": oid, "reference_id": "r",
                                          "merchant_trade_no": "OOLD",
                                          "amount": 100, "currency": "TWD",
                                          "choose_payment": "Credit",
                                          "status": "paid", "refunded_amount": 0,
                                          "created_at": None})
    monkeypatch.setattr(demo.orders_store, "payment_info", lambda oid: None)
    r = client.get("/demo/api/status?trade_no=ONEW")
    assert r.status_code == 200
    d = r.json()
    assert d["kind"] == "order" and d["id"] == "o-9" and d["status"] == "paid"


def test_查不到的單號回404(client, on, monkeypatch):
    monkeypatch.setattr(demo.attempts_store, "resolve", lambda tn: None)
    assert client.get("/demo/api/status?trade_no=沒這個").status_code == 404


# --- 後台面板 ----------------------------------------------------------

def test_feed同時給事件與投遞(client, on, monkeypatch):
    """示範的重點就是這個對照：同一筆，一條是拉的、一條是推的。"""
    monkeypatch.setattr(demo.events_store, "list_after",
                        lambda cid, after, limit: [
                            {"id": 7, "event_type": "payment.return",
                             "subject_kind": "order", "subject_id": "o-1",
                             "payload": {"RtnCode": "1"}, "received_at": None}])
    monkeypatch.setattr(demo.deliveries_store, "list_for_caller",
                        lambda cid, limit=10: [
                            {"id": "d-1", "event_id": 7, "status": "delivered",
                             "attempts": 1, "last_status": 200,
                             "created_at": None}])
    d = client.get("/demo/api/feed").json()
    assert d["events"][0]["rtn_code"] == "1"
    assert d["next_cursor"] == 7
    assert d["deliveries"][0]["status"] == "delivered"


def test_沒有事件時游標不倒退(client, on, monkeypatch):
    monkeypatch.setattr(demo.events_store, "list_after", lambda cid, a, l: [])
    monkeypatch.setattr(demo.deliveries_store, "list_for_caller",
                        lambda cid, limit=10: [])
    assert client.get("/demo/api/feed?after=42").json()["next_cursor"] == 42


# --- 示範接收端（caller 那一側）---------------------------------------

def _post_hook(client, body: bytes, *, secret, t=None, tamper=False):
    from app.webhooks import signing
    import time as _t
    t = int(_t.time()) if t is None else t
    sig = signing.header(secret, t, body)
    if tamper:
        sig = sig[:-1] + ("0" if sig[-1] != "0" else "1")
    return client.post("/demo/hook", content=body,
                       headers={"X-Signature": sig,
                                "Content-Type": "application/json",
                                "X-Delivery-Attempt": "1"})


@pytest.fixture
def secret(on):
    from app.webhooks import signing
    demo._INBOX.clear()
    return signing.secret_for("demo-storefront")


def _event_body(eid=1234, etype="payment.return"):
    import json as _j
    return _j.dumps({"id": eid, "event_type": etype, "subject_kind": "order",
                     "subject_id": "o-1", "payload": {"RtnCode": "1"},
                     "received_at": None},
                    ensure_ascii=False, separators=(",", ":")).encode()


def test_接收端驗簽通過就回2xx並記進收件匣(client, secret):
    r = _post_hook(client, _event_body(), secret=secret)
    assert r.status_code == 200 and r.json()["received"] == 1234
    got = client.get("/demo/api/inbox").json()["items"][0]
    assert got["ok"] is True and got["event_type"] == "payment.return"


def test_簽章被改一個字就不收(client, secret):
    r = _post_hook(client, _event_body(), secret=secret, tamper=True)
    assert r.status_code == 400
    assert client.get("/demo/api/inbox").json()["items"][0]["ok"] is False


def test_時間戳太舊就拒_不是只看簽章對不對(client, secret):
    """防重放。簽章正確但時間戳超出容忍一樣要拒 ——
    不然一份側錄下來的推送可以無限期重播。"""
    import time as _t
    r = _post_hook(client, _event_body(), secret=secret,
                   t=int(_t.time()) - 400)
    assert r.status_code == 400
    assert "時間戳" in client.get("/demo/api/inbox").json()["items"][0]["why"]


def test_原始bytes_中文payload重新序列化就驗不過(client, secret):
    """這是整個介接最容易錯的一條：Express 預設會先 parse 掉 JSON，
    而重新 stringify 出來的字串跟原文不保證逐位元組相同 ——
    **只在有中文的 payload 上才發作**，而綠界的 RtnMsg 就是中文。"""
    import json as _j
    original = _j.dumps({"id": 1, "event_type": "payment.return",
                         "payload": {"RtnMsg": "付款成功"}},
                        ensure_ascii=False, separators=(",", ":")).encode()
    # 用原文簽，卻送「ASCII 跳脫過」的版本 —— 模擬接收端重新序列化
    reserialized = _j.dumps(_j.loads(original), ensure_ascii=True,
                            separators=(",", ":")).encode()
    assert reserialized != original

    from app.webhooks import signing
    import time as _t
    t = int(_t.time())
    r = client.post("/demo/hook", content=reserialized, headers={
        "X-Signature": f"t={t},v1={signing.signature(secret, t, original)}",
        "Content-Type": "application/json"})
    assert r.status_code == 400


def test_用body裡的id去重_不是用header(client, secret):
    """X-Event-Id 沒經過驗簽，拿它當去重鍵等於讓別人決定你收不收。"""
    body = _event_body(eid=555)
    assert _post_hook(client, body, secret=secret).json()["duplicate"] is False
    assert _post_hook(client, body, secret=secret).json()["duplicate"] is True


def test_ping在去重之前就處理掉(client, secret):
    """ping 的 id 固定是 0。照順序去重的話第二次會被自己擋掉，
    看起來像沒送到 —— 所以要先短路。"""
    import json as _j
    ping = _j.dumps({"id": 0, "event_type": "ping", "subject_kind": None,
                     "subject_id": None, "payload": {}, "received_at": None},
                    separators=(",", ":")).encode()
    for _ in range(2):
        r = _post_hook(client, ping, secret=secret)
        assert r.status_code == 200 and r.json()["duplicate"] is False
    assert all(x["ping"] for x in client.get("/demo/api/inbox").json()["items"])


def test_收件匣沒開demo時也是404(client, fake_settings):
    fake_settings.demo_caller_id = None
    assert client.get("/demo/api/inbox").status_code == 404
    assert client.post("/demo/hook", content=b"{}").status_code == 404
