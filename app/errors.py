"""服務自己的錯誤型別與對外語意。

兩個刻意選擇：
- 無效的 API key 一律 401 且不區分原因（不幫攻擊者縮小範圍）
- 查詢別人的資源回 404 而不是 403（403 會洩漏「該資源存在」）
"""
from fastapi import HTTPException


class FieldError(ValueError):
    """帶欄位名的驗證錯誤 —— caller 要知道該改哪一個欄位，
    只回一句訊息的話他得自己猜。"""

    def __init__(self, message: str, field: str, code: str):
        self.field = field
        self.code = code
        super().__init__(message)

    def as_detail(self) -> dict:
        return {"error": self.code, "field": self.field, "message": str(self)}


class InvalidAmount(FieldError):
    def __init__(self, message: str, field: str = "amount"):
        super().__init__(message, field, "invalid_amount")


class InvalidField(FieldError):
    def __init__(self, message: str, field: str):
        super().__init__(message, field, "invalid_field")


class ECPayError(Exception):
    """綠界回了非成功的結果。

    綠界的錯誤有兩種形狀：HTTP 層失敗，以及 HTTP 200 但 `RtnCode != 1`。
    後者才是常態 —— 把兩者統一成這一個型別，呼叫端不必分辨。
    """

    def __init__(self, rtn_code, rtn_msg: str = "", status: int = 200,
                 raw: dict = None):
        self.rtn_code = str(rtn_code)
        self.rtn_msg = rtn_msg
        self.status = status
        self.raw = raw or {}
        super().__init__(f"ECPay RtnCode={rtn_code} {rtn_msg}")


def not_found(what: str = "resource") -> HTTPException:
    # 別人的資源也走這裡 —— 對呼叫者來說「不存在」與「不屬於你」不該有分別
    return HTTPException(status_code=404, detail=f"{what} not found")


def bad_request(detail) -> HTTPException:
    """detail 可以是字串，也可以是 FieldError —— 後者會展開成
    {"error", "field", "message"}，讓 caller 知道要改哪個欄位。"""
    if isinstance(detail, FieldError):
        return HTTPException(status_code=400, detail=detail.as_detail())
    return HTTPException(status_code=400, detail=detail)


def upstream_error(exc: ECPayError) -> HTTPException:
    """綠界的錯誤原文照實往上帶。RtnMsg 是向綠界客服查詢的唯一憑據，
    翻譯或吞掉它只會讓事故當下無從查起。"""
    return HTTPException(
        status_code=502,
        detail={"error": "ecpay_upstream", "rtn_code": exc.rtn_code,
                "rtn_msg": exc.rtn_msg},
    )
