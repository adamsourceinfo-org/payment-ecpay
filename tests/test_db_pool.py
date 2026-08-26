"""連線池的上界與交易。

這兩件事在平常的測試裡看不出差別 —— 它們只在突發流量下才有意義，
而那正是最不想靠上線去發現的時候。
"""
import threading
import time

import pytest

from app import db


class FakeConn:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.executed = []

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class FakeCursor:
    description = None

    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, args=()):
        self.conn.executed.append((sql, args))

    def fetchone(self):
        return None

    def fetchall(self):
        return []


@pytest.fixture
def conns(monkeypatch):
    """把 _new_conn 換掉，並在每個測試前後清乾淨模組層的池狀態。"""
    made = []

    def _new():
        c = FakeConn()
        made.append(c)
        return c

    db.reset_pool_for_tests()
    monkeypatch.setattr(db, "_new_conn", _new)
    yield made
    db.reset_pool_for_tests()


def test_開到上限就不再開新連線(conns, fake_settings):
    fake_settings.db_pool_max = 2

    a = db._acquire()
    b = db._acquire()
    assert len(conns) == 2

    db._release(a)
    c = db._acquire()          # 應該重用歸還的那條，不是開第三條
    assert len(conns) == 2
    assert c is a

    db._release(b)
    db._release(c)


def test_借不到就等到有人歸還(conns, fake_settings):
    fake_settings.db_pool_max = 1
    fake_settings.db_pool_timeout_seconds = 5

    held = db._acquire()
    got = []

    def waiter():
        got.append(db._acquire())

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.05)
    assert got == []           # 還在等

    db._release(held)
    t.join(timeout=2)
    assert got and got[0] is held
    assert len(conns) == 1
    db._release(got[0])


def test_等逾時要拋PoolExhausted而不是無限等(conns, fake_settings):
    """無限等會讓 threadpool 的 worker 全部卡住，
    症狀從「慢」變成「整個實例沒反應」—— 那時候連哪裡壞了都答不出來。"""
    fake_settings.db_pool_max = 1
    fake_settings.db_pool_timeout_seconds = 0.1

    held = db._acquire()
    started = time.monotonic()
    with pytest.raises(db.PoolExhausted):
        db._acquire()
    assert time.monotonic() - started < 2
    db._release(held)


def test_壞掉的連線被丟棄後名額要還回來(conns, fake_settings):
    """漏掉這個遞減，池會慢慢「漏」到永久耗盡 ——
    而且症狀是「跑一陣子之後開始逾時」，最難查的那一種。"""
    fake_settings.db_pool_max = 1
    fake_settings.db_pool_timeout_seconds = 0.5

    a = db._acquire()
    db._release(a, discard=True)
    assert a.closed

    b = db._acquire()          # 名額還回來了，所以借得到
    assert b is not a
    db._release(b)


def test_連線建立失敗也要還名額(monkeypatch, fake_settings):
    fake_settings.db_pool_max = 1
    fake_settings.db_pool_timeout_seconds = 0.5
    db.reset_pool_for_tests()

    def boom():
        raise RuntimeError("連不上")

    monkeypatch.setattr(db, "_new_conn", boom)
    for _ in range(3):
        # 每次都要拋原本的錯，不能第二次就變成 PoolExhausted
        with pytest.raises(RuntimeError, match="連不上"):
            db._acquire()
    db.reset_pool_for_tests()


# --- 交易 -------------------------------------------------------------

def test_交易裡的多次查詢共用同一條連線且只commit一次(conns, fake_settings):
    fake_settings.db_pool_max = 3

    with db.transaction() as tx:
        db.query("SELECT 1", tx=tx)
        db.query("SELECT 2", tx=tx)

    assert len(conns) == 1
    assert conns[0].commits == 1
    assert [sql for sql, _ in conns[0].executed] == ["SELECT 1", "SELECT 2"]


def test_交易中途拋例外時兩次寫入都不落地(conns, fake_settings):
    """這條就是那個既有 bug 的迴歸測試：事件落地了、狀態更新沒跑，
    而綠界的重送會被去重擋掉 —— 復原路徑被自己的冪等堵死。"""
    fake_settings.db_pool_max = 3

    with pytest.raises(RuntimeError):
        with db.transaction() as tx:
            db.query("INSERT event", tx=tx)
            raise RuntimeError("實例被回收")

    assert conns[0].commits == 0
    assert conns[0].rollbacks == 1


def test_不給tx時行為不變_各自借還各自commit(conns, fake_settings):
    fake_settings.db_pool_max = 3

    db.query("SELECT 1")
    db.query("SELECT 2")

    assert len(conns) == 1          # LIFO 池重用同一條
    assert conns[0].commits == 2    # 但是兩個獨立的交易


# --- migration 鎖 -----------------------------------------------------

def test_拿不到advisory_lock就跳過不阻塞(conns, fake_settings, monkeypatch, caplog):
    """突發擴容時 20 個實例同時啟動，阻塞式的 pg_advisory_lock
    會把冷啟動變成序列的。拿不到代表別人正在跑 —— 沒有理由等它。"""

    class LockedCursor(FakeCursor):
        def fetchone(self):
            return (False,)         # pg_try_advisory_lock 回 false

    monkeypatch.setattr(FakeConn, "cursor", lambda self: LockedCursor(self))

    applied = db.run_migrations("migrations")
    assert applied == []
    sqls = " ".join(sql for sql, _ in conns[0].executed)
    assert "pg_try_advisory_lock" in sqls
    assert "pg_advisory_unlock" not in sqls   # 沒拿到就不該解鎖
