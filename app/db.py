"""Cloud SQL 連線：IAM 資料庫認證，Secret Manager 裡沒有任何 DB 機密。

最容易踩的坑寫在這裡：access token 大約一小時過期。已建立的連線不受影響
（Postgres 只在連線當下驗密碼），但**每一條新連線都需要有效的 token**。
所以 token 的取得必須在「建立新連線」的路徑上，並依 expires_in 判斷是否重取；
在啟動時取一次就永久使用，會變成「跑一小時後新連線開始失敗」這種很難查的問題。

第二個坑是連線數。`apps-pg` 是**一個環境一台**、服務只靠 database 分隔，
所以這裡開太多連線不只拖垮自己，是拖垮同一台上的**其他服務**。
`DB_POOL_MAX` 因此是「同時在外的連線數」的硬上限，不只是池的大小 ——
借不到就等，等逾時就 PoolExhausted 回 503。容量規則：

    實例數 × DB_POOL_MAX ≤ 本服務的 Cloud SQL 連線預算

任一邊單獨調都是錯的，`max-instances` 與 `DB_POOL_MAX` 要一起看。
"""
import json
import logging
import threading
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from queue import Empty, LifoQueue

import pg8000.dbapi
from pg8000.exceptions import InterfaceError

from app.config import get_settings

log = logging.getLogger("db")

_METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/token"
)
# token 剩餘壽命低於這個秒數就重取
_REFRESH_MARGIN = 60
# migration 用的 advisory lock 常數，多個 Cloud Run 實例同時啟動時只有一個會跑
_MIGRATION_LOCK = 8891_0002

_token_cache = None          # (token, expires_at)
_token_lock = threading.Lock()
_pool: LifoQueue = None
_pool_lock = threading.Lock()
# 同時在外＋池裡的連線總數。上限是 DB_POOL_MAX。
_open = 0
_open_cv = threading.Condition()


class PoolExhausted(RuntimeError):
    """在 DB_POOL_TIMEOUT_SECONDS 內借不到連線。

    **刻意不是無限等。** 無限等會讓 threadpool 的 worker 全部卡住，
    症狀從「慢」變成「整個實例沒反應」，健康檢查也跟著死 ——
    那時候連「哪裡壞了」都答不出來。回 503 讓綠界重送（它本來就會）、
    讓 caller 重試，至少故障是說得出口的。
    """


