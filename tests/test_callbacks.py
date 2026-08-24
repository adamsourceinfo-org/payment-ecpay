"""綠界回呼。這是整個服務最需要小心的地方 ——
驗簽、去重、以及「首期回呼分辨不出是不是訂閱」那個陷阱。"""
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import callbacks
from tests.conftest import sign


class FakeStore:
    """就地記錄呼叫，讓測試能斷言「有沒有做對事」而不只是「有沒有回 200」。"""

    def __init__(self):
        self.orders = {}
        self.subs = {}
        self.events = {}
        self.attempts = {}
        self.calls = []

    # --- events
    def record(self, dedupe_key, event_type, caller_id, kind, subject_id, raw):
        if dedupe_key in self.events:
            return None                     # 綠界重送
        self.events[dedupe_key] = (event_type, caller_id, kind, subject_id)
        return len(self.events)


@pytest.fixture
def store(monkeypatch):
    fs = FakeStore()
    monkeypatch.setattr(callbacks.events_store, "record", fs.record)

    # trade_attempts：單號 → (kind, id)。回呼一律先走這裡，
    # 因為訂單可能換過單號（綠界的單號送出過就不能再用）。
    monkeypatch.setattr(callbacks.attempts_store, "record",
                        lambda tn, kind, sid, _fs=fs: _fs.attempts.update(
                            {tn: (kind, str(sid))}))

    def _resolve(tn, _fs=fs):
        hit = _fs.attempts.get(tn)
        return {"subject_kind": hit[0], "subject_id": hit[1]} if hit else None
    monkeypatch.setattr(callbacks.attempts_store, "resolve", _resolve)

    def _by_id(store_dict):
        def inner(sid, _d=store_dict):
            return next((v for v in _d.values() if str(v["id"]) == str(sid)), None)
        return inner
    monkeypatch.setattr(callbacks.orders_store, "get_by_id", _by_id(fs.orders))
    monkeypatch.setattr(callbacks.subs_store, "get_by_id", _by_id(fs.subs))

    for name in ("get_by_trade_no",):
        monkeypatch.setattr(callbacks.orders_store, name,
                            lambda tn, _fs=fs: _fs.orders.get(tn))
        monkeypatch.setattr(callbacks.subs_store, name,
                            lambda tn, _fs=fs: _fs.subs.get(tn))
    for mod, label in ((callbacks.orders_store, "order"),
                       (callbacks.subs_store, "sub")):
        for fn in ("mark_paid", "set_status", "mark_active", "record_charge",
                   "set_totals", "save_payment_info"):
            if hasattr(mod, fn):
                monkeypatch.setattr(
                    mod, fn,
                    lambda *a, _l=label, _f=fn, _fs=fs, **k:
                        _fs.calls.append((_l, _f, a, k)))
    return fs


@pytest.fixture
def client():
    return TestClient(app)


def _order_return(trade_no="O123", rtn="1", amount="100"):
    return sign({
        "MerchantID": "3002607", "MerchantTradeNo": trade_no,
        "PaymentDate": "2026/08/24 12:00:00", "PaymentType": "Credit_CreditCard",
        "RtnCode": rtn, "RtnMsg": "Succeeded" if rtn == "1" else "Failed",
        "SimulatePaid": "0", "TradeAmt": amount, "TradeDate": "2026/08/24 11:59:00",
        "TradeNo": "2608241159001234",
    })


def test_return_rejects_bad_signature(client, store):
    """驗簽不過就 400 且**不落地**。"""
    bad = _order_return()
    bad["TradeAmt"] = "1"                   # 改金額，簽章就不合
    r = client.post("/ecpay/return", data=bad)
    assert r.status_code == 400
    assert store.events == {}


def test_return_rejects_missing_signature(client, store):
    r = client.post("/ecpay/return", data={"MerchantTradeNo": "O1", "RtnCode": "1"})
    assert r.status_code == 400


def test_return_acks_with_exact_string(client, store):
    """必須逐字回 `1|OK`。回別的綠界會當成沒收到，隔 5~15 分重送四次。"""
    store.orders["O123"] = {"id": "oid-1", "caller_id": "c1", "status": "created"}
    r = client.post("/ecpay/return", data=_order_return())
    assert r.status_code == 200
    assert r.text == "1|OK"


def test_return_marks_order_paid(client, store):
    store.orders["O123"] = {"id": "oid-1", "caller_id": "c1", "status": "created"}
    client.post("/ecpay/return", data=_order_return())
    assert ("order", "mark_paid") in [(c[0], c[1]) for c in store.calls]


def test_return_duplicate_is_noop_but_still_acks(client, store):
    """重送時什麼都不做，但**還是要回 1|OK** —— 不回它會一直重送。"""
    store.orders["O123"] = {"id": "oid-1", "caller_id": "c1", "status": "created"}
    body = _order_return()
    first = client.post("/ecpay/return", data=body)
    store.calls.clear()
    second = client.post("/ecpay/return", data=body)
    assert first.text == second.text == "1|OK"
    assert store.calls == []                # 第二次沒有再動任何資料


def test_first_period_charge_looks_identical_to_one_off(client, store):
    """**這個介接最大的陷阱。**

    定期定額首期的回呼與一次性付款逐欄位相同 —— PeriodType / Frequency /
    ExecTimes / TotalSuccessTimes 一個都沒有。所以只能靠 MerchantTradeNo
    查自己的表；同一份 payload，查到訂閱就走訂閱、查到訂單就走訂單。
    """
    payload = _order_return(trade_no="S999")
    assert "PeriodType" not in payload      # 釘住這個事實
    assert "TotalSuccessTimes" not in payload

    store.subs["S999"] = {"id": "sid-1", "caller_id": "c1", "period_amount": 100}
    r = client.post("/ecpay/return", data=payload)
    assert r.text == "1|OK"
    done = [(c[0], c[1]) for c in store.calls]
    assert ("sub", "mark_active") in done
    assert ("order", "mark_paid") not in done


