"""綠界檢查碼（CheckMacValue）。整個介接的信任根，所以獨立成一個模組。

演算法（官方文件「檢查碼機制說明」）：
  1. 參數依名稱 A→Z 排序（不分大小寫）
  2. 前後夾成 HashKey=<key>&<排序後的 k=v>&HashIV=<iv>
  3. **整串** URLEncode —— 連 `=` 與 `&` 都會變成 %3d / %26
  4. 轉小寫
  5. SHA256
  6. 轉大寫

第 3 步是最容易錯的地方。綠界的基準是 .NET 的 HttpUtility.UrlEncode，
它與 Python 的 quote_plus 有兩處不同，必須修正（見 _dotnet_urlencode）。
官方範例向量在 tests/test_checkmac.py，算不出來就是這裡壞了。
"""
import hashlib
import hmac
from urllib.parse import quote_plus

# .NET 的 UrlEncode 不編碼這些字元，Python 的 quote_plus 會 —— 還原回去。
# 順序無所謂，但值必須小寫，因為替換發生在轉小寫之後。
_DOTNET_LITERALS = {
    "%21": "!",
    "%2a": "*",
    "%28": "(",
    "%29": ")",
}


def _dotnet_urlencode(raw: str) -> str:
    """模擬 .NET HttpUtility.UrlEncode，回傳**已轉小寫**的結果。

    Python 的 quote_plus 永遠不編碼 `-_.~`，.NET 則不編碼 `-_.!*()` 但會編碼 `~`。
    差集就是下面兩段修正。空白兩邊都是 `+`。
    """
    # safe="" 仍不會編碼 -_.~（那是 quote_plus 的內建 always-safe 集合）
    out = quote_plus(raw, safe="").lower()
    # .NET 會編碼 ~，Python 不會
    out = out.replace("~", "%7e")
    # .NET 不編碼 !*()，Python 會
    for encoded, literal in _DOTNET_LITERALS.items():
        out = out.replace(encoded, literal)
    return out


def _sorted_pairs(params: dict) -> str:
    """依參數名稱 A→Z（不分大小寫）排序後串成 k=v&k=v。

    值一律轉成字串。None 視為空字串 —— 綠界的表單沒有「不存在」與「空」的分別，
    但呼叫端不該為了這件事在每個地方寫 str(x or "")。
    """
    items = sorted(params.items(), key=lambda kv: kv[0].lower())
    return "&".join(f"{k}={'' if v is None else v}" for k, v in items)


def generate(params: dict, hash_key: str, hash_iv: str) -> str:
    """算出檢查碼。params **不可**包含 CheckMacValue 本身。"""
    raw = f"HashKey={hash_key}&{_sorted_pairs(params)}&HashIV={hash_iv}"
    return hashlib.sha256(_dotnet_urlencode(raw).encode()).hexdigest().upper()


def verify(params: dict, hash_key: str, hash_iv: str) -> bool:
    """驗證綠界送來的表單。會自動排除 CheckMacValue 欄位。

    比對用 constant-time —— 檢查碼是驗真偽用的，時間差洩漏沒有正當理由存在。
    綠界沒送 CheckMacValue 就是不合法，直接 False（不是「沒有就跳過」）。
    """
    received = params.get("CheckMacValue")
    if not received:
        return False
    rest = {k: v for k, v in params.items() if k != "CheckMacValue"}
    expected = generate(rest, hash_key, hash_iv)
    return hmac.compare_digest(expected, str(received).upper())
