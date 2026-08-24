"""金額驗證。綠界只收 TWD 且**不接受小數**（`TotalAmount` 是整數）。

在進門就擋，不要送到綠界才被拒 —— 那時錯誤訊息對 caller 沒有幫助，
而且浪費一次外部呼叫，還在綠界那邊留下一筆失敗紀錄。
"""
from decimal import Decimal, InvalidOperation

from app.errors import InvalidAmount

CURRENCY = "TWD"
# 綠界單筆上限。超過的在綠界那邊會被拒，先擋掉省一次往返。
MAX_AMOUNT = 20_000_000


def validate_amount(amount, field: str = "amount") -> int:
    """回正整數。接受 int 或字串形式的整數，拒絕小數與非數字。

    刻意**不接受** "100.00" —— 綠界的欄位是整數，接受帶小數點的寫法
    只會讓 caller 以為這裡支援小數，然後某天傳 "100.50" 被靜靜地截掉。
    """
    if isinstance(amount, bool):        # bool 是 int 的子類，會偷渡
        raise InvalidAmount("金額必須是整數", field=field)
    if isinstance(amount, int):
        value = amount
    else:
        text = str(amount).strip()
        try:
            dec = Decimal(text)
        except (InvalidOperation, ValueError):
            raise InvalidAmount(f"金額格式不正確：{amount!r}", field=field)
        # 看**小數位數**而不是數值：Decimal("1.00") == Decimal("1") 是成立的，
        # 用數值比較會放行 "1.00"，而放行它就等於默許 caller 之後傳 "100.50"。
        if dec.as_tuple().exponent < 0:
            raise InvalidAmount(
                f"綠界只接受整數金額（TWD 無小數），收到 {amount!r}", field=field)
        value = int(dec)

    if value <= 0:
        raise InvalidAmount("金額必須大於 0", field=field)
    if value > MAX_AMOUNT:
        raise InvalidAmount(f"金額超過上限 {MAX_AMOUNT}", field=field)
    return value
