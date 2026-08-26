"""投遞、內部端點、sweep。

這裡測的是「壞掉的時候會怎樣」—— 而推送這個功能的價值幾乎全在那上面。
"""
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import internal as internal_router
from app.webhooks import dispatch, signing, tasks

NOW = datetime(2026, 8, 26, 3, 14, 15, 926000, tzinfo=timezone.utc)
URL = "https://caller.example/pay/events"
KEY = {"X-Internal-Key": "test-internal-key"}


def _delivery(**kw):
    base = {"id": "d-1", "event_id": 1234, "endpoint_id": "ep-1",
            "caller_id": "c1", "url": URL, "status": "pending", "attempts": 0,
            "last_status": None, "last_error": None, "created_at": NOW,
            "updated_at": NOW, "delivered_at": None}
    base.update(kw)
    return base


def _event(**kw):
    base = {"id": 1234, "event_type": "payment.return",
            "subject_kind": "subscription", "subject_id": "0f9c1a2b",
            "payload": {"RtnMsg": "付款成功"}, "received_at": NOW,
            "caller_id": "c1"}
    base.update(kw)
    return base


class FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


@pytest.fixture
def sent():
    """記錄真正送出去的那一次 HTTP。"""
    return {}


@pytest.fixture
def wired(monkeypatch, sent):
    """一列 pending 的 delivery + 一筆事件 + 假的 httpx。"""
    state = {"row": _delivery(), "posts": []}

    monkeypatch.setattr(dispatch.deliveries_store, "get",
                        lambda did, tx=None: dict(state["row"]))
    monkeypatch.setattr(dispatch.events_store, "get",
                        lambda eid, tx=None: _event())

    def begin(did, tx=None):
        state["row"]["attempts"] += 1
        return dict(state["row"])
    monkeypatch.setattr(dispatch.deliveries_store, "begin_attempt", begin)

    def delivered(did, status, tx=None):
        state["row"].update(status="delivered", last_status=status)
    monkeypatch.setattr(dispatch.deliveries_store, "mark_delivered", delivered)

    def failed(did, status, error, tx=None):
        state["row"].update(status="failed", last_status=status, last_error=error)
    monkeypatch.setattr(dispatch.deliveries_store, "mark_failed", failed)

    # 網址在測試裡永遠是「公開的」—— SSRF 那條路另有測試
    monkeypatch.setattr(dispatch.targets, "assert_public", lambda url: None)

    def fake_post(url, content=None, headers=None, timeout=None,
                  follow_redirects=None):
        state["posts"].append({"url": url, "content": content,
                               "headers": headers,
                               "follow_redirects": follow_redirects})
        sent.update(state["posts"][-1])
        return state.pop("next_response", None) or FakeResponse(200)
    monkeypatch.setattr(dispatch.httpx, "post", fake_post)

    state["respond"] = lambda r: state.__setitem__("next_response", r)
    return state


# --- payload 形狀 -----------------------------------------------------

def test_推送的body逐欄等於GET_events的items元素():
    """兩條出口共用 app/event_view.py 的同一個函式 ——
    形狀不會漂移，因為根本沒有兩份。"""
    from app.event_view import item
    from app.routers import events as events_router

    row = _event()
    assert dispatch.event_payload(row) == item(row)
    assert events_router.item is item        # 拉取那條也用同一個
    assert "caller_id" not in item(row)      # caller 自己知道自己是誰


def test_ping的body形狀一樣但id是0():
    body = dispatch.ping_payload()
    assert list(body) == list(dispatch.event_payload(_event()))
    assert body["id"] == 0 and body["event_type"] == "ping"


def test_簽的與送的是同一份bytes(wired, sent, fake_settings):
    """分成「簽一份、送另一份」是這類系統的經典 bug：重新 json.dumps
    出來的字串跟原文不保證逐位元組相同，而且只在有中文的 payload 上才發作。"""
    dispatch.deliver("d-1")
    raw = sent["content"]
    assert "付款成功" in raw.decode()          # ensure_ascii=False
    t = int(sent["headers"]["X-Signature"].split(",")[0].split("=")[1])
    expected = signing.header(signing.secret_for("c1"), t, raw)
    assert sent["headers"]["X-Signature"] == expected
    assert json.loads(raw)["id"] == 1234


def test_不跟隨redirect(wired, sent, fake_settings):
    """跟了就繞過送出當下那一關 SSRF 檢查。"""
    dispatch.deliver("d-1")
    assert sent["follow_redirects"] is False


# --- 投遞結果 ---------------------------------------------------------

def test_caller回200就是delivered(wired, fake_settings):
    assert dispatch.deliver("d-1") == ("delivered", 200)
    assert wired["row"]["status"] == "delivered"


