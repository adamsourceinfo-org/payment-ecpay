"""綠界只收 TWD 整數。這裡的規則跟 payment-paypal 剛好相反 ——
那邊要處理小數位數，這邊是「一律不准有小數」。"""
import pytest

from app.errors import InvalidAmount
from app.money import MAX_AMOUNT, validate_amount


@pytest.mark.parametrize("given,expected", [
    (100, 100), ("100", 100), ("  250  ", 250), (1, 1),
])
def test_accepts_integers(given, expected):
    assert validate_amount(given) == expected


@pytest.mark.parametrize("bad", ["100.50", "0.5", "1.00"])
def test_rejects_decimals(bad):
    """"1.00" 也要拒絕。接受它會讓 caller 以為這裡支援小數，
    然後某天傳 "100.50" 被靜靜地截掉。"""
    with pytest.raises(InvalidAmount):
        validate_amount(bad)


@pytest.mark.parametrize("bad", [0, -1, "0", "-5"])
def test_rejects_non_positive(bad):
    with pytest.raises(InvalidAmount):
        validate_amount(bad)


@pytest.mark.parametrize("bad", ["abc", "", "1e5x", None])
def test_rejects_garbage(bad):
    with pytest.raises(InvalidAmount):
        validate_amount(bad)


def test_rejects_bool():
    """bool 是 int 的子類，不擋的話 True 會變成金額 1。"""
    with pytest.raises(InvalidAmount):
        validate_amount(True)


def test_rejects_over_max():
    with pytest.raises(InvalidAmount):
        validate_amount(MAX_AMOUNT + 1)


def test_error_names_the_field():
    """caller 要知道該改哪個欄位，只給一句訊息他得自己猜。"""
    with pytest.raises(InvalidAmount) as e:
        validate_amount("1.5", field="period_amount")
    assert e.value.as_detail() == {
        "error": "invalid_amount", "field": "period_amount",
        "message": e.value.args[0]}
