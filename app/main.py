import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app import db
from app.config import get_settings

log = logging.getLogger("payment-ecpay")


class _RedactFilter(logging.Filter):
    """最後一道防線：機密永遠不該進日誌，但人會犯錯。

    綠界有**兩個**機密（HashKey 與 HashIV），跟 PayPal 只有一個 secret 不同。
    """

    def filter(self, record):
        s = get_settings()
        secrets = [v for v in (s.hash_key, s.hash_iv) if v]
        try:
            msg = record.getMessage()
        except Exception:                       # noqa: BLE001
            return True
        for sec in secrets:
            if sec and sec in msg:
                record.msg = msg.replace(sec, "***redacted***")
                record.args = ()
        return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    logging.basicConfig(level=getattr(logging, s.log_level.upper(), logging.INFO))
    logging.getLogger().addFilter(_RedactFilter())
    if s.db_configured:
        try:
            applied = db.run_migrations()
            log.info("migration 套用 %s", applied or "（無新項目）")
        except Exception as exc:                # noqa: BLE001
            # 不讓 migration 失敗擋住啟動，健康檢查會把 db 標成 not ok
            log.error("migration 失敗：%s: %s", type(exc).__name__, exc)
    else:
        log.warning("DB 未設定，跳過 migration")
    yield


app = FastAPI(title="payment-ecpay", version="1", lifespan=lifespan)


@app.exception_handler(db.PoolExhausted)
def _pool_exhausted(request, exc):
    """連線池滿了就誠實回 503，不要無限等。

    無限等會讓 threadpool 的 worker 全部卡住，症狀從「慢」變成
    「整個實例沒反應」，健康檢查也跟著死 —— 那時候連哪裡壞了都答不出來。
    503 讓綠界重送（它本來就會）、讓 caller 重試，至少故障說得出口。
    """
    log.error("連線池耗盡：%s", exc)
    return JSONResponse(
        {"error": "overloaded",
         "message": "database connections exhausted, retry shortly"},
        status_code=503)


def _mount():
    from app.routers import (callbacks, events, health, orders, subscriptions)
    app.include_router(health.router)
    app.include_router(orders.router)
    app.include_router(subscriptions.router)
    app.include_router(events.router)
    app.include_router(callbacks.router)


_mount()
