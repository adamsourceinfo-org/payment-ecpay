"""對外的 request schema。

金額用 int 而不是字串 —— 跟 payment-paypal 相反，因為綠界的 TotalAmount
本來就是整數欄位、不接受小數。用字串傳「整數」只會讓 caller 以為
這裡可能有小數。真正的驗證在 app/money.py（pydantic 只擋型別）。
"""
from typing import Optional

from pydantic import BaseModel, Field


class OrderCreate(BaseModel):
    reference_id: str = Field(min_length=1, max_length=100,
                              description="caller 提供的冪等鍵，同一個值只會建一筆")
    amount: int = Field(description="TWD 整數，不可有小數")
    item_name: str = Field(min_length=1, max_length=400,
                           description="多項商品用 # 分隔")
    trade_desc: Optional[str] = Field(default=None, max_length=200)
    choose_payment: str = Field(
        default="Credit",
        description="Credit / WebATM / ATM / CVS / BARCODE / ALL；"
                    "實際可用的由環境的 ECPAY_ALLOWED_PAYMENTS 決定")
    return_url: Optional[str] = Field(
        default=None, description="使用者付完款要導回的位置（caller 自己的頁面）")
    custom1: Optional[str] = Field(default=None, max_length=50)
    custom2: Optional[str] = Field(default=None, max_length=50)
    custom3: Optional[str] = Field(default=None, max_length=50)
    custom4: Optional[str] = Field(default=None, max_length=50)


class RefundCreate(BaseModel):
    amount: Optional[int] = Field(
        default=None, description="省略代表全額退款")
    note: Optional[str] = Field(default=None, max_length=255)


class SubscriptionCreate(BaseModel):
    reference_id: str = Field(min_length=1, max_length=100)
    amount: int = Field(description="每期金額，TWD 整數")
    item_name: str = Field(min_length=1, max_length=400)
    trade_desc: Optional[str] = Field(default=None, max_length=200)
    period_type: str = Field(default="M", description="D 日 / M 月 / Y 年")
    frequency: int = Field(default=1, description="每幾個週期扣一次")
    # **刻意不開放 exec_times。** 綠界沒有「無限期直到取消」的選項，
    # ExecTimes 是必填 —— 但那是綠界的實作細節，不該外洩給 caller。
    # 讓 caller 填一個有限的數字，遲早會有人填了 12 然後在第 13 個月
    # 發現訂閱無預警停掉。訂閱的語意就是「到取消為止」，
    # 期數由服務內部固定成月週期上限（見 ecpay/subscriptions.py）。
    return_url: Optional[str] = None
    custom1: Optional[str] = Field(default=None, max_length=50)
    custom2: Optional[str] = Field(default=None, max_length=50)
