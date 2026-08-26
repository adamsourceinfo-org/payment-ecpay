# payment-ecpay

供多個 caller 呼叫的**綠界（ECPay）底層金流後端**。一次性付款、定期定額訂閱、
訂單查詢、信用卡退款。Caller 用 API key 認證。

部署由 [`adamsourceinfo-org/ci`](https://github.com/adamsourceinfo-org/ci) 負責：
push `main` → dev，推 `vX.Y.Z` tag → prod（不重新 build，promote 同一個 image）。

設計文件：[`docs/superpowers/specs/2026-08-24-payment-ecpay-design.md`](docs/superpowers/specs/2026-08-24-payment-ecpay-design.md)

## 綠界跟 PayPal 不一樣的地方

如果你看過 `payment-paypal`，這幾點是會讓你踩坑的差異：

| | PayPal | 綠界 |
|---|---|---|
| 建單 | REST 拿 order id | **表單 POST 導轉**，自算 `CheckMacValue` |
| 憑證 | Client ID 半公開 + secret | **HashKey 與 HashIV 兩個都是機密** |
| 金額 | 多幣別、有小數 | **只有 TWD、整數、不可有小數** |
| 事件 | webhook 有全域 event id | **沒有**，去重鍵自己造 |
| 事件回應 | 2xx 即可 | 必須逐字回 **`1|OK`** |

## 快速上手

```bash
KEY=...   # 用 scripts/add-caller.sh 產生
BASE=https://payment-ecpay-xxxx.a.run.app

# 建一筆信用卡訂單
curl -s -X POST "$BASE/v1/orders" -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' -d '{
    "reference_id": "my-order-001",
    "amount": 100,
    "item_name": "測試商品",
    "choose_payment": "Credit",
    "return_url": "https://my-app.example/paid"
  }'
```

回傳裡的 `checkout_url` 就是把使用者導過去的地方；`form` 是同一份表單的原料，
要自己 render（例如 App 內嵌 WebView）就用它。

```bash
curl -s "$BASE/v1/orders/<id>" -H "X-API-Key: $KEY"                # 查單
curl -s "$BASE/v1/orders/<id>?refresh=true" -H "X-API-Key: $KEY"   # 去綠界對帳
curl -s "$BASE/v1/events?after=0" -H "X-API-Key: $KEY"             # 拉事件
```

## API

| 端點 | scope | 說明 |
|---|---|---|
| `POST /v1/orders` | `orders:write` | 建單。`reference_id` 是冪等鍵，重複回原本那筆 |
| `GET /v1/orders` | `orders:read` | 列出 |
| `GET /v1/orders/{id}` | `orders:read` | 查單，`?refresh=true` 去綠界對帳 |
| `POST /v1/orders/{id}/refund` | `orders:write` | 退款，**僅信用卡**（其他付款方式綠界沒有退款 API） |
| `POST /v1/subscriptions` | `subscriptions:write` | 定期定額 |
| `GET /v1/subscriptions/{id}` | `subscriptions:read` | 查，`?refresh=true` 帶每期明細 |
| `POST /v1/subscriptions/{id}/cancel` | `subscriptions:write` | 終止，**不可復原** |
| `GET /v1/events?after=` | `events:read` | 游標拉事件 |
| `PUT /v1/webhook-endpoint` | `webhooks:write` | 註冊／更新推送網址，回應帶簽章密鑰 |
| `GET /v1/webhook-endpoint` | `webhooks:read` | 查目前設定（含密鑰） |
| `DELETE /v1/webhook-endpoint` | `webhooks:write` | 停用推送。事件照樣落地，拉得到 |
| `POST /v1/webhook-endpoint/test` | `webhooks:write` | 送一筆合成的 `ping`，**不落地** |
| `GET /v1/deliveries?event_id=&status=` | `webhooks:read` | 投遞紀錄。「那筆到底送出去沒有」 |
| `POST /v1/events/{id}/redeliver` | `webhooks:write` | 重新排一次投遞 |
| `GET /health` | — | 壞掉回 503 |

綠界打的端點（不驗 API key，驗 `CheckMacValue`）：
`/ecpay/checkout/{token}`、`/ecpay/return`、`/ecpay/period-return`、
`/ecpay/payment-info`、`/ecpay/order-result`。

## 訂閱的期數不用（也不能）設

綠界**沒有「無限期直到取消」**這個選項，`ExecTimes` 是必填 —— 但那是綠界的
實作細節，本服務**不開放 caller 設定**，一律固定 999 期（月週期 ≈83 年）。

理由：讓 caller 填一個有限數字，遲早有人填了 12，然後在第 13 個月才發現訂閱
無預警停掉。訂閱的語意就是「到取消為止」，要停就呼叫 `cancel`。

## 查一筆訂閱／訂單

```bash
# 用我們的 id
curl -s "$BASE/v1/subscriptions/{id}" -H "X-API-Key: $KEY"

# 只記得自己的 reference_id 也查得到
curl -s "$BASE/v1/subscriptions?reference_id=my-sub-001" -H "X-API-Key: $KEY"

# 要綠界端的權威狀態（會打上游，別高頻呼叫）
curl -s "$BASE/v1/subscriptions/{id}?refresh=true" -H "X-API-Key: $KEY"
```

`status` 是**我們的**紀錄；`ecpay_exec_status_text`（`running` / `terminated` /
`completed`）是**綠界的** —— 「下個月還會不會扣款」看後者，而且只有
`?refresh=true` 之後才有值。

## 會咬人的地方

**定期定額的首期回呼跟一次性付款逐位元組相同。** `PeriodType` / `Frequency` /
`ExecTimes` / `TotalSuccessTimes` 只出現在**第二期起**的 `PeriodReturnURL`。
所以「這張單是不是訂閱」必須在建單當下就寫進 DB，回呼時用 `MerchantTradeNo` 查出來。
只存綠界原文的事件表事後永遠分辨不出來。

**取消訂閱不會退錢。** `cancel` 只停後續扣款，已收的不動 ——
caller 應該讓權限給滿最後一期。而且**終止後無法重新啟用**，只能重開一張新單。

**要確認「下個月還會不會扣款」，看 `ecpay_exec_status`**（呼叫
`GET /v1/subscriptions/{id}?refresh=true` 才會更新）：`running` 執行中、
`terminated` 已終止、`completed` 期數跑完。那是**綠界端**的狀態，
比本地的 `status` 有權威性。實測：取消前 `running`、取消後 `terminated`，
而 `total_success_times` 停在 1 —— 約定 12 期只扣了第一期。

**退款在測試環境其實可以用。** 綠界文件寫測試環境不提供，實測是錯的 ——
對真實授權過的 stage 訂單送 `Action=N` 會回 `Succeeded.`。所以退款不依環境擋。

**付款方式有金額下限，而綠界不公布數字。** 實測（測試特店）：超商代碼 27、
超商條碼 16、ATM 與 WebATM 2、信用卡無下限。用 `ECPAY_MIN_AMOUNTS` 逐環境設定；
沒設就不擋，caller 會拿到 `checkout_url` 但使用者在綠界撞到死路、訂單停在 `created`。
要量自己的商店：`scripts/probe-limits.py`。

**避開每日 20:15–20:30** 打退款 API —— 那是綠界的自動關帳時段。

**綠界後台的「介接允許 IP」永遠不要填。** Cloud Run 沒有固定對外 IP，
填了之後退款與查詢 API 會被擋掉。

## 環境

| | dev | prod |
|---|---|---|
| `ECPAY_ENV` | `stage` | `production` |
| 商店代號 | `3002607`（綠界公開的測試特店） | `3017099`（Adam Studio） |
| 定期定額 | 可用 | 可用（實證：2026-08-19 有成功的月週期單） |
| 退款 API | 可用（文件說沒有，實測有） | 可用 |
| 海外卡 | — | 不支援（帳號資格） |

dev 用的測試特店是**全球開發者共用**的，所以 `MerchantTradeNo` 一律高熵亂碼 ——
流水號會撞到陌生人的訂單。stage 上也會看到不是你建的訂單。

## 驗證

```bash
.venv/bin/python -m pytest -q                    # 單元（含綠界官方檢查碼向量）
BASE=… KEY=… python3 scripts/stage-smoke.py checks     # 對已部署的服務跑 API 檢查
BASE=… KEY=… python3 scripts/replay-callbacks.py       # 訂閱狀態機（簽好的回呼重放）
BASE=… KEY=… python3 scripts/manual-verify.py          # 需要人刷一次卡的那段
```

`manual-verify.py` 會印出付款連結與測試卡資料，然後自己等回呼 ——
你只要在瀏覽器點連結、刷卡。之所以需要人：綠界收銀台是 Vue SPA，
信用卡表單由前端動態產生（HTML 原始碼裡只有 `<div id="PayForm"></div>`），
伺服器端重放不可能，而自動化瀏覽器跑到最後一步不會送出。
ATM 與超商代碼那類「留在綠界站內」的流程都自動驗過了。

## 開發

```bash
uv venv --python python3.12 && uv pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q
```

本機一定要用 Python 3.12（與容器一致）。

## 新增一個 caller

```bash
./scripts/add-caller.sh dev my-service "orders:read,orders:write,events:read" "備註"
```

沒有 admin API、沒有萬能鑰匙 —— 一把能製造其他 key 的鑰匙，長期風險大於它省的麻煩。
整套沒有 DB 密碼：人與服務都走 Cloud SQL IAM 認證。

## 怎麼接事件推送

事件有**兩條出口**：`GET /v1/events` 拉取（原本就有，一個位元組都沒變），
以及推送。沒註冊端點的 caller 完全不會有推送。

推送的用途是「**沒有人在跟你的服務講話的時候**」—— 典型的是每月續期扣款。
如果你的服務也 scale to zero，那筆錢進來時沒有任何請求會觸發你去拉。

### 一、註冊

```bash
curl -s -X PUT "$BASE/v1/webhook-endpoint" -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://your-service.a.run.app/pay/events"}'
```

回應帶 `secret` —— 那是你的簽章密鑰。它是**推導**出來的，不是隨機產生存起來的，
所以 `GET /v1/webhook-endpoint` 隨時拿得回同一把，不存在「弄丟了」這條路。

只收 `https://`。內網位址（`10/8`、`172.16/12`、`192.168/16`、`169.254/16`、
loopback）與 `.internal` 一律回 400。

### 二、驗簽（TypeScript）

```ts
import { createHmac, timingSafeEqual } from 'node:crypto';

const TOLERANCE_SECONDS = 300;

export function verify(
  rawBody: Buffer, header: string, secret: string, now: Date,
): boolean {
  // 不要用 split('=', 2) —— JS 的第二個參數是「取幾段」不是「切幾次」，
  // 值裡真的出現 '=' 時它會把後半截默默丟掉。
  const parts: Record<string, string> = {};
  for (const kv of header.split(',')) {
    const i = kv.indexOf('=');
    if (i > 0) parts[kv.slice(0, i).trim()] = kv.slice(i + 1);
  }
  const t = Number(parts.t);
  if (!Number.isFinite(t)) return false;
  if (Math.abs(now.getTime() / 1000 - t) > TOLERANCE_SECONDS) return false;

  const expected = createHmac('sha256', secret)
    .update(`${t}.`).update(rawBody).digest();
  const got = Buffer.from(parts.v1 ?? '', 'hex');
  return got.length === expected.length && timingSafeEqual(got, expected);
}
```

⚠️ **驗簽必須用原始 bytes。** Express 預設會先把 JSON 解析掉，而重新
`JSON.stringify` 出來的字串跟原文**不保證逐位元組相同**（鍵的順序、Unicode
跳脫、空白都可能不同）。接收端要用 `express.raw({ type: 'application/json' })`。

這個 bug 只有在 payload 含非 ASCII 時才發作 —— 而綠界的 `RtnMsg` 是中文。

**簽章向量**（`payment-ecpay` 與 `payment-paypal` **算出來必須逐字相同**，
拿它驗你自己的實作）：

```
WEBHOOK_SIGNING_KEY = "test-signing-key"
caller_id           = "line-translate-bot"
secret              = a6b1f5b99eceb78d8161ce309c2aaa884331bfae5d0f0b438458795953a38a4c

t    = 1756090455
body = {"id":1234,"event_type":"payment.return"}
X-Signature = t=1756090455,v1=5b1967f64135c6dff853b169effe4421cf9a1e0dff72125008c789f3d4bd2b39
```

### 三、body 與 header

body 就是 `GET /v1/events` 回應裡 `items[]` 的**一個元素**，逐欄相同 ——
所以你只要寫一份 parser，兩條路都能吃。

```json
{
  "id": 1234,
  "event_type": "payment.return",
  "subject_kind": "subscription",
  "subject_id": "0f9c1a2b-…",
  "payload": { "…綠界原文…": true },
  "received_at": "2026-08-25T03:14:15.926Z"
}
```

| Header | 說明 |
|---|---|
| `X-Signature` | `t=<unix秒>,v1=<小寫 hex>` |
| `X-Event-Id` | 事件 id。**不可信**，別拿它當去重鍵 |
| `X-Event-Type` | 同 body |
| `X-Delivery-Id` | 拿去 `GET /v1/deliveries` 查案 |
| `X-Delivery-Attempt` | 第幾次嘗試，從 1 起算 |

### 四、你必須處理的四件事

**1. 用 body 裡的 `id` 去重。** 投遞是**至少一次**、**不保證順序**。
`id` 是 `bigserial`，天然單調。用 `X-Event-Id` 去重是錯的 —— 它沒有經過驗簽。

**2. `event_type === "ping"` 要在去重之前就 return。**
ping 的 `id` 固定是 `0`，照順序去重的話第二次 ping 會被你自己擋掉，
看起來像沒送到。

**3. `payment.info` 不代表收到錢。** 那是 ATM／超商的**取號**結果 ——
只有虛擬帳號或繳費代碼與期限，使用者可能幾天後才去繳。
收到錢的是 `payment.return` 且 `payload.RtnCode == "1"`。

**4. 剛註冊完會收到一批過去 48 小時的事件。**
補漏機制只看「有沒有投遞紀錄」，不看「事件落地當下你註冊了沒有」。
這是刻意的 —— 接上推送之前那兩天的續期扣款不會憑空消失。用 `id` 去重就好。

### 五、回什麼

- **2xx** = 收下了，不再重送
- **其他任何回應（含 timeout）** = 我們會重試，最長 12 小時、約 23 次，
  指數退避到一小時封頂

處理失敗時**回 500 讓我們重送** —— 純拉取沒有這個機制（游標一推進就回不去了），
推送把那個安全網還給你，而且由你控制。

### 六、送不到的時候

```bash
# 那筆到底送出去沒有
curl -s "$BASE/v1/deliveries?event_id=1234" -H "X-API-Key: $KEY"

# 修好接收端之後補送
curl -s -X POST "$BASE/v1/events/1234/redeliver" -H "X-API-Key: $KEY"

# 沒有任何真實金流也能驗完整條路
curl -s -X POST "$BASE/v1/webhook-endpoint/test" -H "X-API-Key: $KEY"
```

重試用完仍失敗的會標成 `dead` 並留在 `GET /v1/deliveries` 裡 ——
**我們不會自動停用你的端點**，那是營運決策，不替你做。

⚠️ **改網址不會讓已經排進佇列的投遞改道。**
每一列投遞記的是**排程當下**的網址（這樣「這筆當初送去哪」才答得出來），
所以換了網址之後，還在重試的那些會繼續打舊網址直到重試用完。
要立刻改道就 `redeliver` —— 新建的那一列會用新網址。

⚠️ **拉取端點永遠保留，而且它是推送的安全網。**
推送有界的重試不等於保證送達。跑一支低頻對帳拉取
（例如每天一次 `GET /v1/events?after=<你的游標>`）永遠是對的 ——
那是唯一一層不依賴我們的。

### 密鑰輪替的代價

簽章密鑰由服務端的一把主金鑰推導。**換掉它等於同時換掉所有 caller 的密鑰。**
這是刻意的取捨 —— 逐 caller 輪替換來的是資料庫裡多一欄要保護的明文。
真的要輪替就是一次全部，而且會事先通知。
