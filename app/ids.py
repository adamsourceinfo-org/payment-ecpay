"""識別碼產生。

`MerchantTradeNo` 有兩個硬限制：**最多 20 碼**、**只能英數**，
而且唯一性是「每商店」而非每筆請求。

流水號在這裡是危險的：dev 用的測試商店 3002607 是**全球開發者共用**的，
`order-1` 或 `20260824001` 這種編號一定會撞到陌生人的訂單，
而撞到的症狀是綠界回一個看似無關的錯誤。所以一律用高熵亂碼。

前綴保留兩碼讓人一眼看得出型別（O=訂單、S=訂閱），剩下 18 碼隨機，
以 62 進位計算約 107 bits，遠超過任何實務碰撞風險。
"""
import secrets
import string

_ALPHABET = string.ascii_uppercase + string.digits
_TRADE_NO_LEN = 20


def _random(n: int) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def merchant_trade_no(prefix: str) -> str:
    """產生綠界的特店交易編號。prefix 只接受 1 碼英文，用來標示型別。"""
    if len(prefix) != 1 or prefix not in string.ascii_uppercase:
        raise ValueError("prefix 必須是 1 碼大寫英文字母")
    return prefix + _random(_TRADE_NO_LEN - 1)


def checkout_token() -> str:
    """付款導轉頁的網址 token。

    那一頁不驗 API key（使用者的瀏覽器會打它），所以**不能**用 order id 當網址 ——
    否則知道 id 就看得到別人的付款頁。獨立一個高熵 token。
    """
    return secrets.token_urlsafe(32)
