"""簽章：密鑰推導與 X-Signature。

密鑰是**推導**出來的，資料庫裡一個字都沒有：

    secret = hex( HMAC-SHA256( key = WEBHOOK_SIGNING_KEY, msg = caller_id ) )

**為什麼不是「每個端點隨機一把存進 DB」**：API key 是**入站**認證，服務只需要
*驗證*它，所以存 sha256 就夠了；簽章密鑰是**出站**的，服務必須*持有*它才簽得出來
—— 存 hash 沒有意義，存明文就是資料庫裡多一欄機密。推導的話一個字都不用存。

**為什麼綁 caller_id 而不是 endpoint_id**：日後開放多端點時，同一個 caller 的
每個端點共用同一把密鑰 —— 那是同一個信任邊界，分開沒有帶來任何隔離，
卻要 caller 記住「哪一把對應哪一個端點」。而且推導不必先讀 DB。

⚠️ **代價**：換掉 `WEBHOOK_SIGNING_KEY` 等於**同時**換掉所有 caller 的密鑰。
這是刻意的取捨 —— 逐 caller 輪替換來的是一欄要保護的明文，不划算。
真的要輪替就是一次全部，並事先通知 caller。
"""
import hashlib
import hmac

from app.config import get_settings

# caller 驗簽時的時間容忍度。寫在這裡是為了讓 README 抄得到同一個數字。
TOLERANCE_SECONDS = 300


def secret_for(caller_id: str) -> str:
    """推導這個 caller 的簽章密鑰。隨時算得回來，所以「密鑰弄丟了」這條路不存在。"""
    s = get_settings()
    if not s.webhook_signing_key:
        raise RuntimeError("WEBHOOK_SIGNING_KEY 未設定")
    return hmac.new(s.webhook_signing_key.encode(), caller_id.encode(),
                    hashlib.sha256).hexdigest()


def signature(secret: str, t: int, raw: bytes) -> str:
    """`HMAC-SHA256(secret, "{t}." + raw_body)` 的小寫 hex。

    ⚠️ 簽的是**原始 bytes**，不是重新序列化過的 JSON。
    重新 `json.dumps` 出來的字串跟原文不保證逐位元組相同（鍵的順序、
    Unicode 跳脫、空白都可能不同）—— 那會讓 caller 永遠驗不過，
    而且只在有中文的 payload 上才發作。
    """
    msg = f"{t}.".encode() + raw
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def header(secret: str, t: int, raw: bytes) -> str:
    """X-Signature 的值。"""
    return f"t={t},v1={signature(secret, t, raw)}"
