"""檢查碼的官方向量。這是整個綠界介接的信任根 —— 這裡錯，
後面每一個查不出原因的錯誤都會先被懷疑到這裡。"""
from app.ecpay.checkmac import _dotnet_urlencode, generate, verify

# 綠界「檢查碼機制說明」文件上的完整範例
DOC_KEY = "pwFHCqoQZGmho4w6"
DOC_IV = "EkRm7iFT261dpevs"
DOC_PARAMS = {
    "MerchantID": "3002607",
    "MerchantTradeNo": "ecpay20230312153023",
    "MerchantTradeDate": "2023/03/12 15:30:23",
    "PaymentType": "aio",
    "TotalAmount": "30000",
    "TradeDesc": "促銷方案",
    "ItemName": "Apple iphone 15",
    "ReturnURL": "https://www.ecpay.com.tw/receive.php",
    "ChoosePayment": "ALL",
    "EncryptType": "1",
}
DOC_EXPECTED = "6C51C9E6888DE861FD62FB1DD17029FC742634498FD813DC43D4243B5685B840"


def test_official_vector():
    assert generate(DOC_PARAMS, DOC_KEY, DOC_IV) == DOC_EXPECTED


def test_official_vector_intermediate_lowercase_string():
    """文件第 4 步的字串也對得上，錯的時候才知道是哪一步壞的。"""
    raw = ("HashKey=pwFHCqoQZGmho4w6&ChoosePayment=ALL&EncryptType=1"
           "&ItemName=Apple iphone 15&MerchantID=3002607"
           "&MerchantTradeDate=2023/03/12 15:30:23"
           "&MerchantTradeNo=ecpay20230312153023&PaymentType=aio"
           "&ReturnURL=https://www.ecpay.com.tw/receive.php"
           "&TotalAmount=30000&TradeDesc=促銷方案&HashIV=EkRm7iFT261dpevs")
    got = _dotnet_urlencode(raw)
    assert got.startswith("hashkey%3dpwfhcqoqzgmho4w6%26choosepayment%3dall")
    assert got.endswith("%26hashiv%3dekrm7ift261dpevs")
    assert "itemname%3dapple+iphone+15" in got          # 空白是 +
    assert "%2f%2fwww.ecpay.com.tw%2freceive.php" in got  # 斜線要編碼
    assert "%e4%bf%83%e9%8a%b7%e6%96%b9%e6%a1%88" in got  # 中文 UTF-8


def test_dotnet_encoding_differences():
    """quote_plus 與 .NET UrlEncode 的差集。官方向量裡沒有這些字元，
    所以要另外釘住，否則哪天 ItemName 出現 `!` 就會靜靜地簽錯。"""
    assert _dotnet_urlencode("!*()") == "!*()"        # .NET 不編碼
    assert _dotnet_urlencode("~") == "%7e"            # .NET 會編碼
    assert _dotnet_urlencode("-_.") == "-_."          # 兩邊都不編碼
    assert _dotnet_urlencode("a b") == "a+b"


def test_sorting_is_case_insensitive():
    """排序不分大小寫。若用 ASCII 排序，大寫全部會排到小寫前面，
    只要有一個小寫開頭的參數就會簽錯。"""
    a = generate({"aKey": "1", "BKey": "2"}, DOC_KEY, DOC_IV)
    b = generate({"BKey": "2", "aKey": "1"}, DOC_KEY, DOC_IV)
    assert a == b


def test_verify_roundtrip():
    params = dict(DOC_PARAMS)
    params["CheckMacValue"] = generate(DOC_PARAMS, DOC_KEY, DOC_IV)
    assert verify(params, DOC_KEY, DOC_IV) is True


def test_verify_rejects_tampering():
    params = dict(DOC_PARAMS)
    params["CheckMacValue"] = generate(DOC_PARAMS, DOC_KEY, DOC_IV)
    params["TotalAmount"] = "1"                        # 改金額
    assert verify(params, DOC_KEY, DOC_IV) is False


def test_verify_rejects_missing_checkmac():
    """沒帶檢查碼就是不合法，不是「沒有就跳過驗證」。"""
    assert verify(dict(DOC_PARAMS), DOC_KEY, DOC_IV) is False
