"""API key 是這個服務唯一的一道門（服務對公網開放，綠界回呼必須打得到）。"""
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth import Caller, hash_key, require
from app.store import api_keys


@pytest.fixture
def client(monkeypatch):
    rows = {hash_key("good"): {"id": "k1", "caller_id": "c1",
                               "scopes": ["orders:read"], "active": True},
            hash_key("disabled"): {"id": "k2", "caller_id": "c2",
                                   "scopes": ["orders:read"], "active": False}}
    monkeypatch.setattr(api_keys, "lookup", lambda h: rows.get(h))
    monkeypatch.setattr(api_keys, "touch", lambda i: None)

    app = FastAPI()

    @app.get("/read")
    def read(c: Caller = Depends(require("orders:read"))):
        return {"caller": c.caller_id}

    @app.get("/write")
    def write(c: Caller = Depends(require("orders:write"))):
        return {"caller": c.caller_id}

    return TestClient(app)


def test_valid_key(client):
    r = client.get("/read", headers={"X-API-Key": "good"})
    assert r.status_code == 200 and r.json()["caller"] == "c1"


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"},
                                     {"X-API-Key": "disabled"}])
def test_all_failures_look_the_same(client, headers):
    """沒帶、錯的、停用的 —— 一律同一個 401，不幫攻擊者縮小範圍。"""
    r = client.get("/read", headers=headers)
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid api key"


def test_scope_enforced(client):
    r = client.get("/write", headers={"X-API-Key": "good"})
    assert r.status_code == 403


def test_key_is_never_stored_in_plaintext():
    """DB 裡只有 sha256。這條測試存在的意義是防止有人「為了好查」改回明文。"""
    h = hash_key("good")
    assert h != "good" and len(h) == 64 and int(h, 16) >= 0
