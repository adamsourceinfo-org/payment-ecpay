# payment-ecpay 實作與驗證記錄

2026-08-24。設計見 [`../specs/2026-08-24-payment-ecpay-design.md`](../specs/2026-08-24-payment-ecpay-design.md)。

## 一次性設定（人跑的，CI 不做）

| 項目 | dev | prod |
|---|---|---|
| Cloud SQL `payment-ecpay-pg` | ✅ | ✅ |
| database `payment_ecpay` | ✅ | ✅ |
| IAM DB user `run-runtime@<專案>.iam` | ✅ | ✅ |
| `GRANT ALL ON SCHEMA public` | ✅ | ✅ |
| 人的 IAM DB user + role membership | ✅ | ✅ |
| Secret `payment-ecpay-hash-{key,iv}-<env>` | ✅ | ✅ |

`run-runtime` 的 `roles/cloudsql.client` 與 `roles/cloudsql.instanceUser` 是專案層級的，
`payment-paypal` 時期就有了，不需重做。

## 驗證結果

### 離線單元測試：77 項全數通過

其中最關鍵的是 `tests/test_checkmac.py` —— 綠界官方文件的完整範例向量，
必須算出 `6C51C9E6888DE861FD62FB1DD17029FC742634498FD813DC43D4243B5685B840`。
檢查碼是整個介接的信任根，這裡錯的話之後每個查不出原因的錯誤都會先被懷疑到它。

### dev 上的 API 檢查：27 項全數通過

健康檢查（含 `db.server_user` 由 DB 自己回答）、認證的三種失敗一致回 401、
金額驗證、付款方式白名單、冪等、404 而非 403、退款的各種拒絕分支、
定期定額參數驗證、事件游標。

### 綠界**真實回呼**驗證（ATM 全生命週期）

| 步驟 | 結果 |
|---|---|
| 導轉頁 auto-submit → 綠界付款頁 | 訂單資訊、中文商品名、金額都正確 |
| ATM 取號 | 綠界 POST `/ecpay/payment-info`，驗簽通過 |
| 取號資訊落地 | 銀行 004、虛擬帳號 `3833546239047825`、期限 `2026/08/27` |
| 訂單狀態 | → `awaiting_payment` |
| 後台模擬付款 | 綠界 POST `/ecpay/return`，驗簽通過，回 `1|OK` |
| 訂單狀態 | → `paid`，`payment_type=ATM_BOT`，`paid_at` 寫入 |
| 事件流 | `payment.info` / `payment.return` 兩筆，`subject_kind=order` |
| `?refresh=true` | `QueryTradeInfo/V5` 真實呼叫，**回應驗簽通過** |

回呼帶的是**進導轉頁時換發的新單號**，靠 `trade_attempts` 才解析得回原訂單 ——
順帶證明了換單號那個修正是對的。

`/ecpay/return` 這支就是信用卡與定期定額首期共用的處理路徑。

### 這次實測抓到的兩個真 bug

1. **latin-1 解碼**：`request.form()` 讓中文 `RtnMsg` 變亂碼，驗簽必敗 ——
   **每一筆真實付款通知都會被拒絕**。13 條回呼測試全過卻沒擋住，因為假資料全是 ASCII。
2. **單號不能重用**：付款頁回訪拿到 `10300028`，等於「跳開再回來付」這條路本來是壞的。

兩個都只有在打真實綠界流量時才會現形。

## 沒有驗到的部分（照實列，不粉飾）

| 項目 | 為什麼 |
|---|---|
| **信用卡付款、定期定額首期扣款、續期扣款、終止** | 綠界刷卡頁的前端被本機 Chrome 的 AdGuard 擴充擋住（`ERR_BLOCKED_BY_CLIENT`），送不出付款。試過真實滑鼠事件、直接送 `PayForm`、後台模擬付款、掃碼模擬四種路徑都不通。不為此重啟使用者的瀏覽器。 |
| **退款成功路徑** | 綠界測試環境**不提供** `DoAction`（官方：因無法提供實際授權）。只驗到各種拒絕分支。 |

已驗到的相關部分：訂閱參數確實正確送達綠界（付款頁顯示「定期定額 每 1 個月扣 1 次，
每次扣款金額 5 元」）；`QueryCreditCardPeriodInfo` 與 `CreditCardPeriodAction` 兩支
真實 API 都呼叫得通、回應解析正常、錯誤原文如實轉給 caller
（未成立的訂閱回 `90100150 不存在的訂單` → 502）。

**要補完這一段，最省事的方法是在沒有廣告攔截擴充的瀏覽器上跑一次
`scripts/stage-smoke.py create` 給的 `checkout_url`，然後跑 `verify`。**
