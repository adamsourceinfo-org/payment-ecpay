"""綠界回呼。這是整個服務最需要小心的地方 ——
驗簽、去重、以及「首期回呼分辨不出是不是訂閱」那個陷阱。"""
import json
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app import db
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
        self.transactions = 0
        self.scheduled = []          # dispatch.schedule 的呼叫
        self.ensured = []            # dispatch.ensure 的呼叫

    # --- events
    def record(self, dedupe_key, event_type, caller_id, kind, subject_id, raw,
               tx=None):
        if dedupe_key in self.events:
            return None                     # 綠界重送
        self.events[dedupe_key] = (event_type, caller_id, kind, subject_id)
        return len(self.events)


@pytest.fixture
def store(monkeypatch):
    fs = FakeStore()

    # 回呼現在把「落地事件 + 更新狀態」包成一個交易。這裡不需要真的連 DB，
    # 但 transaction() 一定要被呼叫到 —— 不然測的就不是實際跑的那條路。
    @contextmanager
    def _fake_tx(_fs=fs):
        _fs.transactions += 1
        yield object()
    monkeypatch.setattr(callbacks.db, "transaction", _fake_tx)

    # 推送的排程：這裡只記錄「有沒有被呼叫」。真正的投遞另外測。
    monkeypatch.setattr(callbacks.dispatch, "schedule",
                        lambda eid, cid, base, _fs=fs: _fs.scheduled.append((eid, cid)))
    monkeypatch.setattr(callbacks.dispatch, "ensure",
                        lambda key, base, _fs=fs: _fs.ensured.append(key))

    monkeypatch.setattr(callbacks.events_store, "record", fs.record)

    # trade_attempts：單號 → (kind, id)。回呼一律先走這裡，
    # 因為訂單可能換過單號（綠界的單號送出過就不能再用）。
    monkeypatch.setattr(callbacks.attempts_store, "record",
                        lambda tn, kind, sid, _fs=fs, **k: _fs.attempts.update(
                            {tn: (kind, str(sid))}))

    def _resolve(tn, _fs=fs, **k):
        hit = _fs.attempts.get(tn)
        return {"subject_kind": hit[0], "subject_id": hit[1]} if hit else None
    monkeypatch.setattr(callbacks.attempts_store, "resolve", _resolve)

    def _by_id(store_dict):
        def inner(sid, _d=store_dict, **k):
            return next((v for v in _d.values() if str(v["id"]) == str(sid)), None)
        return inner
    monkeypatch.setattr(callbacks.orders_store, "get_by_id", _by_id(fs.orders))
    monkeypatch.setattr(callbacks.subs_store, "get_by_id", _by_id(fs.subs))

    for name in ("get_by_trade_no",):
        monkeypatch.setattr(callbacks.orders_store, name,
                            lambda tn, _fs=fs, **k: _fs.orders.get(tn))
        monkeypatch.setattr(callbacks.subs_store, name,
                            lambda tn, _fs=fs, **k: _fs.subs.get(tn))
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


def _paid_marks(store):
    return [c for c in store.calls if c[1] == "mark_paid"]


