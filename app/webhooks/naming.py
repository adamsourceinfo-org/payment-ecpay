"""Cloud Tasks queue 的命名規則。**只用標準函式庫。**

為什麼單獨一個模組：這條規則有**兩個**使用者 ——
服務自己（排 task 時要知道打哪個 queue），以及 `scripts/add-caller.sh`
（上線 caller 時要建那個 queue）。兩邊算出來的名字必須逐字相同。

抄一份給腳本的話，某天改了消毒規則就會建出一個服務永遠找不到的 queue，
而症狀是「靜靜地退回共用 queue」—— 公平性消失而沒有人發現。

所以規則寫在這裡，兩邊都 import 它。而且這個檔**不可以** import httpx
之類的第三方套件 —— 腳本用的是系統 python3，不是 .venv。
"""
import hashlib
import re

# Cloud Tasks 的 queue id 只收 [A-Za-z0-9-]，上限 100 字元。
_MAX_SLUG = 40


def build_queue_name(prefix: str, caller_id: str) -> str:
    """`{prefix}-{消毒後的 caller}-{sha256 前 8 碼}`。

    ⚠️ 尾巴那 8 碼雜湊不是裝飾：消毒會把 `a.b` 與 `a-b` 變成同一個字串，
    沒有雜湊的話兩個不同的 caller 會共用一個 queue —— 隔離就白做了。
    """
    slug = re.sub(r"[^A-Za-z0-9-]", "-", caller_id)[:_MAX_SLUG].strip("-") or "caller"
    digest = hashlib.sha256(caller_id.encode()).hexdigest()[:8]
    return f"{prefix}-{slug}-{digest}"
