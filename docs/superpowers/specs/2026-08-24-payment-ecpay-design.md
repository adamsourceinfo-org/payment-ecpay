# payment-ecpay 設計

2026-08-24

## 這是什麼

`adamsourceinfo-org/payment-ecpay` —— 供多個 caller 呼叫的**綠界（ECPay）底層金流後端**。
一次性付款、定期定額訂閱、訂單查詢、退款。Caller 用 API key 認證。

跟 `payment-paypal` 是平行的兄弟服務：同一套 CI、同一套認證模型、同樣的三層切分，
但**不共用資料庫、不互相呼叫**。跟舊的 `adamhsu-apps/payment-ecpay` 同名但無關，全新重寫。

### 為什麼不是把綠界塞進 payment-paypal

兩家金流的模型根本不同（見下節）。硬塞進同一個服務會逼出一層「共同抽象」，
而那層抽象必須同時容納「REST 建單拿 id」與「表單導轉 + 簽章回呼」，
結果是兩邊都不像。分成兩個服務，各自誠實面對自己的上游。

## 綠界與 PayPal 的模型差異（設計的起點）

| | PayPal | 綠界 |
|---|---|---|
| 建單 | REST，回 order id | **表單 POST 導轉**，要自算 `CheckMacValue` |
| 憑證 | Client ID 半公開 + secret | **HashKey 與 HashIV 兩個都是機密** |
| 幣別 | 多幣別、有小數 | **只有 TWD、整數、不可有小數** |
| 冪等鍵 | `invoice_id` 可帶任意長度 | `MerchantTradeNo` **上限 20 碼英數** |
| 事件 | webhook 帶全域 event id | 表單 POST，**沒有 event id**，去重要自己造 |
| 事件回應 | 2xx 即可 | 必須回**字串 `1|OK`**，否則 5~15 分重送、當天四次 |
| 訂閱 | plan 物件 + subscription 物件 | 沒有 plan 物件，週期參數直接帶在建單上 |

## 決策紀錄

**建單回 `checkout_url`，不是只回 form fields。** 綠界是表單導轉模型，
caller 若自組表單就得自己算 `CheckMacValue` —— 那等於把 HashKey 發給每個 caller，
本服務就失去存在意義。所以服務自己 host 一頁 auto-submit。
`form`（action + fields）照樣一併回傳，讓要自己 render 的 caller（App 內嵌 WebView）也有路。

**`checkout_url` 帶的是獨立的隨機 token，不是 order id。** 那一頁不驗 API key
（使用者的瀏覽器會打它），用 order id 當網址等於讓人猜得到別人的付款頁。
token 存在 `orders.checkout_token`，一次性付款成立後失效。

**回呼一律先查自己的表，不看 payload 判型別。**
綠界定期定額的**首期回呼跟一次性付款逐欄位相同** —— `PeriodType` / `Frequency` /
`ExecTimes` / `TotalSuccessTimes` 只出現在第二期起的 `PeriodReturnURL`。
所以「這張單是不是訂閱」必須在**建單當下**就寫進 DB，回呼時用 `MerchantTradeNo` 查出來。
`events` 表在落地當下就定好 `subject_kind` / `subject_id`；只存綠界原文的事件表
事後永遠分辨不出來（既有系統就是這樣壞的）。

**`MerchantTradeNo` 用高熵亂碼，不用流水號。**
唯一性是「每商店」，而測試商店 3002607 是全球開發者共用的 ——
`order-1`、`20260824001` 這種一定撞到陌生人的單。dev/prod 用同一支產生器。

**dev 的 HashKey/HashIV 照樣走 Secret Manager**，即使它公開在綠界文件上。
目的是讓兩個環境的載入路徑一模一樣，不要等 prod 上線才第一次執行到讀 secret 的程式碼。

**服務不主動推送事件。** caller 用 `GET /v1/events?after=` 游標拉。
可靠送達是一整套子系統（重試、退避、死信、對方端點的可用性），caller 越多負擔越重。
`events` 表就是將來要做推送時的來源。

### 明確不做的

- **綁卡扣款 / 信用卡記憶卡號**（商店自存卡片 token）—— 那是另一個產品、另一份介接文件，
  且需要特約賣家資格。本服務的「訂閱」是綠界排程的**定期定額**。
- **分期付款**（需特約賣家）、**海外卡 / 銀聯卡**（`OnlyTaiwanCard=True`）。
- **電子發票**、**物流** —— 綠界有，但不是金流。
- **admin API / 萬能鑰匙** —— API key 用 `scripts/add-caller.sh` 手動建。
- **對帳檔下載** —— 有需要再說，v1 靠 `?refresh=true` 逐筆對。

