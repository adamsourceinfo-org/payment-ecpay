from fastapi import APIRouter, Response

from app import db
from app.config import get_settings

router = APIRouter(tags=["health"])


# 路徑刻意不是 /healthz —— Google Frontend 在 *.run.app 上會攔截 /healthz
# 自己回 404，請求根本不會進到容器。
@router.get("/health")
def health(response: Response):
    """db 的 server_user 與 database 由 DB 自己回答 ——
    回音環境變數證明不了任何事。

    **壞掉時回 503。** ci 的 smoke 只看狀態碼、完全不看 body，
    回 200 但內容寫著「db 掛了」對 CI 來說是綠燈，那這個檢查什麼都沒證明。
    Cloud Run 的 startup probe 是 TCP、沒有 liveness probe，
    所以 503 不會讓實例被回收 —— 只會讓部署紅燈，正是要的效果。

    **綠界區塊刻意不做連線探測。** 綠界沒有 OAuth token 之類的便宜端點，
    唯一能打的是查詢 API，而那需要一個真的單號。用假單號去打只會污染
    綠界的日誌又證明不了憑證是對的。所以這裡只回報憑證是否已載入 ——
    誠實地少證明一點，好過發明一個假的探測。
    """
    s = get_settings()
    db_info = db.db_status()

    body = {
        "service": "payment-ecpay",
        "env": s.app_env,
        "version": s.app_version,
        "db": db_info,
        "ecpay": {
            "env": s.ecpay_env,
            "merchant_id": s.merchant_id,
            "credentials": "loaded" if (s.hash_key and s.hash_iv) else "missing",
            "allowed_payments": sorted(s.allowed_payments),
            # 退款 API 在測試環境不存在，這不是故障，是上游的限制。
            "refund_api": "available" if s.do_action_available else "stage-unavailable",
        },
    }

    if db_info.get("ok") is not True:
        response.status_code = 503
    return body
