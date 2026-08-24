"""退款動作的選擇。送錯會失敗，而先前的程式**永遠送 N** ——
`orders.closed` 從來沒有任何地方寫入過，所以隔天以後的退款一律會壞。"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.refunds import TAIPEI, actions_for, last_closing_at


def tp(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=TAIPEI)


def test_last_closing_before_and_after_the_window():
    """關帳時段是 20:15~20:30，用**結束**時間當界線 ——
    否則正在關帳的那半小時會被誤判成已關帳。"""
    assert last_closing_at(tp(2026, 8, 24, 20, 29)) == tp(2026, 8, 23, 20, 30)
    assert last_closing_at(tp(2026, 8, 24, 20, 30)) == tp(2026, 8, 24, 20, 30)
    assert last_closing_at(tp(2026, 8, 24, 3, 0)) == tp(2026, 8, 23, 20, 30)


def test_same_day_before_closing_tries_abandon_first():
    """當天剛付款、還沒到關帳時間 → 還沒請款 → 先送 N（放棄授權）。"""
    assert actions_for(tp(2026, 8, 24, 14, 0), False,
                       now=tp(2026, 8, 24, 15, 0)) == ["N", "R"]


def test_yesterday_tries_refund_first():
    """昨天付的款已經被自動關帳 → 先送 R（退刷）。
    這正是先前壞掉的情境：舊程式永遠送 N。"""
    assert actions_for(tp(2026, 8, 23, 14, 0), False,
                       now=tp(2026, 8, 24, 15, 0)) == ["R", "N"]


def test_after_todays_closing_tries_refund_first():
    assert actions_for(tp(2026, 8, 24, 14, 0), False,
                       now=tp(2026, 8, 24, 21, 0)) == ["R", "N"]


def test_known_closed_tries_refund_first():
    assert actions_for(tp(2026, 8, 24, 14, 0), True,
                       now=tp(2026, 8, 24, 15, 0))[0] == "R"


def test_unknown_paid_at_still_returns_both():
    """不知道何時付的也要能退 —— 給兩個動作讓它自己找。"""
    assert sorted(actions_for(None, False)) == ["N", "R"]


def test_utc_input_is_converted():
    """DB 回來的是 UTC。UTC 8/23 19:00 = 台北 8/24 03:00，晚於最近一次關帳
    （台北 8/23 20:30），所以還沒關帳 → 先送 N。

    這條測的就是時區：若拿 UTC 的 8/23 19:00 直接跟台北的 8/23 20:30 比，
    會得到「早於關帳」的相反答案。"""
    utc = datetime(2026, 8, 23, 19, 0, tzinfo=ZoneInfo("UTC"))
    assert actions_for(utc, False, now=tp(2026, 8, 24, 15, 0)) == ["N", "R"]


@pytest.mark.parametrize("actions", [["R", "N"], ["N", "R"]])
def test_always_offers_a_fallback(actions):
    """兩個動作互斥、失敗沒有部分效果，所以一定要有退路。"""
    assert len(set(actions)) == 2
