"""退款要送哪個動作。

綠界的信用卡退款分兩種，而且送錯會失敗：

- `R` 退刷 —— 訂單**已關帳**（錢已經請款）時用
- `N` 放棄授權 —— 訂單**尚未關帳**（只授權還沒請款）時用

關帳由綠界**每日 20:15~20:30（台北）自動執行**（商店可關閉此設定，
3017099 是開啟的）。所以「當天剛付款」通常還沒關帳，「昨天以前付款」通常已關帳。

這裡刻意**不去查綠界的授權明細**來判斷：那支 API（`CreditDetail/QueryTrade/V2`）
同樣只有正式環境有，而且需要一個額外的 `CreditCheckCode` 機密。
為了決定一個二選一的參數而多引進一個機密不划算。

改成：依關帳時間推測先送哪一個，**被拒就自動改送另一個**。
兩個動作互斥、失敗不會有部分效果，所以重試是安全的；而且結果會回寫
`orders.closed`，同一筆的後續部分退款就直接命中。
"""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")
# 綠界的自動關帳時段結束時間。用結束時間而不是開始時間，是為了不要把
# 正在關帳中的那半小時誤判成已關帳。
CLOSING_DONE = time(20, 30)


def last_closing_at(now: datetime) -> datetime:
    """最近一次自動關帳完成的時刻（台北時間）。"""
    now = now.astimezone(TAIPEI)
    today = datetime.combine(now.date(), CLOSING_DONE, tzinfo=TAIPEI)
    return today if now >= today else today - timedelta(days=1)


def actions_for(paid_at: datetime, closed: bool, now: datetime = None) -> list:
    """回傳要依序嘗試的動作。第一個是最可能對的。"""
    if closed:
        return ["R", "N"]
    if paid_at is None:
        # 不知道何時付的 —— 用比較常見的情況當首選
        return ["R", "N"]
    now = now or datetime.now(TAIPEI)
    return ["R", "N"] if paid_at.astimezone(TAIPEI) < last_closing_at(now) else ["N", "R"]