def test_caller回500就是failed(wired, fake_settings):
    wired["respond"](FakeResponse(500, "boom"))
    outcome, status = dispatch.deliver("d-1")
    assert (outcome, status) == ("failed", 500)
    assert wired["row"]["status"] == "failed"


def test_timeout也算失敗(wired, monkeypatch, fake_settings):
    def boom(*a, **k):
        raise TimeoutError("read timeout")
    monkeypatch.setattr(dispatch.httpx, "post", boom)
    assert dispatch.deliver("d-1") == ("failed", None)
    assert "TimeoutError" in wired["row"]["last_error"]


def test_已經送達就不再送一次(wired, fake_settings):
    """Cloud Tasks 是至少一次的，重複派送很正常。"""
    wired["row"]["status"] = "delivered"
    wired["row"]["last_status"] = 200
    assert dispatch.deliver("d-1") == ("done", 200)
    assert wired["posts"] == []


def test_已經是死信就停手(wired, fake_settings):
    wired["row"]["status"] = "dead"
    assert dispatch.deliver("d-1")[0] == "done"
    assert wired["posts"] == []


def test_嘗試次數來自我們自己的欄位而不是CloudTasks的header(wired, sent,
                                                            fake_settings):
    """sweep 重排過的 delivery 會拿到一個**全新的** task，
    X-CloudTasks-TaskRetryCount 從 0 重新算 —— 用它的話 caller 會看到
    「第 1 次嘗試」出現在已經失敗二十次的 delivery 上。"""
    wired["row"]["attempts"] = 19
    dispatch.deliver("d-1")
    assert sent["headers"]["X-Delivery-Attempt"] == "20"


# --- 內部端點 ---------------------------------------------------------

@pytest.fixture
def client():
    return TestClient(app)


def test_內部端點沒帶金鑰回401(client):
    assert client.post("/internal/deliveries/d-1").status_code == 401
    assert client.post("/internal/deliveries/sweep").status_code == 401


def test_內部端點帶錯金鑰回401(client):
    bad = {"X-Internal-Key": "wrong-key"}
    assert client.post("/internal/deliveries/d-1", headers=bad).status_code == 401


def test_非ASCII的金鑰回401而不是500(client):
    """⚠️ hmac.compare_digest 的 str 版本只吃 ASCII，而 HTTP header 可以帶
    latin-1 字元 —— 不 encode 就比的話，隨便一個亂打的 key 會拋 TypeError，
    讓這支端點吐 500 加一份 stack trace。"""
    bad = {"X-Internal-Key": "猜的".encode("utf-8")}
    assert client.post("/internal/deliveries/d-1", headers=bad).status_code == 401


def test_投遞失敗時內部端點回502讓佇列重試(client, monkeypatch):
    """⚠️ 回應碼是給 Cloud Tasks 看的，不是給人看的。
    回 2xx 佇列就以為成功了，不會再重試。"""
    monkeypatch.setattr(internal_router.dispatch, "deliver",
                        lambda did: ("failed", 500))
    r = client.post("/internal/deliveries/d-1", headers=KEY)
    assert r.status_code == 502
    assert r.json()["caller_status"] == 500


def test_投遞成功時內部端點回200讓佇列停手(client, monkeypatch):
    monkeypatch.setattr(internal_router.dispatch, "deliver",
                        lambda did: ("delivered", 200))
    assert client.post("/internal/deliveries/d-1", headers=KEY).status_code == 200


def test_那一列不見了就回200_重試沒有意義(client, monkeypatch):
    monkeypatch.setattr(internal_router.dispatch, "deliver",
                        lambda did: ("missing", None))
    assert client.post("/internal/deliveries/d-1", headers=KEY).status_code == 200


def test_sweep走的是自己的路徑不是被當成delivery_id(client, monkeypatch):
    """`/internal/deliveries/sweep` 必須註冊在 `/{delivery_id}` 前面，
    否則 "sweep" 會被當成一個 id。"""
    monkeypatch.setattr(internal_router.dispatch, "sweep",
                        lambda base: {"filled": 7, "requeued": 0, "dead": 0,
                                      "truncated": False})
    r = client.post("/internal/deliveries/sweep", headers=KEY)
    assert r.status_code == 200 and r.json()["filled"] == 7


# --- queue 命名 -------------------------------------------------------

def test_queue名字對同一個caller穩定(fake_settings):
    assert tasks.queue_name("c1") == tasks.queue_name("c1")


def test_消毒後會撞名的兩個caller拿到不同的queue(fake_settings):
    """⚠️ 尾巴那 8 碼雜湊不是裝飾：消毒會把 `a.b` 與 `a-b` 變成同一個字串，
    沒有雜湊的話兩個不同的 caller 會共用一個 queue —— 隔離就白做了。"""
    assert tasks.queue_name("a.b") != tasks.queue_name("a-b")


