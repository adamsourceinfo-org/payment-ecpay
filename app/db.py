"""Cloud SQL 連線：IAM 資料庫認證，Secret Manager 裡沒有任何 DB 機密。

最容易踩的坑寫在這裡：access token 大約一小時過期。已建立的連線不受影響
（Postgres 只在連線當下驗密碼），但**每一條新連線都需要有效的 token**。
所以 token 的取得必須在「建立新連線」的路徑上，並依 expires_in 判斷是否重取；
在啟動時取一次就永久使用，會變成「跑一小時後新連線開始失敗」這種很難查的問題。
"""
import json
import os
import threading
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from queue import Empty, LifoQueue

import pg8000.dbapi

from app.config import get_settings

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


@contextmanager
def get_conn():
    """從池借一條連線，用完歸還。連線壞掉就丟棄，下次借會建新的。"""
    pool = _get_pool()
    try:
        conn = pool.get_nowait()
    except Empty:
        conn = _new_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        raise
    else:
        try:
            pool.put_nowait(conn)
        except Exception:
            conn.close()


def query(sql: str, args=(), fetch: str = "all"):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, args)
        if fetch == "none":
            return None
        cols = [d[0] for d in cur.description] if cur.description else []
        if fetch == "one":
            row = cur.fetchone()
            return dict(zip(cols, row)) if row else None
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def run_migrations(migrations_dir: str = "migrations") -> list:
    """依檔名順序套用未執行的 migration。用 advisory lock 讓多實例只有一個會跑。"""
    applied = []
    files = sorted(Path(migrations_dir).glob("*.sql"))
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK,))
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
        return {"configured": True, "ok": True, "instance": s.db_instance, **row}
    except Exception as exc:                      # noqa: BLE001 — 診斷用，要看到原文
        return {"configured": True, "ok": False, "instance": s.db_instance,
                "error": f"{type(exc).__name__}: {exc}"}
