"""定期定額：參數驗證、查詢、終止。

綠界沒有「方案（plan）」這種物件 —— 週期參數直接帶在建單表單上。
所以本服務也不造一層 plan 抽象；訂閱建立時就把週期寫進自己的表。
"""
import time

from app.config import get_settings
from app.ecpay import client
from app.errors import InvalidField

# PeriodType → Frequency 的合法範圍（綠界規定）
FREQUENCY_RANGE = {
    "D": (1, 365),
    "M": (1, 12),
    "Y": (1, 1),
}
# PeriodType → ExecTimes 上限。下限一律 2（「次數不可小於 2 次」）。
EXEC_TIMES_MAX = {"D": 999, "M": 999, "Y": 99}
EXEC_TIMES_MIN = 2

# 本服務固定用的期數。綠界沒有「無限期直到取消」，ExecTimes 必填；
# 用月週期的上限（999 期 ≈ 83 年）讓「到取消為止」成為唯一語意。
# 不開放 caller 設定 —— 填了有限數字遲早有人在第 N+1 個月才發現訂閱停了。
FIXED_EXEC_TIMES = 999


def validate_period(period_type: str, frequency: int, exec_times: int) -> dict:
    """在進門就擋。送到綠界才被拒的話，錯誤訊息對 caller 沒有幫助。"""
    pt = (period_type or "").strip().upper()
    if pt not in FREQUENCY_RANGE:
        raise InvalidField(
            f"period_type 只能是 {sorted(FREQUENCY_RANGE)}，收到 {period_type!r}",
            field="period_type")

    lo, hi = FREQUENCY_RANGE[pt]
    if not isinstance(frequency, int) or not (lo <= frequency <= hi):
        raise InvalidField(
            f"period_type={pt} 時 frequency 必須在 {lo}~{hi}，收到 {frequency!r}",
            field="frequency")

    hi_times = EXEC_TIMES_MAX[pt]
    if not isinstance(exec_times, int) or not (EXEC_TIMES_MIN <= exec_times <= hi_times):
        raise InvalidField(
            f"period_type={pt} 時 exec_times 必須在 {EXEC_TIMES_MIN}~{hi_times}，"
            f"收到 {exec_times!r}", field="exec_times")

    return {"period_type": pt, "frequency": frequency, "exec_times": exec_times}


def query(merchant_trade_no: str) -> dict:
    """查定期定額。回傳含 ExecLog（每期扣款明細）。

    這是**唯一**能分辨「某筆交易是不是定期定額」的途徑 ——
    首期的回呼跟一次性付款長得一模一樣。
    """
    s = get_settings()
    return client.post(s.query_period_url, {
        "MerchantID": s.merchant_id,
        "MerchantTradeNo": merchant_trade_no,
        "TimeStamp": str(int(time.time())),
    })


def cancel(merchant_trade_no: str) -> dict:
    """終止後續扣款。

    **成功後無法重新啟用** —— 綠界文件：「終止交易成功後，無法重新啟用，
    只能重新發動新定期定額訂單進行交易。」所以呼叫端不該提供「恢復」。

    已經收到的錢不會退。取消 ≠ 退款，權限該給到期末就給到期末。
    """
    s = get_settings()
    data = client.post(s.period_action_url, {
        "MerchantID": s.merchant_id,
        "MerchantTradeNo": merchant_trade_no,
        "Action": "Cancel",
        "TimeStamp": str(int(time.time())),
    })
    return client.require_success(data)
