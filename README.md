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
| `POST /v1/orders/{id}/refund` | `orders:write` | 退款，**僅信用卡、僅正式環境** |
| `POST /v1/subscriptions` | `subscriptions:write` | 定期定額 |
| `GET /v1/subscriptions/{id}` | `subscriptions:read` | 查，`?refresh=true` 帶每期明細 |
| `POST /v1/subscriptions/{id}/cancel` | `subscriptions:write` | 終止，**不可復原** |
| `GET /v1/events?after=` | `events:read` | 游標拉事件 |
| `GET /health` | — | 壞掉回 503 |

綠界打的端點（不驗 API key，驗 `CheckMacValue`）：
`/ecpay/checkout/{token}`、`/ecpay/return`、`/ecpay/period-return`、
`/ecpay/payment-info`、`/ecpay/order-result`。

## 會咬人的地方

**定期定額的首期回呼跟一次性付款逐位元組相同。** `PeriodType` / `Frequency` /
`ExecTimes` / `TotalSuccessTimes` 只出現在**第二期起**的 `PeriodReturnURL`。
所以「這張單是不是訂閱」必須在建單當下就寫進 DB，回呼時用 `MerchantTradeNo` 查出來。
只存綠界原文的事件表事後永遠分辨不出來。

**取消訂閱不會退錢。** `cancel` 只停後續扣款，已收的不動 ——
caller 應該讓權限給滿最後一期。而且**終止後無法重新啟用**，只能重開一張新單。

**退款在測試環境不存在。** 綠界文件：「因無法提供實際授權，故無法使用此 API」。
dev 上呼叫會拿到帶說明的 400。真正的退刷只能在 prod 驗。

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
| 退款 API | **不存在** | 可用 |
| 海外卡 | — | 不支援（帳號資格） |

dev 用的測試特店是**全球開發者共用**的，所以 `MerchantTradeNo` 一律高熵亂碼 ——
流水號會撞到陌生人的訂單。stage 上也會看到不是你建的訂單。

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
