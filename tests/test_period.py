"""定期定額的參數限制。送到綠界才被拒的話，錯誤訊息對 caller 沒有幫助。"""
import pytest

from app.ecpay.subscriptions import validate_period
from app.errors import InvalidField


def test_monthly_defaults():
    assert validate_period("M", 1, 99) == {
        "period_type": "M", "frequency": 1, "exec_times": 99}


def test_lowercase_period_type_is_normalised():
    assert validate_period("m", 1, 12)["period_type"] == "M"


@pytest.mark.parametrize("pt,freq", [("D", 366), ("M", 13), ("Y", 2), ("M", 0)])
def test_frequency_out_of_range(pt, freq):
    with pytest.raises(InvalidField) as e:
        validate_period(pt, freq, 12)
    assert e.value.field == "frequency"


def test_exec_times_minimum_is_two():
    """綠界：「次數不可小於 2 次」。1 次的話那不是定期定額，是單筆。"""
    with pytest.raises(InvalidField) as e:
        validate_period("M", 1, 1)
    assert e.value.field == "exec_times"


def test_yearly_exec_times_cap_is_lower():
    validate_period("Y", 1, 99)
    with pytest.raises(InvalidField):
        validate_period("Y", 1, 100)      # 年週期上限 99，不是 999


def test_bad_period_type():
    with pytest.raises(InvalidField) as e:
        validate_period("W", 1, 12)
    assert e.value.field == "period_type"
