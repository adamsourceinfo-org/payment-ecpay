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
    # 「商家檢查碼」。只有 `CreditDetail/QueryTrade/V2`（查詢信用卡單筆明細，
    # 會回授權/關帳/取消狀態）需要它，而那支只有正式環境有 ——
    # 所以 dev 不會有值，設計上必須允許缺席。
    credit_check_code: Optional[str]
    allowed_payments: frozenset
    # 付款方式 → 最低金額。綠界**沒有公布**這些數字（在合約與費率頁、依商店而異），
    # 所以不寫死在程式裡，由每個環境自己量、自己設。沒設就不擋。
    min_amounts: dict
    timeout_seconds: float
    public_base_url: Optional[str]
    db_pool_max: int
    db_pool_timeout_seconds: float
    # 事件推送。**兩把機密缺席時不啟動失敗，而是關閉推送** ——
    # 第一次部署時 secret 還沒建，硬性必填會讓服務起不來，
    # 而沒有推送的服務仍然是完全可用的服務（GET /v1/events 還在）。
    webhook_signing_key: Optional[str]
    internal_key: Optional[str]
    webhook_timeout_seconds: float
    webhook_enqueue_timeout_seconds: float
    tasks_queue_prefix: str
    tasks_location: str
    # 示範商店的 caller 身分。**沒設就整組 /demo 端點 404** ——
    # 它只寫進 .cicd/env.dev，prod 沒有。見 app/routers/demo.py。
    demo_caller_id: Optional[str]
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
        """信用卡請退款。

        綠界文件寫「測試環境：因無法提供實際授權，故無法使用此 API」，
        **但那是錯的** —— 實測 stage 上這支端點存在且可用：對一筆真實授權過的
        stage 訂單送 `Action=N` 會回 `RtnCode=1 Succeeded.`，送 `Action=R`
        則回 `10000002 更新失敗.(error_amount_R)`。
        （stage 其實提供得了授權，走的是模擬 3D 驗證。）

        所以不依環境擋 —— 擋了反而讓退款這條路在 dev 永遠測不到。
        """
        return f"{self.ecpay_host}/CreditDetail/DoAction"

    @property
    def push_configured(self) -> bool:
        """兩把都要有才推得動：一把用來簽給 caller，一把用來認自己的內部端點。"""
        return bool(self.webhook_signing_key and self.internal_key)

    @property
    def db_configured(self) -> bool:
        return bool(self.db_instance and self.db_user and self.db_name)


def _required(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"缺少必要環境變數 {name}")
    return v


def _optional(name: str):
    """可以缺席的機密。**一定要 strip。**

    ⚠️ Secret Manager 存的是位元組，而最自然的建立方式
    （`python3 -c 'print(...)' | gcloud secrets create --data-file=-`）
    會把**換行也存進去**。Cloud Run 原樣注入，於是值變成 "abc\n"。

    症狀依用途而異，而且都很難查：
    - `INTERNAL_KEY` → 內部端點永遠回 401（比對的另一邊是 trim 過的）
    - `WEBHOOK_SIGNING_KEY` → 簽章能算但與別人算的不同
    - `ECPAY_CREDIT_CHECK_CODE` → CheckMacValue 對不上，綠界只說驗證失敗

    `_required()` 本來就 strip，所以 hash_key/hash_iv 一直沒事 ——
    這幾個可選的必須跟上，否則同一個 repo 裡兩種行為。
    """
    return (os.environ.get(name) or "").strip() or None


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
        credit_check_code=_optional("ECPAY_CREDIT_CHECK_CODE"),
        allowed_payments=payments,
        min_amounts=mins,
        timeout_seconds=float(os.environ.get("ECPAY_TIMEOUT_SECONDS", "10")),
        # 沒設就由請求自身的 scheme+host 推導 —— 這樣第一次部署不會卡在
        # 「還沒有網址就填不了回呼網址」的雞生蛋。
        public_base_url=(os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/") or None,
        db_pool_max=int(os.environ.get("DB_POOL_MAX", "3")),
        # 借不到連線就等這麼久，然後 PoolExhausted → 503。
        # 不做成無限等：見 app/db.py 的 PoolExhausted。
        db_pool_timeout_seconds=float(
            os.environ.get("DB_POOL_TIMEOUT_SECONDS", "5")),
        webhook_signing_key=_optional("WEBHOOK_SIGNING_KEY"),
        internal_key=_optional("INTERNAL_KEY"),
        webhook_timeout_seconds=float(
            os.environ.get("WEBHOOK_TIMEOUT_SECONDS", "10")),
        # 建 task 是一次對外 HTTP，而它就在回綠界 1|OK 的路徑上。
        # Cloud Tasks API 一慢，ACK 就慢，綠界超時就重送 —— 事故當下再加一輪流量。
        # 所以給它一個短 timeout，逾時就交給 sweep 補。
        webhook_enqueue_timeout_seconds=float(
            os.environ.get("WEBHOOK_ENQUEUE_TIMEOUT_SECONDS", "2")),
        # per-caller queue 是 {prefix}-{消毒後的 caller}；prefix 本身是退路用的共用 queue。
        tasks_queue_prefix=os.environ.get(
            "TASKS_QUEUE_PREFIX", "payment-ecpay-deliveries"),
        tasks_location=os.environ.get("TASKS_LOCATION", "asia-east1"),
        demo_caller_id=_optional("DEMO_CALLER_ID"),
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