def test_return_failure_does_not_mark_paid(client, store):
    store.orders["O123"] = {"id": "oid-1", "caller_id": "c1", "status": "created"}
    client.post("/ecpay/return", data=_order_return(rtn="10100058"))
    done = [(c[0], c[1]) for c in store.calls]
    assert ("order", "mark_paid") not in done
    assert ("order", "set_status") in done


def test_unknown_trade_no_still_acks_and_lands(client, store):
    """測試商店全球共用，會收到陌生人的回呼。落地但 caller_id 為 NULL。"""
    r = client.post("/ecpay/return", data=_order_return(trade_no="ZZZ"))
    assert r.text == "1|OK"
    (_, caller_id, kind, _), = store.events.values()
    assert caller_id is None and kind is None


def _period_return(trade_no="S999", gwsr="G1", rtn="1"):
    return sign({
        "MerchantID": "3002607", "MerchantTradeNo": trade_no,
        "RtnCode": rtn, "RtnMsg": "paid", "gwsr": gwsr, "amount": "100",
        "process_date": "2026/09/24 12:00:00", "auth_code": "R05013",
        "PeriodType": "M", "Frequency": "1", "ExecTimes": "99",
        "TotalSuccessTimes": "2", "TotalSuccessAmount": "200",
        "TradeNo": "2609241200001234",
    })


def test_period_return_has_the_fields_first_charge_lacks(client, store):
    """反過來釘住：續期回呼**才有** Period 系列欄位。"""
    p = _period_return()
    assert p["PeriodType"] == "M" and p["TotalSuccessTimes"] == "2"


def test_period_return_dedupes_on_gwsr(client, store):
    store.subs["S999"] = {"id": "sid-1", "caller_id": "c1",
                          "total_success_amount": 100}
    body = _period_return()
    assert client.post("/ecpay/period-return", data=body).text == "1|OK"
    store.calls.clear()
    assert client.post("/ecpay/period-return", data=body).text == "1|OK"
    assert store.calls == []


def test_period_charge_failure_does_not_cancel_subscription(client, store):
    """單次扣款失敗不等於訂閱結束 —— 綠界會繼續嘗試，連續六期失敗才自動終止。"""
    store.subs["S999"] = {"id": "sid-1", "caller_id": "c1",
                          "total_success_amount": 100}
    client.post("/ecpay/period-return",
                data=_period_return(gwsr="G2", rtn="10100058"))
    statuses = [c for c in store.calls if c[1] == "set_status"]
    assert statuses == []


def test_payment_info_lands_and_sets_awaiting(client, store):
    """ATM 取號：這時還沒付款，只有虛擬帳號與繳費期限。"""
    store.orders["O555"] = {"id": "oid-5", "caller_id": "c1", "status": "created"}
    body = sign({
        "MerchantID": "3002607", "MerchantTradeNo": "O555", "RtnCode": "2",
        "RtnMsg": "Get vAccount Succeeded", "TradeNo": "2608241200005555",
        "TradeAmt": "100", "BankCode": "808", "vAccount": "9103522175887271",
        "ExpireDate": "2026/08/31", "PaymentType": "ATM_TAISHIN",
    })
    r = client.post("/ecpay/payment-info", data=body)
    assert r.text == "1|OK"
    done = [(c[0], c[1]) for c in store.calls]
    assert ("order", "save_payment_info") in done
    assert ("order", "set_status") in done


def test_order_result_does_not_change_state(client, store):
    """瀏覽器導回可以被偽造、也可能根本不發生（使用者關掉分頁）。
    狀態的真相來源只有幕後的 ReturnURL。"""
    store.orders["O123"] = {"id": "oid-1", "caller_id": "c1",
                            "return_url": "https://caller.example/done"}
    r = client.post("/ecpay/order-result", data=_order_return(),
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("https://caller.example/done?")
    assert store.calls == []                # 一個狀態都沒改


def test_callback_for_an_old_trade_no_still_finds_the_order(client, store):
    """綠界的單號送出過就不能再用，所以「回付款頁再付一次」會換新單號。
    但舊單號的回呼還是可能進來（兩個分頁、或綠界延遲重送）——
    透過 trade_attempts 解析，舊的照樣找得回同一筆訂單。"""
    store.orders["OLD"] = {"id": "oid-9", "caller_id": "c1", "status": "created"}
    store.attempts["OLD"] = ("order", "oid-9")
    store.attempts["NEW"] = ("order", "oid-9")     # 換過的新單號

    r = client.post("/ecpay/return", data=_order_return(trade_no="OLD"))
    assert r.text == "1|OK"
    (_, caller_id, kind, subject_id), = store.events.values()
    assert (caller_id, kind, subject_id) == ("c1", "order", "oid-9")


def test_second_success_on_a_paid_order_does_not_double_mark(client, store):
    """使用者開了兩個分頁、兩次都付成功。事件要留痕，但不重複標記付款。"""
    store.orders["T2"] = {"id": "oid-8", "caller_id": "c1", "status": "paid"}
    store.attempts["T2"] = ("order", "oid-8")
    r = client.post("/ecpay/return", data=_order_return(trade_no="T2"))
    assert r.text == "1|OK"
    assert ("order", "mark_paid") not in [(c[0], c[1]) for c in store.calls]
    assert len(store.events) == 1                  # 但事件有落地