## 對外介面

Caller 用，需 `X-API-Key`：

| 端點 | scope | 說明 |
|---|---|---|
| `POST /v1/orders` | `orders:write` | 建單，回 `{id, checkout_url, form}` |
| `GET /v1/orders` | `orders:read` | 列出自己的訂單 |
| `GET /v1/orders/{id}` | `orders:read` | 查單；`?refresh=true` 去綠界 `QueryTradeInfo/V5` 對帳 |
| `POST /v1/orders/{id}/refund` | `orders:write` | 退款，**僅信用卡** |
| `POST /v1/subscriptions` | `subscriptions:write` | 建定期定額，回 `checkout_url` |
| `GET /v1/subscriptions` | `subscriptions:read` | 列出 |
| `GET /v1/subscriptions/{id}` | `subscriptions:read` | 查；`?refresh=true` 去綠界 `QueryCreditCardPeriodInfo` |
| `POST /v1/subscriptions/{id}/cancel` | `subscriptions:write` | 終止，**不可復原** |
| `GET /v1/events` | `events:read` | 游標拉事件 |
| `GET /health` | — | db + 設定；壞掉回 **503** |

綠界打的，不驗 API key，靠 `CheckMacValue` 驗真偽：

| 端點 | 說明 |
|---|---|
| `GET /ecpay/checkout/{token}` | auto-submit form，POST 到綠界 |
| `POST /ecpay/return` | 付款結果（含定期定額**首期**），回 `1|OK` |
| `POST /ecpay/period-return` | 定期定額**第二期起**，回 `1|OK` |
| `POST /ecpay/payment-info` | ATM／超商**取號**結果，回 `1|OK` |
| `GET /ecpay/order-result` | 瀏覽器導回 → 302 到 caller 的 `return_url` |

### 事件流的契約

`GET /v1/events?after=<id>&limit=<n>` 回 `{items:[...], next_cursor}`。
`id` 是單調遞增的 bigserial，caller 記住最後一筆當下次的 `after`。
`after=0` 從頭拉，這也是對帳路徑。對應不到 caller 的事件以 `caller_id = NULL` 落地，
對每個 caller 都不可見（`WHERE caller_id = %s` 不匹配 NULL）。

## 認證與授權

API key 存 sha256、不存明文，可停用，有 `last_used_at`。
沒帶、錯的、停用的一律回 **401 且不區分原因**，不幫攻擊者縮小範圍。
查別人的資源回 **404 而非 403**（403 會洩漏「該資源存在」）。

**服務對外是公開的**（`allow_unauthenticated: true`）—— 綠界的回呼必須打得到。
所以應用層的 API key 是唯一一道門，回呼端點則靠 `CheckMacValue` 驗真偽。

## 資料模型

- `api_keys` —— `caller_id` / `key_hash`(unique) / `scopes` / `active` / `last_used_at`
- `orders` —— `caller_id` / `reference_id` / `merchant_trade_no`(unique) / `ecpay_trade_no` /
  `amount`(integer) / `status` / `choose_payment` / `checkout_token`(unique) / `return_url` /
  `paid_at` / `refunded_amount` / `closed`（是否已關帳，決定退款動作）
  唯一鍵 `(caller_id, reference_id)` —— 網路重試不會變成兩筆收款
- `order_payment_info` —— ATM／超商取號結果：`bank_code` / `v_account` / `payment_no` / `expire_date`
- `subscriptions` —— `caller_id` / `reference_id` / `merchant_trade_no`(unique) /
  `period_amount` / `period_type` / `frequency` / `exec_times` / `status` /
  `total_success_times` / `total_success_amount` / `cancelled_at`
- `subscription_charges` —— 每期扣款：`gwsr`(unique，綠界每次授權的交易號) / `amount` /
  `process_date` / `auth_code` / `rtn_code`
- `events` —— `id` bigserial（就是對外游標）/ `dedupe_key`(unique) / `event_type` /
  `caller_id`(可 NULL) / `subject_kind` / `subject_id` / `payload` jsonb
- `schema_migrations`

## 綠界整合

### 一環境一組憑證

| | dev | prod |
|---|---|---|
| `ECPAY_ENV` | `stage` | `production` |
| `ECPAY_MERCHANT_ID` | `3002607`（官方公開測試商店） | `3017099`（Adam Studio） |
| 付款網域 | `payment-stage.ecpay.com.tw` | `payment.ecpay.com.tw` |
| HashKey / HashIV | Secret Manager | Secret Manager |

