"""推送目標網址的驗證。

**一個能讓服務去打內網的功能就是一個 SSRF。** 這支端點是 caller 用自己的
API key 指定的，所以風險有界 —— 但有界不等於沒有，而且服務跑在 Cloud Run 上，
metadata server 就在 169.254.169.254。

⚠️ **註冊時的字面檢查擋不住全部，這一點要誠實。**
caller 完全可以註冊一個公開網域，讓它的 A record 指到 `169.254.169.254`，
或註冊之後才改 DNS。所以**送出當下還要再擋一次**（`assert_public`），
而且投遞一律不跟隨 redirect。即便如此仍有 DNS TOCTOU 的殘餘風險 ——
我們接受它，但不用「已擋 SSRF」的語氣描述它。

檔名刻意不是 urls.py：`app/urls.py` 是綠界回呼網址的推導，兩者無關。
"""
import ipaddress
import socket
from urllib.parse import urlsplit

from app.errors import InvalidField

# 這些主機名一律拒絕，不必等 DNS。`.internal` 涵蓋 metadata.google.internal
# 以及 GCE 的內部 DNS 網域。
_BLOCKED_SUFFIXES = (".internal", ".local")
_BLOCKED_NAMES = frozenset({"localhost", "metadata.google.internal"})


def _blocked_ip(ip_text: str) -> bool:
    """loopback／私有／link-local／保留位址一律擋。

    `is_private` 在 Python 的 ipaddress 裡已經涵蓋 10/8、172.16/12、192.168/16、
    127/8 與 fc00::/7；link-local（169.254/16、fe80::/10）另外判，
    因為 metadata server 就在那裡。
    """
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def validate(url: str) -> str:
    """註冊時的檢查。回正規化後的網址，不合格就丟 InvalidField（→ 400）。"""
    if not url or not url.strip():
        raise InvalidField("url 不能是空的", "url")
    url = url.strip()
    parts = urlsplit(url)

    if parts.scheme != "https":
        raise InvalidField("只收 https:// 的網址", "url")
    if parts.username or parts.password:
        # 帶帳密的網址是一種混淆真實 host 的老技巧，而且那組帳密會進我們的 log
        raise InvalidField("網址不可以帶帳號密碼", "url")

    host = (parts.hostname or "").lower()
    if not host:
        raise InvalidField("網址少了主機名", "url")
    if host in _BLOCKED_NAMES or host.endswith(_BLOCKED_SUFFIXES):
        raise InvalidField(f"不接受內部主機名：{host}", "url")
    if _blocked_ip(host):
        raise InvalidField(f"不接受內部位址：{host}", "url")
    return url


def assert_public(url: str) -> None:
    """送出當下再擋一次：解析 host，任何一個解析結果落在內網就不送。

    註冊時只看得到字面，這裡看得到 DNS 的答案 —— 「公開網域指到 169.254.169.254」
    只有在這裡擋得掉。
    """
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        raise InvalidField("網址少了主機名", "url")
    if host in _BLOCKED_NAMES or host.endswith(_BLOCKED_SUFFIXES):
        raise InvalidField(f"不接受內部主機名：{host}", "url")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise InvalidField(f"網址解析不到：{host}（{exc}）", "url") from exc
    for info in infos:
        addr = info[4][0]
        if _blocked_ip(addr):
            raise InvalidField(
                f"{host} 解析到內部位址 {addr}，不送", "url")
