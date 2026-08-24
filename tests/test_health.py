"""健康檢查壞掉時必須回 503 —— ci 的 smoke 只看狀態碼、完全不看 body。
回 200 但內容寫著「db 掛了」對 CI 來說是綠燈，那這個檢查什麼都沒證明。"""
import pytest
from fastapi.testclient import TestClient

from app import db
from app.routers import health


@pytest.fixture
def client(monkeypatch):
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(health.router)
    return TestClient(app)


def _ok():
    return {"configured": True, "ok": True, "instance": "p:r:i",
            "server_user": "run-runtime@p.iam", "database": "payment_ecpay"}


def test_healthy_is_200(client, monkeypatch):
    monkeypatch.setattr(db, "db_status", _ok)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["db"]["server_user"] == "run-runtime@p.iam"


def test_db_down_is_503(client, monkeypatch):
    monkeypatch.setattr(db, "db_status",
                        lambda: {"configured": True, "ok": False,
                                 "error": "boom"})
    assert client.get("/health").status_code == 503


def test_never_leaks_credentials(client, monkeypatch):
    """HashKey / HashIV 絕對不能出現在健康檢查裡。"""
    monkeypatch.setattr(db, "db_status", _ok)
    body = client.get("/health").text
    assert "pwFHCqoQZGmho4w6" not in body and "EkRm7iFT261dpevs" not in body
    assert "loaded" in body


def test_reports_refund_api_unavailable_on_stage(client, monkeypatch):
    """退款 API 在測試環境不存在。這不是故障，但要說出來。"""
    monkeypatch.setattr(db, "db_status", _ok)
    assert client.get("/health").json()["ecpay"]["refund_api"] == "stage-unavailable"
