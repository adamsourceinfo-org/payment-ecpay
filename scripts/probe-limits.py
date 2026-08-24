"""量出某個綠界商店各付款方式的金額下限。

綠界的開發文件**沒有公布**這些數字（在合約與費率頁，且依商店而異），
所以與其猜，不如量。量到的值填進 `.cicd/env.<env>` 的 ECPAY_MIN_AMOUNTS，
建單時就會擋下必定走不通的金額。

直接把簽好的表單 POST 給綠界的收銀台並讀回應 —— 這就是瀏覽器做的事。
金額過低時綠界會在頁面上寫「因交易金額低於下限」。不經過我們的服務，
所以不會在資料庫留下垃圾訂單。
"""
import os, re, sys, time, urllib.parse, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.ecpay.checkmac import generate

# 預設量測綠界公開的測試特店。要量正式商店就自己帶環境變數：
#   ECPAY_HASH_KEY=... ECPAY_HASH_IV=... ECPAY_MERCHANT_ID=3017099 \
#   ECPAY_HOST=https://payment.ecpay.com.tw python3 scripts/probe-limits.py
#
# ⚠️ 對正式商店量測會在你的綠界後台留下數十筆**未付款**的探測訂單
#    （不會扣款、不會產生費用，但看得到）。要不要接受由你決定。
import os
KEY = os.environ.get("ECPAY_HASH_KEY", "pwFHCqoQZGmho4w6")
IV = os.environ.get("ECPAY_HASH_IV", "EkRm7iFT261dpevs")
MID = os.environ.get("ECPAY_MERCHANT_ID", "3002607")
URL = os.environ.get("ECPAY_HOST", "https://payment-stage.ecpay.com.tw") \
    + "/Cashier/AioCheckOut/V5"
import secrets, string
AB = string.ascii_uppercase + string.digits

def probe(pay, amount):
    p = {
        "MerchantID": MID,
        "MerchantTradeNo": "P" + "".join(secrets.choice(AB) for _ in range(19)),
        "MerchantTradeDate": time.strftime("%Y/%m/%d %H:%M:%S"),
        "PaymentType": "aio", "TotalAmount": str(amount),
        "TradeDesc": "limit probe", "ItemName": "probe",
        "ReturnURL": "https://example.com/r", "ChoosePayment": pay,
        "EncryptType": "1",
    }
    p["CheckMacValue"] = generate(p, KEY, IV)
    req = urllib.request.Request(URL, data=urllib.parse.urlencode(p).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", "replace")
    if "低於下限" in html or "未提供" in html:
        return "below"
    if "超過上限" in html:
        return "above"
    if "交易失敗" in html:
        m = re.search(r"訊息代碼[：:]\s*(\d+)", html)
        return f"fail:{m.group(1) if m else '?'}"
    return "ok"

def find_min(pay, lo=1, hi=200):
    if probe(pay, hi) != "ok":
        return f"連 {hi} 都不行（{probe(pay, hi)}）"
    if probe(pay, lo) == "ok":
        return f"<= {lo}（沒有下限或極低）"
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if probe(pay, mid) == "ok":
            hi = mid
        else:
            lo = mid
    return hi

for pay in ("Credit", "ATM", "CVS", "BARCODE", "WebATM"):
    print(f"{pay:8} 下限 = {find_min(pay)}", flush=True)
