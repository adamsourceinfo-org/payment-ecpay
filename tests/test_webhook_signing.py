"""簽章與網址驗證。

簽章向量是**兩個服務共用**的：payment-ecpay 與 payment-paypal 必須算出
逐字相同的 hex。各自產生一組「應該相同」的向量，等於沒有向量 ——
所以這一組是唯一真相，另一個服務逐字複製，README 也放同一組讓 caller 驗自己的實作。
"""
import pytest

from app.errors import InvalidField
from app.webhooks import signing, targets

# --- 共用向量 ---------------------------------------------------------
# 兩個服務、README、caller 的實作，全部要對得上這一組。
VECTOR_KEY = "test-signing-key"
VECTOR_CALLER = "line-translate-bot"
VECTOR_T = 1756090455
VECTOR_BODY = b'{"id":1234,"event_type":"payment.return"}'


def test_密鑰由caller_id推導且穩定(fake_settings):
    fake_settings.webhook_signing_key = VECTOR_KEY
    a = signing.secret_for(VECTOR_CALLER)
    b = signing.secret_for(VECTOR_CALLER)
    assert a == b
    assert len(a) == 64 and a == a.lower()


def test_不同caller拿到不同密鑰(fake_settings):
    fake_settings.webhook_signing_key = VECTOR_KEY
    assert signing.secret_for("caller-a") != signing.secret_for("caller-b")


def test_換掉簽章金鑰等於換掉所有caller的密鑰(fake_settings):
    """這是刻意的取捨，寫成測試讓它不會被誤當成 bug 修掉。
    逐 caller 輪替換來的是資料庫裡一欄要保護的明文，不划算。"""
    fake_settings.webhook_signing_key = "key-one"
    before = signing.secret_for(VECTOR_CALLER)
    fake_settings.webhook_signing_key = "key-two"
    assert signing.secret_for(VECTOR_CALLER) != before


def test_簽章向量(fake_settings):
    """固定 key / caller / t / body → 固定 hex。**兩個服務必須一致。**"""
    fake_settings.webhook_signing_key = VECTOR_KEY
    secret = signing.secret_for(VECTOR_CALLER)
    assert secret == (
        "a6b1f5b99eceb78d8161ce309c2aaa88"
        "4331bfae5d0f0b438458795953a38a4c")
    assert signing.header(secret, VECTOR_T, VECTOR_BODY) == (
        "t=1756090455,v1="
        "5b1967f64135c6dff853b169effe4421cf9a1e0dff72125008c789f3d4bd2b39")


def test_簽的是原始bytes不是重新序列化的json(fake_settings):
    """重新 json.dumps 出來的字串跟原文不保證逐位元組相同（鍵的順序、
    Unicode 跳脫、空白都可能不同）—— 而且只在有中文的 payload 上才發作。"""
    fake_settings.webhook_signing_key = VECTOR_KEY
    secret = signing.secret_for(VECTOR_CALLER)
    compact = '{"msg":"付款成功"}'.encode()
    spaced = '{"msg": "付款成功"}'.encode()
    escaped = '{"msg":"\\u4ed8\\u6b3e\\u6210\\u529f"}'.encode()
    sigs = {signing.signature(secret, 1, b) for b in (compact, spaced, escaped)}
    assert len(sigs) == 3        # 三種寫法、三個簽章 —— 所以只能簽原文


def test_時間戳進簽章_換了t就換了簽章(fake_settings):
    fake_settings.webhook_signing_key = VECTOR_KEY
    secret = signing.secret_for(VECTOR_CALLER)
    assert (signing.signature(secret, 1, VECTOR_BODY)
            != signing.signature(secret, 2, VECTOR_BODY))


# --- 網址驗證 ---------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://caller.example/hook",                 # 非 https
    "https://user:pw@caller.example/hook",        # 帶帳密
    "https://127.0.0.1/hook",
    "https://[::1]/hook",
    "https://10.0.0.1/hook",
    "https://172.16.0.1/hook",
    "https://192.168.1.1/hook",
    "https://169.254.169.254/computeMetadata/v1/",   # ⚠️ metadata server
    "https://metadata.google.internal/hook",
    "https://anything.internal/hook",
    "https://localhost/hook",
    "https://0.0.0.0/hook",
    "",
])
def test_註冊時擋掉的網址(url):
    with pytest.raises(InvalidField):
        targets.validate(url)


def test_正常的網址收下來(fake_settings):
    u = "https://line-translate-bot-xxxx.a.run.app/pay/events"
    assert targets.validate(f"  {u}  ") == u


def test_送出當下再擋一次_公開網域指到內網也要擋(monkeypatch):
    """註冊時只看得到字面。「公開網域的 A record 指到 169.254.169.254」
    只有在送出當下解析得出來 —— 那正是註冊時檢查擋不掉的那一種。"""
    monkeypatch.setattr(
        targets.socket, "getaddrinfo",
        lambda host, port: [(2, 1, 6, "", ("169.254.169.254", 0))])
    with pytest.raises(InvalidField, match="內部位址"):
        targets.assert_public("https://totally-public.example/hook")


def test_送出當下解析到公開位址就放行(monkeypatch):
    monkeypatch.setattr(
        targets.socket, "getaddrinfo",
        lambda host, port: [(2, 1, 6, "", ("34.120.1.1", 0))])
    targets.assert_public("https://totally-public.example/hook")


def test_解析不到也不送(monkeypatch):
    import socket as _s

    def boom(host, port):
        raise _s.gaierror("Name or service not known")

    monkeypatch.setattr(targets.socket, "getaddrinfo", boom)
    with pytest.raises(InvalidField, match="解析不到"):
        targets.assert_public("https://nope.example/hook")