def test_queue名字合法且不超過長度上限(fake_settings):
    import re
    name = tasks.queue_name("某個很長的中文 caller 名稱" * 5)
    assert re.fullmatch(r"[A-Za-z0-9-]{1,100}", name)


def test_queue不存在時退回共用queue並吵一聲(monkeypatch, fake_settings, caplog):
    """不吵的話所有 caller 會靜靜地退化回共用 queue，
    公平性消失而沒有人知道 —— 直到活動當天。"""
    used = []

    def fake_create(queue, url, headers, timeout):
        used.append(queue)
        if len(used) == 1:
            raise tasks.QueueMissing(queue)
        return "task/1"

    monkeypatch.setattr(tasks, "create_task", fake_create)
    with caplog.at_level("ERROR"):
        tasks.enqueue_delivery("c1", "https://self/internal/deliveries/d-1")
    assert used[1] == "payment-ecpay-deliveries"        # 共用 queue
    assert "不存在" in caplog.text


# --- sweep ------------------------------------------------------------

@pytest.fixture
def sweeper(monkeypatch):
    state = {"missing": [], "never": [], "dead": [], "created": [],
             "requeued": [], "enqueued": []}
    monkeypatch.setattr(dispatch.deliveries_store, "missing",
                        lambda limit, tx=None: state["missing"].pop(0)
                        if state["missing"] else [])
    monkeypatch.setattr(dispatch.deliveries_store, "never_dispatched",
                        lambda limit, tx=None: state["never"])
    monkeypatch.setattr(dispatch.deliveries_store, "mark_dead_older_than",
                        lambda secs, limit, tx=None: state["dead"])
    monkeypatch.setattr(
        dispatch.deliveries_store, "create",
        lambda ev, ep, cid, url, tx=None: state["created"].append(ev)
        or {"id": f"d-{ev}", "caller_id": cid, "url": url})
    monkeypatch.setattr(dispatch.deliveries_store, "requeue",
                        lambda did, tx=None: state["requeued"].append(did))
    monkeypatch.setattr(dispatch.tasks, "enqueue_delivery",
                        lambda cid, url: state["enqueued"].append(url))
    monkeypatch.setattr(dispatch.tasks, "retry_window_seconds",
                        lambda q, timeout=5.0: 12 * 3600)
    return state


def _missing_row(event_id):
    return {"event_id": event_id, "caller_id": "c1", "endpoint_id": "ep-1",
            "url": URL}


def test_sweep補上沒有投遞列的事件(sweeper, fake_settings):
    sweeper["missing"] = [[_missing_row(1), _missing_row(2)]]
    out = dispatch.sweep("https://self")
    assert out["filled"] == 2
    assert sweeper["created"] == [1, 2]
    assert len(sweeper["enqueued"]) == 2


def test_sweep會一直掃到乾淨而不是只掃一批(sweeper, fake_settings):
    """突發漏了一萬筆，每小時只補 500 就要二十小時才排乾 ——
    而過程中沒有任何人知道它正在追進度。"""
    sweeper["missing"] = [[_missing_row(i) for i in range(500)],
                          [_missing_row(500)]]
    out = dispatch.sweep("https://self")
    assert out["filled"] == 501          # 掃了兩輪
    assert out["truncated"] is False


def test_sweep撞到單輪上限要吵不要靜默截斷(sweeper, fake_settings, caplog,
                                            monkeypatch):
    monkeypatch.setattr(dispatch, "_MAX_PER_RUN", 500)
    sweeper["missing"] = [[_missing_row(i) for i in range(500)],
                          [_missing_row(500)]]
    with caplog.at_level("WARNING"):
        out = dispatch.sweep("https://self")
    assert out["truncated"] is True
    assert "上限" in caplog.text


def test_sweep重排從未派送成功的(sweeper, fake_settings):
    """failed 且 attempts = 0 唯一地代表「列建了但 task 沒建成」。"""
    sweeper["never"] = [_delivery(id="d-9", status="failed", attempts=0)]
    out = dispatch.sweep("https://self")
    assert out["requeued"] == 1 and sweeper["requeued"] == ["d-9"]


def test_sweep標死信並逐筆記ERROR(sweeper, fake_settings, caplog):
    """不標的話「送不出去的事件」只存在於 Cloud Tasks 的統計裡，
    服務自己答不出來 —— 而那正是這個欄位存在的唯一理由。"""
    sweeper["dead"] = [_delivery(id="d-死", status="dead", attempts=23)]
    with caplog.at_level("ERROR"):
        out = dispatch.sweep("https://self")
    assert out["dead"] == 1
    assert "死信" in caplog.text and "d-死" in caplog.text


def test_死信門檻是queue窗口的兩倍(sweeper, fake_settings):
    assert dispatch.dead_threshold_seconds() == 2 * 12 * 3600


