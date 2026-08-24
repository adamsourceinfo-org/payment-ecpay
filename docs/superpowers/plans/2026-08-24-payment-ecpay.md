# payment-ecpay 實作與驗證記錄

2026-08-24。設計見 [`../specs/2026-08-24-payment-ecpay-design.md`](../specs/2026-08-24-payment-ecpay-design.md)。

## 一次性設定（人跑的，CI 不做）

> **2026-08-24 下午環境改版**：每服務一台的 instance 全部收掉，改成一個環境一台
> 共用的 `apps-pg`，執行身分也從共用的 `run-runtime@` 換成每服務一個
> `run-payment-ecpay@`。隔離改由 `payment_ecpay` 這個 database 的
> `REVOKE CONNECT FROM PUBLIC` + 只授權給自己的 SA 提供。
> `.cicd/config.yml` 因此只宣告 `db.name` 與 `runtime_sa`，不再宣告 instance。
> 下表是改版**後**的狀態。

| 項目 | dev | prod |
|---|---|---|
| Cloud SQL（共用 `apps-pg`） | ✅ | ✅ |
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
| **超商代碼**：取號 → `awaiting_payment` | `payment_no=LLL26236930264`、期限 7 天 |
| **超商代碼**：後台模擬付款 → `paid` | `payment_type=CVS_CVS` |
| `?refresh=true` | `QueryTradeInfo/V5` 真實呼叫，**回應驗簽通過** |

回呼帶的是**進導轉頁時換發的新單號**，靠 `trade_attempts` 才解析得回原訂單 ——
順帶證明了換單號那個修正是對的。

`/ecpay/return` 這支就是信用卡與定期定額首期共用的處理路徑。

### 這次實測抓到的兩個真 bug

1. **latin-1 解碼**：`request.form()` 讓中文 `RtnMsg` 變亂碼，驗簽必敗 ——
   **每一筆真實付款通知都會被拒絕**。13 條回呼測試全過卻沒擋住，因為假資料全是 ASCII。
2. **單號不能重用**：付款頁回訪拿到 `10300028`，等於「跳開再回來付」這條路本來是壞的。

兩個都只有在打真實綠界流量時才會現形。

### 訂閱狀態機：以正確簽章的回呼重放驗證（`scripts/replay-callbacks.py`，25 項全過）

綠界是導轉模型，訂閱首期必須有人在瀏覽器真的刷一張卡；那條路在本機跑不完（見下），
所以改用**正確簽章的回呼重放**打已部署的服務與真實資料庫：

| 驗到什麼 | |
|---|---|
| 首期回呼（欄位與一次性付款相同）能被解析成訂閱 | ✓ |
| 首期成功 → `active`、寫入 `first_charged_at` | ✓ |
| 首期重送不會變成兩筆扣款 | ✓ |
| 續期走 `PeriodReturnURL`、累計次數與金額更新 | ✓ |
| 續期以 `gwsr` 去重（綠界全域唯一的授權交易號） | ✓ |
| **扣款失敗不會把訂閱標成結束**（綠界連六期失敗才自動終止） | ✓ |
| 事件的 `subject_kind` 標成 `subscription` 而非 `order` | ✓ |
| 竄改金額的回呼被拒絕且**不落地** | ✓ |

**這支驗不到的是「綠界是否真的那樣送」** —— 簽章是我們自己算的。
但簽章演算法由官方向量鎖定，而「綠界實際送出的回呼我們驗得過」已由 ATM 那條真實流程證明，
所以缺口只剩「綠界的欄位是否與文件一致」。

## 沒有驗到的部分（照實列，不粉飾）

| 項目 | 為什麼 |
|---|---|
| **綠界刷卡頁上的真實信用卡授權** | 見下方「為什麼這條走不通」——四條路都封死，且各有明確原因。非本服務的問題。 |
| **退款成功路徑** | 綠界測試環境**不提供** `DoAction`（官方：因無法提供實際授權）。只驗到各種拒絕分支。 |

已驗到的相關部分：訂閱參數確實正確送達綠界（付款頁顯示「定期定額 每 1 個月扣 1 次，
每次扣款金額 5 元」）；`QueryCreditCardPeriodInfo` 與 `CreditCardPeriodAction` 兩支
真實 API 都呼叫得通、回應解析正常、錯誤原文如實轉給 caller
（未成立的訂閱回 `90100150 不存在的訂單` → 502）。

### 為什麼信用卡這條走不通（四條路都試過）

| 路徑 | 結果 | 原因 |
|---|---|---|
| 自動化瀏覽器點「立即付款」 | Vue 把 `PayForm` 建好、填好、連 `action` 都設好，**就是不送出**。無例外、無網路請求、未呼叫 `submit()` | 在他們的壓縮包內 |
| 手動送出 `PayForm` | `10300029 訂單建立失敗` | 送出前還有一步；且不是頁面過期（開頁 53 秒內送出照樣失敗） |
| **伺服器端重放請求序列** | 拿不到表單 | 收銀台 HTML 裡只有 `<!-- PayForm 容器，由 Vue render 函數動態生成 --><div id="PayForm"></div>` ——**表單根本不存在於伺服器回應中** |
| 後台「模擬付款」 | 找不到訂單 | 綠界要真的送出付款方式才會建立可查詢的交易；信用卡卡在上一步 |
| （順帶）關掉 3D 驗證 | `此帳號無法更改信用卡設定` | 共用的測試特店不給改 |

界線很清楚：**留在綠界站內的動作都成功（ATM、超商代碼取號都過），
一旦要交棒給銀行或 3D 驗證就停住**（信用卡與 WebATM 同一個症狀）。
**一度歸咎於 AdGuard 擴充是錯的** —— 擴充移除後行為完全相同。

**要補完這一段，最省事的方法是由人手動在綠界測試付款頁刷一次測試卡**
（`4311-9511-1111-1111`，3D 驗證碼 `1234`），對象是 `scripts/stage-smoke.py create`
印出的 `checkout_url`，刷完跑 `scripts/stage-smoke.py verify`。

**更省事的做法：`scripts/manual-verify.py`** —— 它會建好訂單與訂閱、印出兩個連結與
卡號資料，然後自己盯著回呼進來，到齊後把該驗的（狀態、對帳、事件分類、終止訂閱）
一次驗完。你只要點連結、刷卡。
訂閱的狀態機本身已由 `replay-callbacks.py` 覆蓋，那一刷要證明的是
**綠界實際送出的信用卡／定期定額回呼欄位與我們預期的一致**。