base URL 由 `ECPAY_ENV` **推導**，不做成設定 —— 可設定就有設錯的餘地，
而「prod 指到 stage」的代價是以為在收錢但沒有（舊服務就發生過）。

### CheckMacValue

1. 參數依名稱 A→Z 排序（不分大小寫）
2. 前後夾成 `HashKey=<key>&<排序後的 k=v>&HashIV=<iv>`
3. **整串** URLEncode（連 `=` `&` 都編碼），採 .NET 風格
4. 轉小寫
5. SHA256
6. 轉大寫

Python 的 `quote_plus` 與 .NET `HttpUtility.UrlEncode` 的差異必須修正：
`!` `*` `(` `)` 要**還原成原字元**，`~` 要編成 `%7e`。`-` `_` `.` 兩邊都不編碼。

官方範例向量寫成單元測試（`tests/test_checkmac.py`）：
`MerchantID=3002607` 那組必須算出
`6C51C9E6888DE861FD62FB1DD17029FC742634498FD813DC43D4243B5685B840`。
算不出來不准過 —— 之後每個查不出原因的綠界錯誤都會先被懷疑是這裡。

### 回呼驗簽與去重

收到的表單欄位**扣掉 `CheckMacValue` 本身**重算，比對不符回 400 且不落地。

綠界沒有全域 event id，去重鍵自己造：

| 回呼 | `dedupe_key` |
|---|---|
| `/ecpay/return` | `return:<MerchantTradeNo>:<RtnCode>` |
| `/ecpay/period-return` | `period:<gwsr>` |
| `/ecpay/payment-info` | `info:<MerchantTradeNo>` |

驗簽通過就**一律回 `1|OK`**，包含重複的那些 —— 不回 `1|OK` 綠界會重送四次。

### 時區

`MerchantTradeDate` 格式 `yyyy/MM/dd HH:mm:ss`，必須是**台北時間**。
容器跑 UTC，用 `zoneinfo.ZoneInfo("Asia/Taipei")`。
查詢 API 的 `TimeStamp` 是 Unix epoch（三分鐘有效），不受時區影響。

### 金額

只支援 **TWD、正整數**。`TotalAmount` 不可有小數。
`0`、負數、小數、非數字一律 400，錯誤帶 `{"error","field","message"}` 指名欄位。
定期定額的 `TotalAmount` 必須**等於** `PeriodAmount`（綠界規定）。

### 定期定額參數

`PeriodType` ∈ `D`(1–365) / `M`(1–12) / `Y`(1)；`Frequency` 依型別限制；
`ExecTimes` ≥ 2，`D`/`M` 上限 999、`Y` 上限 99。首期授權失敗則整張單不進排程。
連續六期失敗綠界自動終止。終止用 `CreditCardPeriodAction` 的 `Action=Cancel`，
**成功後無法重新啟用**，只能重開一張新單。

### 退款

`CreditDetail/DoAction`，**只有正式環境有**（測試環境不提供，官方文件明說
「因無法提供實際授權，故無法使用此 API」）。

動作依關帳狀態選：

| 訂單狀態 | 動作 | 說明 |
|---|---|---|
| 已關帳 | `R`（退刷） | 綠界每日 20:15–20:30 自動關帳 |
| 未關帳（當天付款） | `N`（放棄授權） | 取消尚未請款的授權 |

`3017099` 的「每日自動關帳」是**啟用**的，所以隔天起的退款走 `R`。
本服務用 `orders.closed` 追蹤，未知時預設走 `R` 並把綠界的錯誤原文回給 caller。

**避開每日 20:15–20:30** 呼叫此 API。

## 設定與機密

```
.cicd/config.yml
  service: payment-ecpay
  health_path: /health
  allow_unauthenticated: true        # 綠界回呼必須打得到
  db: {instance: payment-ecpay-pg, name: payment_ecpay}
```

| 變數 | 哪裡 | dev | prod |
|---|---|---|---|
| `ECPAY_ENV` | `env.<env>` | `stage` | `production` |
| `ECPAY_MERCHANT_ID` | `env.<env>` | `3002607` | `3017099` |
| `ECPAY_ALLOWED_PAYMENTS` | `env.<env>` | `Credit,WebATM,ATM,CVS,BARCODE` | 同左 |
| `ECPAY_TIMEOUT_SECONDS` / `DB_POOL_MAX` | `env.common` | 10 / 3 | 同左 |
| `PUBLIC_BASE_URL` | 選填 | 不設 | 不設 |
| `ECPAY_HASH_KEY` / `ECPAY_HASH_IV` | `secrets.<env>` | Secret Manager | Secret Manager |
| `APP_ENV` / `APP_VERSION` / DB 連線 | — | CI 依部署目標注入 | 同左 |

