"""綠界回應的解析。"""
import httpx

from app.ecpay.client import _parse


def _resp(text, ctype):
    return httpx.Response(200, text=text, headers={"content-type": ctype})


def test_json_body_labelled_as_html():
    """**QueryCreditCardPeriodInfo 回 JSON 但標頭寫 text/html。**
    照 Content-Type 判斷會把整包當無結構字串，欄位全部讀不到 ——
    實測踩過：對帳因此把 TotalSuccessTimes 寫成 0，蓋掉正確資料。"""
    body = ('{"MerchantTradeNo":"S1","RtnCode":1,"TotalSuccessTimes":1,'
            '"TotalSuccessAmount":5,"ExecLog":[{"gwsr":14563860,"amount":5}]}')
    d = _parse(_resp(body, "text/html; charset=utf-8"))
    assert d["TotalSuccessTimes"] == 1
    assert d["ExecLog"][0]["gwsr"] == 14563860


def test_form_urlencoded_body():
    d = _parse(_resp("MerchantTradeNo=O1&TradeStatus=1", "text/html"))
    assert d == {"MerchantTradeNo": "O1", "TradeStatus": "1"}


def test_unstructured_error_keeps_the_original_text():
    d = _parse(_resp("Error: something broke", "text/plain"))
    assert d["RtnCode"] == "0" and "something broke" in d["RtnMsg"]


def test_malformed_json_does_not_explode():
    d = _parse(_resp("{not really json", "text/html"))
    assert "RtnMsg" in d
