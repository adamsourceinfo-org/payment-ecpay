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


def test_reports_refund_api_available(client, monkeypatch):
    """綠界文件說測試環境沒有退款 API，實測是錯的 —— 兩邊都回報可用。"""
    monkeypatch.setattr(db, "db_status", _ok)
    assert client.get("/health").json()["ecpay"]["refund_api"] == "available"


def test_credit_check_code_absence_is_not_unhealthy(client, monkeypatch,
                                                    fake_settings):
    """商家檢查碼只有正式環境用得到（查詢信用卡單筆明細只有正式環境有），
    dev 沒有是正常的，不能因此讓健康檢查紅燈。"""
    monkeypatch.setattr(db, "db_status", _ok)
    fake_settings.credit_check_code = None
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ecpay"]["credit_check_code"] == "unset"

    fake_settings.credit_check_code = "97361824"
    assert client.get("/health").json()["ecpay"]["credit_check_code"] == "loaded"


def test_credit_check_code_value_never_appears_in_health(client, monkeypatch,
                                                         fake_settings):
    """跟 HashKey 一樣，只回報有沒有，不回報值。"""
    monkeypatch.setattr(db, "db_status", _ok)
    fake_settings.credit_check_code = "97361824"
    assert "97361824" not in client.get("/health").text
