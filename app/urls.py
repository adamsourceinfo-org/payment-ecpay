"""回呼網址的來源。

綠界的 ReturnURL 必須是絕對網址，而第一次部署時我們還不知道自己的網址 ——
這是個雞生蛋。解法是**預設由請求自身推導**：Cloud Run 會把服務網域放進
Host 標頭，X-Forwarded-Proto 是 https。PUBLIC_BASE_URL 只在要換自訂網域時覆蓋。

這樣就不需要「先部署一次拿網址、再填設定、再部署一次」。
"""
from fastapi import Request

from app.config import get_settings


def base_url(request: Request) -> str:
    s = get_settings()
    if s.public_base_url:
        return s.public_base_url
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"
