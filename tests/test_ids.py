"""MerchantTradeNo 的限制是硬的：20 碼、英數。
而且 dev 的測試商店全球共用，流水號會撞到陌生人的訂單。"""
import pytest

from app.ids import checkout_token, merchant_trade_no


def test_length_and_charset():
    for prefix in ("O", "S"):
        n = merchant_trade_no(prefix)
        assert len(n) == 20                    # 綠界上限，超過整張單被拒
        assert n.isalnum() and n.isupper()
        assert n[0] == prefix


def test_high_entropy():
    """1000 次不重複。流水號在共用測試商店上是會踩到別人的。"""
    seen = {merchant_trade_no("O") for _ in range(1000)}
    assert len(seen) == 1000


@pytest.mark.parametrize("bad", ["", "OO", "o", "1"])
def test_prefix_must_be_single_upper_letter(bad):
    with pytest.raises(ValueError):
        merchant_trade_no(bad)


def test_checkout_token_is_not_guessable():
    a, b = checkout_token(), checkout_token()
    assert a != b and len(a) >= 32
