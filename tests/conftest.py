import pytest

import app.config as cfg
from app.ecpay import checkmac

# 綠界官方文件公布的測試特店金鑰（全球開發者共用，不是機密）
TEST_KEY = "pwFHCqoQZGmho4w6"
TEST_IV = "EkRm7iFT261dpevs"


class FakeSettings:
    """不是 frozen dataclass，測試才能逐項覆寫。"""
    app_env = "test"
    app_version = "test"
    ecpay_env = "stage"
    ecpay_host = "https://payment-stage.ecpay.com.tw"
    aio_checkout_url = "https://payment-stage.ecpay.com.tw/Cashier/AioCheckOut/V5"
    query_trade_url = "https://payment-stage.ecpay.com.tw/Cashier/QueryTradeInfo/V5"
    query_period_url = "https://payment-stage.ecpay.com.tw/Cashier/QueryCreditCardPeriodInfo"
    period_action_url = "https://payment-stage.ecpay.com.tw/Cashier/CreditCardPeriodAction"
    do_action_url = "https://payment.ecpay.com.tw/CreditDetail/DoAction"
    merchant_id = "3002607"
    credit_check_code = None      # 測試環境沒有（那支查詢只有正式環境有）
    hash_key = TEST_KEY
    hash_iv = TEST_IV
    allowed_payments = frozenset({"Credit", "WebATM", "ATM", "CVS", "BARCODE"})
    # 測試特店實測值（見 scripts/probe-limits.py）
    min_amounts = {"ATM": 2, "CVS": 27, "BARCODE": 16, "WebATM": 2}
    timeout_seconds = 5.0
    public_base_url = None
    db_pool_max = 3
    log_level = "debug"
    db_instance = "proj:region:inst"
    db_user = "run-runtime@proj.iam"
    db_name = "payment_ecpay"
    db_configured = True


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch):
    """塞進 app.config 的單例，而不是 patch get_settings。

    各模組是 `from app.config import get_settings` 綁函式物件的，
    patch 函式只會補到被列舉到的模組；改動單例則所有 importer 都吃得到。
    """
    s = FakeSettings()
    monkeypatch.setattr(cfg, "_settings", s)
    return s


def sign(params: dict) -> dict:
    """幫測試用的表單補上正確的檢查碼 —— 模擬綠界送過來的樣子。"""
    out = dict(params)
    out["CheckMacValue"] = checkmac.generate(params, TEST_KEY, TEST_IV)
    return out
