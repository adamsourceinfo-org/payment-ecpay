"""所有環境變數在這裡讀一次、驗一次。這是唯一碰 os.environ 的模組。

缺少必要變數就啟動失敗 —— Cloud Run 起不來、CI 的 smoke 紅燈、當場知道，
比上線後第一筆爭議時才發現好。
"""
import os
from dataclasses import dataclass
from typing import Optional

# base URL 由 ECPAY_ENV 推導，不做成設定。可設定就有設錯的餘地，
# 而「prod 指到 stage」的代價是以為在收錢但沒有 —— 舊服務就發生過。
ECPAY_HOSTS = {
    "stage": "https://payment-stage.ecpay.com.tw",
    "production": "https://payment.ecpay.com.tw",
}

# 綠界支援但本服務不開放的付款方式（需特約賣家或未申請）刻意不列在這裡；
# 每個環境實際開通了什麼由 ECPAY_ALLOWED_PAYMENTS 決定，程式不寫死。
KNOWN_PAYMENTS = frozenset({
    "Credit", "WebATM", "ATM", "CVS", "BARCODE",
    "ApplePay", "TWQR", "BNPL", "WeiXin", "ALL",
})


@dataclass(frozen=True)
class Settings:
    app_env: str
    app_version: str
    ecpay_env: str
    merchant_id: str
    hash_key: str
    hash_iv: str
    allowed_payments: frozenset
    # 付款方式 → 最低金額。綠界**沒有公布**這些數字（在合約與費率頁、依商店而異），
    # 所以不寫死在程式裡，由每個環境自己量、自己設。沒設就不擋。
    min_amounts: dict
    timeout_seconds: float
    public_base_url: Optional[str]
    db_pool_max: int
    log_level: str
    db_instance: Optional[str]
    db_user: Optional[str]
    db_name: Optional[str]

    @property
    def ecpay_host(self) -> str:
        return ECPAY_HOSTS[self.ecpay_env]

    @property
    def aio_checkout_url(self) -> str:
        return f"{self.ecpay_host}/Cashier/AioCheckOut/V5"

    @property
    def query_trade_url(self) -> str:
        return f"{self.ecpay_host}/Cashier/QueryTradeInfo/V5"

    @property
    def query_period_url(self) -> str:
        return f"{self.ecpay_host}/Cashier/QueryCreditCardPeriodInfo"

    @property
    def period_action_url(self) -> str:
        return f"{self.ecpay_host}/Cashier/CreditCardPeriodAction"

    @property
    def do_action_url(self) -> str:
        """信用卡請退款。**測試環境不存在這支 API** —— 綠界文件明說
        「因無法提供實際授權，故無法使用此 API」。stage 上呼叫必定失敗，
        這是上游的限制，不是本服務的 bug。"""
        return f"{self.ecpay_host}/CreditDetail/DoAction"

    @property
    def do_action_available(self) -> bool:
        return self.ecpay_env == "production"

    @property
    def db_configured(self) -> bool:
        return bool(self.db_instance and self.db_user and self.db_name)


def _required(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"缺少必要環境變數 {name}")
    return v


def load_settings() -> Settings:
    ecpay_env = _required("ECPAY_ENV")
    if ecpay_env not in ECPAY_HOSTS:
        raise RuntimeError(
            f"ECPAY_ENV 只能是 {sorted(ECPAY_HOSTS)}，收到 {ecpay_env!r}")

    payments = frozenset(
        p.strip() for p in
        os.environ.get("ECPAY_ALLOWED_PAYMENTS", "Credit").split(",")
        if p.strip()
    )
    if not payments:
        raise RuntimeError("ECPAY_ALLOWED_PAYMENTS 不能是空的")
    unknown = payments - KNOWN_PAYMENTS
    if unknown:
        raise RuntimeError(
            f"ECPAY_ALLOWED_PAYMENTS 有綠界不認得的值：{sorted(unknown)}；"
            f"可用：{sorted(KNOWN_PAYMENTS)}")

    mins = {}
    for item in os.environ.get("ECPAY_MIN_AMOUNTS", "").split(","):
        item = item.strip()
        if not item:
            continue
        method, _, value = item.partition(":")
        method = method.strip()
        if method not in KNOWN_PAYMENTS or not value.strip().isdigit():
            raise RuntimeError(
                f"ECPAY_MIN_AMOUNTS 格式錯誤：{item!r}，要 <付款方式>:<整數>")
        mins[method] = int(value)

    return Settings(
        app_env=os.environ.get("APP_ENV", "unknown"),
        app_version=os.environ.get("APP_VERSION", "(dev build)"),
        ecpay_env=ecpay_env,
        merchant_id=_required("ECPAY_MERCHANT_ID"),
        # HashKey 與 HashIV **兩個都是機密**（跟 PayPal 的 client id 不同，
        # 那個是半公開的）。兩個都走 Secret Manager。
        hash_key=_required("ECPAY_HASH_KEY"),
        hash_iv=_required("ECPAY_HASH_IV"),
        allowed_payments=payments,
        min_amounts=mins,
        timeout_seconds=float(os.environ.get("ECPAY_TIMEOUT_SECONDS", "10")),
        # 沒設就由請求自身的 scheme+host 推導 —— 這樣第一次部署不會卡在
        # 「還沒有網址就填不了回呼網址」的雞生蛋。
        public_base_url=(os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/") or None,
        db_pool_max=int(os.environ.get("DB_POOL_MAX", "3")),
        log_level=os.environ.get("LOG_LEVEL", "info"),
        # 這三個由 CI 依部署目標推導注入，寫進 .cicd/env.* 會被 verify 擋下
        db_instance=os.environ.get("INSTANCE_CONNECTION_NAME") or None,
        db_user=os.environ.get("DB_USER") or None,
        db_name=os.environ.get("DB_NAME") or None,
    )


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reset_settings_for_tests() -> None:
    global _settings
    _settings = None
