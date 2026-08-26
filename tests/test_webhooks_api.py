"""caller 面對的推送 API：註冊清單、投遞紀錄、redeliver、ping。"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import webhooks as router_mod
from app.store import api_keys

H = {"X-API-Key": "k"}
NOW = datetime(2026, 8, 26, 3, 14, 15, tzinfo=timezone.utc)

SCOPES = ["orders:read", "events:read", "webhooks:read", "webhooks:write"]


class FakeEndpoints:
    """一個 caller 一列 —— 就是 migration 裡那個唯一索引的行為。"""

    def __init__(self):
        self.rows = {}
        self.next_id = 1

    def upsert(self, caller_id, url, tx=None):
        row = self.rows.get(caller_id)
        if row:
            row.update(url=url, active=True, updated_at=NOW)
        else:
            row = {"id": f"ep-{self.next_id}", "caller_id": caller_id,
                   "url": url, "active": True, "updated_at": NOW}
            self.next_id += 1
            self.rows[caller_id] = row
        return dict(row)

    def get(self, caller_id, tx=None):
        row = self.rows.get(caller_id)
        return dict(row) if row else None

    def get_active(self, caller_id, tx=None):
        row = self.rows.get(caller_id)
        return dict(row) if row and row["active"] else None

    def deactivate(self, caller_id, tx=None):
        row = self.rows.get(caller_id)
        if not row:
            return None
        row["active"] = False
        return dict(row)


@pytest.fixture
def endpoints(monkeypatch):
    fe = FakeEndpoints()
    for name in ("upsert", "get", "get_active", "deactivate"):
        monkeypatch.setattr(router_mod.endpoints_store, name, getattr(fe, name))
    return fe


@pytest.fixture
def client(monkeypatch, endpoints):
    monkeypatch.setattr(api_keys, "lookup", lambda h: {
        "id": "k1", "caller_id": "c1", "active": True, "scopes": SCOPES})
    monkeypatch.setattr(api_keys, "touch", lambda i: None)
    return TestClient(app)


URL = "https://line-translate-bot-xxxx.a.run.app/pay/events"


# --- 註冊清單 ---------------------------------------------------------

def test_註冊回200且帶密鑰(client):
    r = client.put("/v1/webhook-endpoint", json={"url": URL}, headers=H)
    assert r.status_code == 200          # upsert，不是 201
    body = r.json()
    assert body["url"] == URL and body["active"] is True
    assert len(body["secret"]) == 64


def test_密鑰是推導的所以每次都給得出來(client):
    a = client.put("/v1/webhook-endpoint", json={"url": URL}, headers=H).json()
    b = client.get("/v1/webhook-endpoint", headers=H).json()
    assert a["secret"] == b["secret"]    # 不需要「只顯示一次」那套儀式


def test_重複註冊只有一列且id不變(client, endpoints):
    first = client.put("/v1/webhook-endpoint", json={"url": URL}, headers=H).json()
    other = "https://line-translate-bot-xxxx.a.run.app/v2/pay"
    second = client.put("/v1/webhook-endpoint", json={"url": other},
                        headers=H).json()
    assert len(endpoints.rows) == 1
    assert first["id"] == second["id"]   # 投遞紀錄的外鍵不會斷
    assert second["url"] == other


def test_停用是軟的_列還在_id不變(client, endpoints):
    created = client.put("/v1/webhook-endpoint", json={"url": URL},
                         headers=H).json()
    deleted = client.delete("/v1/webhook-endpoint", headers=H).json()
    assert deleted["active"] is False
    assert deleted["id"] == created["id"]
    assert len(endpoints.rows) == 1      # 沒有刪列

    again = client.put("/v1/webhook-endpoint", json={"url": URL},
                       headers=H).json()
    assert again["active"] is True and again["id"] == created["id"]


def test_沒註冊過查詢回404(client):
    assert client.get("/v1/webhook-endpoint", headers=H).status_code == 404


@pytest.mark.parametrize("bad", [
    "http://caller.example/hook",
    "https://169.254.169.254/computeMetadata/v1/",
    "https://10.0.0.1/hook",
    "https://metadata.google.internal/hook",
])
def test_危險的網址擋在進門處(client, bad):
    r = client.put("/v1/webhook-endpoint", json={"url": bad}, headers=H)
    assert r.status_code == 400
    assert r.json()["detail"]["field"] == "url"


def test_缺少scope回403(monkeypatch, endpoints):
    monkeypatch.setattr(api_keys, "lookup", lambda h: {
        "id": "k1", "caller_id": "c1", "active": True, "scopes": ["orders:read"]})
    monkeypatch.setattr(api_keys, "touch", lambda i: None)
    c = TestClient(app)
    assert c.put("/v1/webhook-endpoint", json={"url": URL},
                 headers=H).status_code == 403


def test_推送未設定時六支端點都回503(client, fake_settings):
    """沒有簽章密鑰就算不出 secret。回一個沒有 secret 的物件只會讓 caller
    拿著空字串去驗簽 —— 那比誠實回 503 難查得多。"""
    fake_settings.push_configured = False
    assert client.put("/v1/webhook-endpoint", json={"url": URL},
                      headers=H).status_code == 503
    assert client.get("/v1/webhook-endpoint", headers=H).status_code == 503
    assert client.delete("/v1/webhook-endpoint", headers=H).status_code == 503
    assert client.post("/v1/webhook-endpoint/test", headers=H).status_code == 503
    assert client.get("/v1/deliveries", headers=H).status_code == 503
    assert client.post("/v1/events/1/redeliver", headers=H).status_code == 503


# --- ping -------------------------------------------------------------

def test_ping走真佇列且不落地事件(client, endpoints, monkeypatch):
    """同步直送會跳過 Cloud Tasks、內部端點、X-Internal-Key、重試 ——
    而那四樣正好是最會壞的部分。"""
    client.put("/v1/webhook-endpoint", json={"url": URL}, headers=H)
    made = {}

    def fake_create(event_id, endpoint_id, caller_id, url, tx=None):
        made.update(event_id=event_id, endpoint_id=endpoint_id,
                    caller_id=caller_id, url=url)
        return {"id": "d-1", "caller_id": caller_id, "url": url}

    enqueued = []
    monkeypatch.setattr(router_mod.dispatch.deliveries_store, "create", fake_create)
    monkeypatch.setattr(router_mod.dispatch.deliveries_store, "get",
                        lambda did, tx=None: _delivery(id=did, event_id=None))
    monkeypatch.setattr(router_mod.dispatch.tasks, "enqueue_delivery",
                        lambda cid, url: enqueued.append((cid, url)))

    r = client.post("/v1/webhook-endpoint/test", headers=H)
    assert r.status_code == 202
    assert made["event_id"] is None          # events 表不會多出任何一列
    assert len(enqueued) == 1                # 走的是真佇列
    assert "/internal/deliveries/d-1" in enqueued[0][1]


def test_沒註冊端點就不能ping(client):
    assert client.post("/v1/webhook-endpoint/test", headers=H).status_code == 400


# --- deliveries / redeliver -------------------------------------------

def _delivery(**kw):
    base = {"id": "d-1", "event_id": 1234, "endpoint_id": "ep-1",
            "caller_id": "c1", "url": URL, "status": "failed", "attempts": 3,
            "last_status": 500, "last_error": "boom", "created_at": NOW,
            "updated_at": NOW, "delivered_at": None}
    base.update(kw)
    return base


def test_投遞紀錄只看得到自己的(client, monkeypatch):
    seen = {}

    def fake_list(caller_id, event_id=None, status=None, limit=100, tx=None):
        seen.update(caller_id=caller_id, event_id=event_id, status=status)
        return [_delivery()]

    monkeypatch.setattr(router_mod.deliveries_store, "list_for_caller", fake_list)
    r = client.get("/v1/deliveries?event_id=1234&status=failed", headers=H)
    assert r.status_code == 200
    assert seen == {"caller_id": "c1", "event_id": 1234, "status": "failed"}
    assert r.json()["items"][0]["last_status"] == 500


def test_redeliver別人的事件回404不是403(client, monkeypatch):
    """events.id 是全域 bigserial，所有 caller 共用同一個序號空間 ——
    不擋的話 caller 可以拿別人的 id 去試探。
    403 會洩漏「該資源存在」，所以回 404。"""
    monkeypatch.setattr(router_mod.events_store, "get",
                        lambda eid, tx=None: {"id": eid, "caller_id": "別人"})
    assert client.post("/v1/events/1234/redeliver", headers=H).status_code == 404


def test_redeliver對應不到caller的事件也回404(client, monkeypatch):
    """caller_id IS NULL 的事件對每個 caller 都不可見。"""
    monkeypatch.setattr(router_mod.events_store, "get",
                        lambda eid, tx=None: {"id": eid, "caller_id": None})
    assert client.post("/v1/events/1234/redeliver", headers=H).status_code == 404


def test_redeliver建新的一列而不是重置舊的(client, endpoints, monkeypatch):
    """GET /v1/deliveries?event_id= 因此看得到完整的投遞史。"""
    client.put("/v1/webhook-endpoint", json={"url": URL}, headers=H)
    monkeypatch.setattr(router_mod.events_store, "get",
                        lambda eid, tx=None: {"id": eid, "caller_id": "c1"})
    created = []
    monkeypatch.setattr(
        router_mod.dispatch.deliveries_store, "create",
        lambda ev, ep, cid, url, tx=None: created.append(ev)
        or {"id": "d-新", "caller_id": cid, "url": url})
    monkeypatch.setattr(router_mod.dispatch.deliveries_store, "get",
                        lambda did, tx=None: _delivery(id=did, status="pending",
                                                       attempts=0))
    monkeypatch.setattr(router_mod.dispatch.tasks, "enqueue_delivery",
                        lambda cid, url: None)

    r = client.post("/v1/events/1234/redeliver", headers=H)
    assert r.status_code == 202              # 排進去了不等於送到了
    assert created == [1234]
    assert r.json()["status"] == "pending" and r.json()["attempts"] == 0