def test_讀不到queue設定就退回常數並吵一聲(monkeypatch, fake_settings, caplog):
    """抓寬在兩個方向都安全：不會誤判還在重試的，
    真死信最晚在兩倍窗口內也看得到。"""
    monkeypatch.setattr(dispatch.tasks, "retry_window_seconds",
                        lambda q, timeout=5.0: None)
    with caplog.at_level("WARNING"):
        assert dispatch.dead_threshold_seconds() == 2 * dispatch._DEFAULT_WINDOW_SECONDS
    assert "退回" in caplog.text


# --- 排程的降級 -------------------------------------------------------

def test_排程失敗不拋例外_把那列標成failed讓sweep撿回去(monkeypatch,
                                                        fake_settings):
    """⚠️ 排程失敗**不可以**讓綠界的回呼回非 1|OK。上游的重送是為了
    「事件沒收到」，不是為了「我們沒轉給 caller」—— 而且我們一旦回過
    1|OK，綠界就再也不會給第二次機會。"""
    marked = {}
    monkeypatch.setattr(dispatch.endpoints_store, "get_active",
                        lambda cid, tx=None: {"id": "ep-1", "url": URL})
    monkeypatch.setattr(dispatch.deliveries_store, "create",
                        lambda *a, **k: {"id": "d-1", "caller_id": "c1"})
    monkeypatch.setattr(dispatch.tasks, "enqueue_delivery",
                        lambda cid, url: (_ for _ in ()).throw(
                            RuntimeError("Cloud Tasks 掛了")))
    monkeypatch.setattr(dispatch.deliveries_store, "mark_failed",
                        lambda did, st, err, tx=None: marked.update(
                            id=did, error=err))

    dispatch.schedule(1234, "c1", "https://self")     # 不該拋
    assert marked["id"] == "d-1" and marked["error"].startswith("enqueue:")


def test_推送未設定時不排程(monkeypatch, fake_settings):
    fake_settings.push_configured = False
    called = []
    monkeypatch.setattr(dispatch.endpoints_store, "get_active",
                        lambda cid, tx=None: called.append(cid))
    dispatch.schedule(1234, "c1", "https://self")
    assert called == []


def test_沒有caller的事件不推(monkeypatch, fake_settings):
    """caller_id IS NULL 的事件對每個 caller 都不可見，推了就是洩漏。"""
    called = []
    monkeypatch.setattr(dispatch.endpoints_store, "get_active",
                        lambda cid, tx=None: called.append(cid))
    dispatch.schedule(1234, None, "https://self")
    assert called == []


def test_沒註冊端點就不建投遞列(monkeypatch, fake_settings):
    monkeypatch.setattr(dispatch.endpoints_store, "get_active",
                        lambda cid, tx=None: None)
    created = []
    monkeypatch.setattr(dispatch.deliveries_store, "create",
                        lambda *a, **k: created.append(a))
    dispatch.schedule(1234, "c1", "https://self")
    assert created == []


def test_ensure在另一個入口已經排過時不重複建(monkeypatch, fake_settings):
    monkeypatch.setattr(dispatch.events_store, "get_by_dedupe_key",
                        lambda key, tx=None: {"id": 1234, "caller_id": "c1"})
    monkeypatch.setattr(dispatch.endpoints_store, "get_active",
                        lambda cid, tx=None: {"id": "ep-1", "url": URL})
    monkeypatch.setattr(dispatch.deliveries_store, "exists_for_event",
                        lambda ev, ep, tx=None: True)
    created = []
    monkeypatch.setattr(dispatch.deliveries_store, "create",
                        lambda *a, **k: created.append(a))
    dispatch.ensure("return:O123:1", "https://self")
    assert created == []


def test_ensure在沒有人排過時補一筆(monkeypatch, fake_settings):
    """導回先到、幕後回呼後到的那個場景 —— 狀態已經對了，但推送還沒有人排。"""
    monkeypatch.setattr(dispatch.events_store, "get_by_dedupe_key",
                        lambda key, tx=None: {"id": 1234, "caller_id": "c1"})
    monkeypatch.setattr(dispatch.endpoints_store, "get_active",
                        lambda cid, tx=None: {"id": "ep-1", "url": URL})
    monkeypatch.setattr(dispatch.deliveries_store, "exists_for_event",
                        lambda ev, ep, tx=None: False)
    created = []
    monkeypatch.setattr(
        dispatch.deliveries_store, "create",
        lambda ev, ep, cid, url, tx=None: created.append(ev)
        or {"id": "d-1", "caller_id": cid})
    monkeypatch.setattr(dispatch.tasks, "enqueue_delivery", lambda cid, url: None)
    dispatch.ensure("return:O123:1", "https://self")
    assert created == [1234]