def _fetch_token():
    """回 (access_token, expires_in)。"""
    req = urllib.request.Request(
        _METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.load(resp)
    return data["access_token"], int(data.get("expires_in", 3600))


def iam_token() -> str:
    global _token_cache
    with _token_lock:
        now = time.time()
        if _token_cache and _token_cache[1] - now > _REFRESH_MARGIN:
            return _token_cache[0]
        token, expires_in = _fetch_token()
        _token_cache = (token, now + expires_in)
        return token


def _new_conn():
    s = get_settings()
    return pg8000.dbapi.connect(
        user=s.db_user,
        password=iam_token(),      # 每條新連線都要有效 token
        database=s.db_name,
        unix_sock=f"/cloudsql/{s.db_instance}/.s.PGSQL.5432",
    )


def _get_pool() -> LifoQueue:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = LifoQueue(maxsize=get_settings().db_pool_max)
    return _pool


def _stale(exc: BaseException) -> bool:
    """這條連線是不是死了（而不是「這個查詢有問題」）。

    ⚠️ Cloud SQL 會關掉閒置太久的連線，而池是 LIFO ——
    低流量時最底下那幾條可以躺很久。借到那種連線的第一個請求會拿到
    `InterfaceError: network error`，而它跟「SQL 寫錯」是完全不同的事：
    前者換一條線重試就好，後者重試幾次都一樣。

    這是實跑 dev 才看到的：連著打四個請求，只有**第一個**回 500。
    原本的 get_conn() 會丟棄壞連線，所以後面三個都正常 ——
    但那個倒楣的 caller 已經吃到 500 了。
    """
    return isinstance(exc, (InterfaceError, OSError, ConnectionError))


def _acquire_ex():
    """回 (conn, 是不是從池裡借的)。從池裡借的才值得重試。"""
    global _open
    s = get_settings()
    pool = _get_pool()
    deadline = time.monotonic() + s.db_pool_timeout_seconds
    while True:
        try:
            return pool.get_nowait(), True
        except Empty:
            pass
        with _open_cv:
            if _open < s.db_pool_max:
                _open += 1
                break                     # 佔到名額，出去建連線
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PoolExhausted(
                    f"連線池已滿（{_open}/{s.db_pool_max}）且等待逾時")
            # 被喚醒後回到迴圈重試 —— 池裡那條可能已經被別人搶走了
            _open_cv.wait(remaining)
    try:
        return _new_conn(), False
    except Exception:
        # ⚠️ 名額一定要還。漏掉的話池會慢慢「漏」到永久耗盡，
        # 而症狀是「跑一陣子之後開始逾時」—— 最難查的那一種。
        with _open_cv:
            _open -= 1
            _open_cv.notify()
        raise


def _acquire():
    return _acquire_ex()[0]


def _release(conn, discard: bool = False) -> None:
    """歸還連線。壞掉的就丟棄，並把名額還回去。"""
    global _open
    if not discard:
        try:
            _get_pool().put_nowait(conn)
        except Exception:                 # noqa: BLE001 — 池滿就丟棄
            discard = True
        else:
            with _open_cv:
                _open_cv.notify()         # 等待中的人現在借得到了
            return
    try:
        conn.close()
    except Exception:                     # noqa: BLE001
        pass
    with _open_cv:
        _open -= 1
        _open_cv.notify()


@contextmanager
def get_conn():
    """從池借一條連線，用完歸還。連線壞掉就丟棄，下次借會建新的。"""
    conn = _acquire()
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:                 # noqa: BLE001
            pass
        _release(conn, discard=True)
        raise
    else:
        _release(conn)


def _exec(conn, sql: str, args, fetch: str):
    cur = conn.cursor()
    cur.execute(sql, args)
    if fetch == "none":
        return None
    cols = [d[0] for d in cur.description] if cur.description else []
    if fetch == "one":
        row = cur.fetchone()
        return dict(zip(cols, row)) if row else None
    return [dict(zip(cols, r)) for r in cur.fetchall()]


class Tx:
    """一個交易裡的查詢入口。由 transaction() 產生，不要自己建。"""

    __slots__ = ("_conn",)

    def __init__(self, conn):
        self._conn = conn

    def query(self, sql: str, args=(), fetch: str = "all"):
        return _exec(self._conn, sql, args, fetch)


@contextmanager
def transaction():
    """把多次查詢包成**一個**交易。

    為什麼需要它：`query()` 每呼叫一次就是一個交易，所以
    「落地事件」與「更新訂單狀態」原本是兩次獨立的 commit。中間掛掉的話，
    綠界的重送會被 dedupe_key 擋掉、走 `record() 回 None` 的早退路徑，
    **狀態更新永遠不會執行** —— 去重鍵一邊做著它該做的事，
    一邊堵死了唯一的復原路徑。

    用法（⚠️ 區塊裡一律要傳 tx=tx，否則那次寫入會落在交易外面）：

        with db.transaction() as tx:
            new_id = events_store.record(..., tx=tx)
            if new_id is None:
                return                      # 這次真的什麼都沒做
            subs_store.mark_active(..., tx=tx)
    """
    with get_conn() as conn:
        yield Tx(conn)


def query(sql: str, args=(), fetch: str = "all", tx: Tx = None):
    """給了 tx 就用那條連線、不自己 commit；沒給就維持原本的行為。

    ⚠️ **從池裡借到的死連線會換一條重試一次。**
    Cloud SQL 會關掉閒置太久的連線，而低流量時池底那幾條躺得最久 ——
    不重試的話，每次服務閒置一陣子之後的第一個請求就吃一個 500。

    只重試「池裡借來的」：新建的連線失敗代表 DB 真的連不上，重試只是把
    延遲加倍。也只重試單句查詢 —— `transaction()` 裡的是呼叫端的程式碼，
    這一層沒有立場替呼叫端重放它。
    """
    if tx is not None:
        return tx.query(sql, args, fetch)
    for is_last in (False, True):
        conn, from_pool = _acquire_ex()
        try:
            out = _exec(conn, sql, args, fetch)
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:                 # noqa: BLE001
                pass
            _release(conn, discard=True)
            if not is_last and from_pool and _stale(exc):
                log.info("池裡的連線已失效（%s），換一條重試", type(exc).__name__)
                continue
            raise
        else:
            _release(conn)
            return out


def run_migrations(migrations_dir: str = "migrations") -> list:
    """依檔名順序套用未執行的 migration。

    ⚠️ 用 **try** lock，拿不到就跳過。拿不到代表別的實例正在跑，等它沒有意義 ——
    而阻塞式的 pg_advisory_lock 會讓「20 個實例同時冷啟動」變成序列的，
    每個都先付了一次 IAM token + TLS 握手才排進去等。行銷活動的第一波
    正好是這個形狀。

    代價：跳過的實例可能在 schema 還沒套用完時就開始服務。這在這個 repo
    可接受，因為 migration 一律是 IF NOT EXISTS／加欄位的相容變更，
    而且部署是 rolling 的（舊 revision 本來就在跑舊 schema）。
    **要做破壞性 migration 時這個前提就不成立** —— 那種要走 CI 的獨立 job。
    """
    applied = []
    files = sorted(Path(migrations_dir).glob("*.sql"))
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pg_try_advisory_lock(%s)", (_MIGRATION_LOCK,))
        row = cur.fetchone()
        if not (row and row[0]):
            # INFO 不是 WARNING —— 這是正常的擴容行為，不是異常
            log.info("另一個實例正在套用 migration，跳過")
            return applied
        try:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version text PRIMARY KEY,"
                " applied_at timestamptz NOT NULL DEFAULT now())")
            cur.execute("SELECT version FROM schema_migrations")
            done = {r[0] for r in cur.fetchall()}
            for f in files:
                if f.name in done:
                    continue
                cur.execute(f.read_text())
                cur.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)", (f.name,))
                applied.append(f.name)
        finally:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK,))
    return applied


def db_status() -> dict:
    """健康檢查用。server_user 與 database 由 DB 自己回答 ——
    回音環境變數證明不了任何事。"""
    s = get_settings()
    if not s.db_configured:
        return {"configured": False}
    try:
        row = query(
            "SELECT current_user AS server_user, current_database() AS database",
            fetch="one")
        return {"configured": True, "ok": True, "instance": s.db_instance,
                "pool": {"open": _open, "max": s.db_pool_max}, **row}
    except Exception as exc:                      # noqa: BLE001 — 診斷用，要看到原文
        return {"configured": True, "ok": False, "instance": s.db_instance,
                "pool": {"open": _open, "max": s.db_pool_max},
                "error": f"{type(exc).__name__}: {exc}"}


def reset_pool_for_tests() -> None:
    global _pool, _open
    with _open_cv:
        _open = 0
    _pool = None
