"""一次性付款：建單參數、查詢、退款。

綠界的「建單」不是一次 API 呼叫 —— 是產生一組要讓使用者的瀏覽器
POST 過去的表單參數。所以這裡只組參數並簽章，沒有網路往返。
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.ecpay import client
from app.errors import ECPayError

# 綠界的時間欄位是台北時間。容器跑 UTC，用系統時間會整整差 8 小時，
# 而症狀是「訂單建立時間看起來很怪」而不是明確的錯誤。
TAIPEI = ZoneInfo("Asia/Taipei")


def now_taipei() -> str:
    return datetime.now(TAIPEI).strftime("%Y/%m/%d %H:%M:%S")


def _truncate(text: str, limit: int) -> str:
    """綠界對這些欄位有長度上限，超過會整張單被拒。
    截斷比讓整筆交易失敗好 —— 這些欄位只是給人看的說明。"""
    text = (text or "").replace("&", "＆").replace("=", "＝")
    return text[:limit]


def checkout_fields(*, merchant_trade_no: str, amount: int, item_name: str,
                    trade_desc: str, choose_payment: str, base_url: str,
                    order_result_url: str = None, custom_fields: dict = None,
                    period: dict = None) -> dict:
    """組出要 POST 到綠界的完整表單（含 CheckMacValue）。

    `period` 有值時這張單就是定期定額。**綠界規定 TotalAmount 必須等於
    PeriodAmount**，所以這裡不讓呼叫端各給一個值。
    """
    s = get_settings()
    params = {
        "MerchantID": s.merchant_id,
        "MerchantTradeNo": merchant_trade_no,
        "MerchantTradeDate": now_taipei(),
        "PaymentType": "aio",
        "TotalAmount": str(amount),
        "TradeDesc": _truncate(trade_desc or "payment", 200),
        "ItemName": _truncate(item_name or "payment", 400),
        "ReturnURL": f"{base_url}/ecpay/return",
        "ChoosePayment": choose_payment,
        "EncryptType": "1",
        # 取號結果（ATM／超商）走另一條路 —— 那時候還沒有付款，
        # 只有虛擬帳號或繳費代碼。不設的話取號資訊就永遠拿不到。
        "PaymentInfoURL": f"{base_url}/ecpay/payment-info",
    }
    if order_result_url:
        # 使用者的瀏覽器導回的地方。指向自己再 302 給 caller ——
        # 這樣 caller 的網址不必寫進綠界，我們也有機會先更新狀態。
        params["OrderResultURL"] = order_result_url
    if custom_fields:
        for i, key in enumerate(("CustomField1", "CustomField2",
                                 "CustomField3", "CustomField4"), start=1):
            v = custom_fields.get(f"custom{i}")
            if v:
                params[key] = str(v)[:50]
    if period:
        params.update({
            "PeriodAmount": str(amount),      # 綠界規定：必須等於 TotalAmount
            "PeriodType": period["period_type"],
            "Frequency": str(period["frequency"]),
            "ExecTimes": str(period["exec_times"]),
            # 第二期起的扣款結果打這裡。**首期打的是 ReturnURL**，
            # 而且內容跟一次性付款完全一樣 —— 分辨不出來，只能查自己的表。
            "PeriodReturnURL": f"{base_url}/ecpay/period-return",
        })
    return client.signed(params)


def query_trade(merchant_trade_no: str) -> dict:
    """查單。TimeStamp 是 Unix epoch，綠界只接受三分鐘內的。"""
    import time
    s = get_settings()
    data = client.post(s.query_trade_url, {
        "MerchantID": s.merchant_id,
        "MerchantTradeNo": merchant_trade_no,
        "TimeStamp": str(int(time.time())),
    }, verify_response=True)
    # 這支查詢成功時**不回 RtnCode** —— 回的是 TradeStatus。
    # 用 require_success 會把成功的查詢當成失敗。
    if "TradeStatus" not in data:
        raise ECPayError(data.get("RtnCode", "0"),
                         data.get("RtnMsg", "查詢沒有回 TradeStatus"), raw=data)
    return data


# 綠界查單回的 TradeStatus → 本服務的狀態
TRADE_STATUS = {
    "0": "awaiting_payment",     # 已建立、尚未付款（ATM／超商取號後就是這個）
    "1": "paid",
    "10200095": "failed",        # 消費者未完成付款
}


def do_action(*, merchant_trade_no: str, trade_no: str, action: str,
              amount: int) -> dict:
    """信用卡請退款。

    **測試環境沒有這支 API**（綠界：「因無法提供實際授權，故無法使用此 API」），
    所以呼叫端要先看 settings.do_action_available，不要送出去等它失敗。

    action:
      R = 退刷（已關帳的訂單）
      N = 放棄授權（尚未關帳，例如當天剛付款的）
      C = 關帳請款      E = 取消關帳
    """
    s = get_settings()
    data = client.post(s.do_action_url, {
        "MerchantID": s.merchant_id,
        "MerchantTradeNo": merchant_trade_no,
        "TradeNo": trade_no,
        "Action": action,
        "TotalAmount": str(amount),
    })
    return client.require_success(data)