def test_order_result_驗簽通過就是第二個入口(client, store):
    """OrderResultURL 與 ReturnURL 的參數集相同、用同一把 HashKey/HashIV 簽 ——
    驗簽通過的導回**一樣可信**。拿它更早把狀態弄對，
    把「錢確定」到「我們的狀態正確」的窗口縮到零。"""
    store.orders["O123"] = {"id": "oid-1", "caller_id": "c1", "status": "created",
                            "return_url": "https://caller.example/done"}
    r = client.post("/ecpay/order-result", data=_order_return(),
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("https://caller.example/done?")
    assert len(_paid_marks(store)) == 1


def test_order_result_驗簽不過就不改狀態(client, store):
    """驗不過的導回就是使用者可以偽造的那種，只導回、不採信。"""
    store.orders["O123"] = {"id": "oid-1", "caller_id": "c1", "status": "created",
                            "return_url": "https://caller.example/done"}
    bad = _order_return()
    bad["TradeAmt"] = "1"
    r = client.post("/ecpay/order-result", data=bad, follow_redirects=False)
    assert r.status_code == 303
    assert "verified=0" in r.headers["location"]
    assert store.calls == []


def test_order_result_沒帶檢查碼就不改狀態(client, store):
    store.orders["O123"] = {"id": "oid-1", "caller_id": "c1", "status": "created",
                            "return_url": "https://caller.example/done"}
    client.post("/ecpay/order-result",
                data={"MerchantTradeNo": "O123", "RtnCode": "1"},
                follow_redirects=False)
    assert store.calls == []


def test_order_result_取號導回不會憑空多一筆return事件(client, store):
    """ATM／超商取號也會導回這裡（RtnCode=2）。那筆的事件是
    /ecpay/payment-info 的 `info:` 鍵 —— 不擋的話會多出一筆 payment.return。"""
    store.orders["O123"] = {"id": "oid-1", "caller_id": "c1", "status": "created",
                            "return_url": "https://caller.example/done"}
    client.post("/ecpay/order-result", data=_order_return(rtn="2"),
                follow_redirects=False)
    assert store.events == {}
    assert store.calls == []


def test_兩個入口只更新一次狀態(client, store):
    """誰先到誰生效，靠同一個 dedupe_key。後到的那個拿到 None。"""
    store.orders["O123"] = {"id": "oid-1", "caller_id": "c1", "status": "created",
                            "return_url": "https://caller.example/done"}
    body = _order_return()
    client.post("/ecpay/order-result", data=body, follow_redirects=False)
    ack = client.post("/ecpay/return", data=body)

    assert ack.text == "1|OK"               # 後到的還是要回 1|OK
    assert len(_paid_marks(store)) == 1     # 但狀態只改一次
    assert len(store.events) == 1


def test_回呼把事件與狀態包在同一個交易裡(client, store):
    """分成兩個 commit 的話，中間掛掉就再也救不回來 ——
    綠界的重送會被 dedupe_key 擋掉，而那正是唯一的復原路徑。"""
    store.orders["O123"] = {"id": "oid-1", "caller_id": "c1", "status": "created"}
    client.post("/ecpay/return", data=_order_return())
    assert store.transactions == 1


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


def test_non_ascii_rtnmsg_verifies(client, store):
    """**綠界真實的成功通知帶中文 RtnMsg（「付款成功」）。**

    這條測試是踩到之後補的：Starlette 的 `request.form()` 用 latin-1 解碼
    urlencoded body，中文會變成亂碼，拿亂碼算檢查碼必定對不上，
    結果是「綠界說沒收到 1|OK」而我們只看到驗簽失敗。

    先前所有回呼測試都用 ASCII，latin-1 與 UTF-8 結果相同，所以全過 ——
    測試綠燈完全沒有證明這條路是通的。
    """
    store.orders["ZH"] = {"id": "oid-zh", "caller_id": "c1", "status": "created"}
    store.attempts["ZH"] = ("order", "oid-zh")
    body = sign({
        "MerchantID": "3002607", "MerchantTradeNo": "ZH", "RtnCode": "1",
        "RtnMsg": "付款成功", "TradeNo": "2608241117046064", "TradeAmt": "9",
        "PaymentDate": "2026/08/24 11:22:26", "PaymentType": "ATM_BOT",
        "PaymentTypeChargeFee": "1", "SimulatePaid": "1",
        "TradeDate": "2026/08/24 11:17:04", "StoreID": "",
        "CustomField1": "", "CustomField2": "", "CustomField3": "",
        "CustomField4": "",
    })
    r = client.post("/ecpay/return", data=body)
    assert r.status_code == 200 and r.text == "1|OK"
    assert ("order", "mark_paid") in [(c[0], c[1]) for c in store.calls]


def test_used_checkout_link_says_already_paid(client, store, monkeypatch):
    """付款完成後 token 會被清掉（不清的話重開這頁會在綠界建立**另一筆**交易，
    等於給了重複付款的機會）。但直接回 404 會讓人以為系統壞了 ——
    分辨得出來的情況要說清楚。"""
    monkeypatch.setattr(callbacks.orders_store, "get_by_checkout_token",
                        lambda t: None)
    monkeypatch.setattr(callbacks.subs_store, "get_by_checkout_token",
                        lambda t: None)
    monkeypatch.setattr(callbacks.orders_store, "get_by_used_token",
                        lambda t: {"id": "oid-1"} if t == "used" else None)
    monkeypatch.setattr(callbacks.subs_store, "get_by_used_token",
                        lambda t: None)

    r = client.get("/ecpay/checkout/used")
    assert r.status_code == 200 and "已經完成付款" in r.text

    r = client.get("/ecpay/checkout/never-existed")
    assert r.status_code == 404 and "無效" in r.text


def test_回呼的處理跑在threadpool而不是事件迴圈(client, store, monkeypatch):
    """pg8000 是同步 driver。同步呼叫寫在 async def 裡會卡住整個事件迴圈 ——
    那個實例上所有請求跟著排隊，包括 caller 正在查的 GET /v1/orders/{id}。
    一筆回呼六次 DB round trip 就是幾十毫秒的全實例停擺。"""
    import asyncio

    seen = {}
    orig = callbacks._payment_return

    def spy(raw, base):
        try:
            asyncio.get_running_loop()
            seen["on_event_loop"] = True
        except RuntimeError:
            seen["on_event_loop"] = False
        return orig(raw, base)

    monkeypatch.setattr(callbacks, "_payment_return", spy)
    store.orders["O123"] = {"id": "oid-1", "caller_id": "c1", "status": "created"}
    client.post("/ecpay/return", data=_order_return())
    assert seen == {"on_event_loop": False}


def test_連線池耗盡回503而不是卡住(client, store, monkeypatch):
    """回 503 讓綠界重送（它本來就會）。無限等會讓症狀從「慢」
    變成「整個實例沒反應」，那時候連哪裡壞了都答不出來。"""
    def boom(*a, **k):
        raise db.PoolExhausted("連線池已滿（3/3）且等待逾時")

    monkeypatch.setattr(callbacks, "_apply_return", boom)
    r = client.post("/ecpay/return", data=_order_return())
    assert r.status_code == 503
    assert r.json()["error"] == "overloaded"


def test_落地新事件就排一次推送(client, store):
    store.orders["O123"] = {"id": "oid-1", "caller_id": "c1", "status": "created"}
    client.post("/ecpay/return", data=_order_return())
    assert store.scheduled == [(1, "c1")]
    assert store.ensured == []


def test_綠界重送不排推送但要確保投遞列存在(client, store):
    """⚠️ record() 回 None 有**兩個**意思：綠界重送，或者這一筆已經被
    /ecpay/order-result 那條路處理掉了。第二種情況下沒有人排過推送 ——
    照舊早退會靜默退化成 sweep 的一小時延遲，而且正好發生在活動期間。"""
    store.orders["O123"] = {"id": "oid-1", "caller_id": "c1", "status": "created"}
    body = _order_return()
    client.post("/ecpay/return", data=body)
    store.scheduled.clear()
    client.post("/ecpay/return", data=body)
    assert store.scheduled == []
    assert store.ensured == ["return:O123:1"]


def test_導回先到時_幕後回呼仍然補上推送(client, store):
    """這是上面那條規則存在的實際場景：order-result 贏了競態，
    狀態已經對了，但推送還沒有人排。"""
    store.orders["O123"] = {"id": "oid-1", "caller_id": "c1", "status": "created",
                            "return_url": "https://caller.example/done"}
    body = _order_return()
    client.post("/ecpay/order-result", data=body, follow_redirects=False)
    assert store.scheduled == [] and store.ensured == []   # 導回不排推送

    client.post("/ecpay/return", data=body)
    assert store.ensured == ["return:O123:1"]              # 幕後補上


def test_導回不排推送(client, store):
    """導回路徑要快，而幕後回呼一定會到。"""
    store.orders["O123"] = {"id": "oid-1", "caller_id": "c1", "status": "created",
                            "return_url": "https://caller.example/done"}
    client.post("/ecpay/order-result", data=_order_return(),
                follow_redirects=False)
    assert store.scheduled == [] and store.ensured == []
