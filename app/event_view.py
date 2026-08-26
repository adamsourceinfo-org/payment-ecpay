"""事件對外的形狀。**兩條出口共用這一個函式。**

`GET /v1/events` 回應裡 `items[]` 的一個元素，與推送的 body，
必須逐欄相同 —— caller 因此只要寫一份 parser。

這不是「寫測試盯著兩邊別漂移」的問題，是**根本不該有兩邊**：
兩個形狀就是兩份程式碼、兩組 bug，而其中一份平常不會執行 ——
那是最糟的一種程式碼。所以形狀只在這裡定義一次。

⚠️ `caller_id` 刻意不在裡面：caller 自己知道自己是誰，回給他沒有意義，
而且那個欄位的 NULL 值代表「這筆對每個 caller 都不可見」，
不該有機會外流。
"""


def item(row: dict) -> dict:
    return {
        "id": row["id"],
        "event_type": row["event_type"],
        "subject_kind": row["subject_kind"],
        "subject_id": row["subject_id"],
        "payload": row["payload"],
        "received_at": row["received_at"],
    }
