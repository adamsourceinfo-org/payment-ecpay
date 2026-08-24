"""綠界 HTTP 層。

跟 PayPal 完全不同的兩件事：
- **沒有 OAuth**。每一次呼叫都自帶 CheckMacValue，沒有 token 可以快取，
  也因此健康檢查無法便宜地驗證憑證是否有效（見 routers/health.py）。
- **進出都是 form-urlencoded**，不是 JSON。少數端點回 JSON，
  所以一律看 Content-Type 決定怎麼解析，不要用猜的。
"""
import json
import logging
from urllib.parse import parse_qsl

import httpx

from app.config import get_settings
from app.ecpay import checkmac
from app.errors import ECPayError

log = logging.getLogger("ecpay")


def signed(params: dict) -> dict:
    """補上 CheckMacValue。回新的 dict，不改動輸入。"""
    s = get_settings()
    out = {k: ("" if v is None else str(v)) for k, v in params.items()}
    out["CheckMacValue"] = checkmac.generate(out, s.hash_key, s.hash_iv)
    return out


def _parse(resp: httpx.Response) -> dict:
    """綠界的回應有三種形狀：JSON、form-urlencoded、以及純錯誤字串。"""
    ctype = resp.headers.get("content-type", "")
    text = resp.text.strip()
    if "json" in ctype:
        return json.loads(text)
    if "=" in text:
        return dict(parse_qsl(text, keep_blank_values=True))
    # 例如 "Error: xxx" —— 沒有結構，原文帶回去比硬套格式有用
    return {"RtnCode": "0", "RtnMsg": text}


def post(url: str, params: dict, verify_response: bool = False) -> dict:
    """送出已簽章的表單並解析回應。

    `verify_response` 給有回檢查碼的端點用（查詢類）。綠界的回應簽章
    跟請求用同一套演算法 —— 不驗的話，中間人改掉 TradeStatus 我們就信了。
    """
    s = get_settings()
    body = signed(params)
    try:
        with httpx.Client(timeout=s.timeout_seconds) as c:
            r = c.post(url, data=body,
                       headers={"Content-Type": "application/x-www-form-urlencoded"})
    except httpx.HTTPError as exc:
        raise ECPayError("network", f"{type(exc).__name__}: {exc}", status=0)

    data = _parse(r)
    if r.status_code >= 400:
        raise ECPayError(data.get("RtnCode", "0"),
                         data.get("RtnMsg", r.text[:200]), status=r.status_code,
                         raw=data)

    if verify_response and "CheckMacValue" in data:
        if not checkmac.verify(data, s.hash_key, s.hash_iv):
            # 驗不過就不能用這份資料下任何判斷
            raise ECPayError("bad_signature", "綠界回應的檢查碼驗證失敗", raw={})

    # 只記結果碼與訊息，不記整包（可能含卡號末四碼、姓名等）
    log.info("ecpay %s -> RtnCode=%s", url.rsplit("/", 1)[-1],
             data.get("RtnCode", "(無)"))
    return data


def require_success(data: dict, success_codes=("1",)) -> dict:
    """綠界的常態失敗是 HTTP 200 + RtnCode != 1。這裡統一轉成例外。"""
    code = str(data.get("RtnCode", ""))
    if code not in success_codes:
        raise ECPayError(code or "0", data.get("RtnMsg", ""), raw=data)
    return data