`PUBLIC_BASE_URL` 沒設就由**請求自身的 scheme + host** 推導（Cloud Run 給的就是服務網域）。
這樣第一次部署不會卡在「還沒有網址就填不了回呼網址」的雞生蛋。

啟動時驗證：缺 `ECPAY_ENV` / `ECPAY_MERCHANT_ID` / `ECPAY_HASH_KEY` / `ECPAY_HASH_IV`
就啟動失敗 —— Cloud Run 起不來、CI smoke 紅燈、當場知道。

## 健康檢查

`/health` 回 `{service, env, version, db:{...}, ecpay:{env, merchant_id, credentials}}`。
`db.server_user` 與 `db.database` **由 DB 自己回答**，回音環境變數證明不了任何事。
`db.ok` 非 true 就回 **503** —— ci 的 smoke 只看狀態碼不看 body，回 200 等於放行一個壞掉的部署。

**綠界沒有便宜的認證探測端點**（沒有 OAuth token 這種東西），所以健康檢查
**不假裝驗證得了綠界連線** —— `credentials` 只回報 HashKey/HashIV 是否已載入。
發明一個假的探測（例如用假單號打 QueryTradeInfo）只會污染綠界的日誌又證明不了什麼。

**路徑是 `/health` 不是 `/healthz`** —— Google Frontend 在 `*.run.app` 上會攔截
`/healthz` 自己回 404，請求根本不會進到容器。

## 模組結構

```
app/
  config.py        唯一碰 os.environ 的模組
  db.py            Cloud SQL IAM 認證、連線池、migration
  auth.py          API key 驗證與 scope
  errors.py        錯誤型別與對外語意
  money.py         TWD 整數驗證
  models.py        request/response schema
  ids.py           高熵 MerchantTradeNo / checkout token
  main.py          組裝
  ecpay/
    checkmac.py    檢查碼：產生與驗證
    client.py      HTTP 層，form-urlencoded 進出
    orders.py      建單參數、查詢、退款
    subscriptions.py 定期定額參數、查詢、終止
  store/           SQL 只住這一層，每個查詢都帶 caller_id
  routers/         HTTP 介面
```

## 測試

- **單元**（離線）：CheckMacValue 官方向量與 .NET 編碼差異、TWD 金額規則、
  API key 與 scope、回呼驗簽與去重、定期定額參數驗證、時區。
- **stage 端對端**（`scripts/stage-smoke.py` 打已部署的 dev URL）：
  建單 → **用瀏覽器實際完成 stage 付款**（測試卡 4311-9511-1111-1111、3D 驗證碼 1234）
  → 確認回呼把狀態翻成 paid → ATM 取號 → 確認取號資訊落地 → 建訂閱 → 首期扣款
  → 終止 → events 游標。

**退款的成功路徑在 dev 驗不到**（綠界測試環境沒有 DoAction）。dev 只驗 400 分支
（非信用卡訂單、未付款訂單、金額超額）。真正的退刷只能在 prod 用小額真刷驗。

## 一次性 runbook（人跑的，CI 不會做）

1. 兩個環境各建 `payment-ecpay-pg` + `payment_ecpay` database + IAM db user
   （短格式 `run-runtime@<專案>.iam`）
2. 連進**目標 database** 下 `GRANT`（Postgres 15+ 的 `public` 不再預設給 PUBLIC 建表權）
3. 四個 secret：`payment-ecpay-hash-key-{dev,prod}`、`payment-ecpay-hash-iv-{dev,prod}`，
   授 `run-runtime` 的 `secretmanager.secretAccessor`
4. 建 repo + 貼 caller stub
5. `scripts/add-caller.sh` 建第一把 API key

## 已知風險與限制

| 風險 | 說明 |
|---|---|
| **退款在 dev 驗不到** | 綠界測試環境不提供 `DoAction`，只能 prod 小額真刷驗 |
| **同日退款動作不同** | 當天付款未關帳，要走 `N` 而非 `R` |
| **海外卡不支援** | `OnlyTaiwanCard=True`，個人賣家資格限制，非程式問題 |
| **後台信用卡區被 2FA 擋** | 人工核帳前要先在綠界後台設定雙因子驗證 |
| **測試商店全球共用** | `MerchantTradeNo` 必須高熵；stage 上會看到陌生人的訂單 |
| **30 日收款額度 NT$200,000** | prod 帳號限制 |
| **手續費 NT$5 固定** | 小額測試淨入極少（NT$6 的單淨入 1 元） |
| **綠界介接 IP 白名單** | 後台那格**永遠不要填** —— Cloud Run 沒有固定出口 IP |
