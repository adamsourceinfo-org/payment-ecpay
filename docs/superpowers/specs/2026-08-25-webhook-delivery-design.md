# payment-ecpay 事件主動推送設計

2026-08-25

## 這是什麼

在現有的 `events` 表上加**第二條出口**：事件落地後主動 POST 給 caller 註冊的網址，
簽章驗真、失敗自動重試、漏掉的由服務自己補。
`GET /v1/events` 與游標語意**一個位元組都不改**。

起因是 `line-translate-bot` 的反饋（`~/repository/adamsourceinfo/2026-08-25-payment-event-push.md`）。
`payment-paypal` 做同一件事，除了服務名稱之外規格逐字相同。

⚠️ **先決條件：`2026-08-26-burst-resilience-design.md` 要先做完。**
那份的 `db.transaction()` 是這份〈排程時機〉的前提，
而那份的第 1 項（事件迴圈阻塞）不修的話，這份會放大它 ——
推送在同一個 handler 裡多加一次對外 HTTP。

**服務的責任只到「通知 caller」為止。** caller 拿到通知之後要推 LINE、開 SSE、
還是讓前端輪詢，是 caller 的設計空間，不在這份文件裡。

## 這份設計推翻了什麼

`docs/superpowers/specs/2026-08-24-payment-ecpay-design.md` 有一條決策：

> **服務不主動推送事件。** caller 用 `GET /v1/events?after=` 游標拉。
> 可靠送達是一整套子系統（重試、退避、死信、對方端點的可用性），caller 越多負擔越重。

那個判斷在當時是對的，推翻它的是兩件當時沒想到的事：

**一、caller 也 scale to zero，「拉取的成本落在 caller 自己身上」不成立。**
`line-translate-bot` 跑在 Cloud Run，沒有流量時沒有任何 process 在跑，也就沒有人去拉。
可行的拉取時機只剩「搭使用者流量的順風車」：

| 情境 | 拉取夠不夠 |
|---|---|
| 使用者剛付完款回到網頁 | ✅ 他自己的請求就會觸發拉取 |
| **每月續期扣款** | ❌ 那筆錢進來時沒有任何人在跟 bot 講話 |

要修掉這個延遲，caller 得自己開一個 Cloud Scheduler、管一把密鑰、維護一支只為了
叫醒自己而存在的端點 —— **而且每一個 caller 都要重做一次**。那正是原決策想避免的
「caller 越多營運負擔越重」，只是負擔跑到了另一邊。

**二、那「一整套子系統」現在租得到。**
重試、指數退避、放棄不必自己寫 —— Cloud Tasks 就是那套子系統，而且它不需要常駐
process，跟 scale-to-zero 天生共存。要寫的只剩兩段：把一筆事件排進佇列、收到時送一次 HTTP。

原決策的最後一句話仍然成立，而且正是這份設計的起點：
「`events` 表就是將來要做推送時的來源。」

---

## 綠界的行為決定了這份設計的參數

三個綠界官方行為直接寫進了下面的設計，先講清楚：

**一、回呼沒收到 `1|OK` 會隔 5~15 分鐘重送、當天四次。**
這是上游的可靠性模型。它的時間尺度是「當天」—— 我們的推送窗口取 12 小時、
sweep 每小時掃一次，落在同一個數量級。不必比綠界自己更執著，也不該差一個數量級。

**二、⚠️ 一旦我們落地並回了 `1|OK`，綠界就不再重送。**
這是整份設計最重要的一條。`events_store.record()` 成功之後綠界的重送會被
`dedupe_key` 擋掉（`record()` 回 `None`），也就是說：

> **排程失敗之後，上游不會再給我們第二次機會。**

所以「事件落地了但沒排出去」這條漏法**無法靠上游重送自癒**，必須由服務自己補。
這是 sweep（見〈安全網〉）存在的唯一理由，不是錦上添花。

**三、綠界沒有全域 event id。**
去重鍵是我們自己造的（`return:{trade_no}:{rtn_code}` 等），對外的識別碼是
`events.id`（`bigserial`）。推送的去重鍵就是它 —— caller 不需要認識綠界的任何欄位。

**邊界：服務推的是原始事件，不做解讀。**
綠界的首期 `payment.return` 同時代表「訂閱建立」與「第一筆扣款」——
那是 caller 要拆成兩件事，不是服務的責任。服務只保證一件事：
**這一筆原文會盡力送到你手上，而且送不到你查得出來。**

---

## 三條不變的原則

### 1. 推送不取代拉取，是第二條出口

`events` 表、`GET /v1/events`、游標語意完全不動。已經在用拉取的 caller 一個字都不用改；
沒註冊端點的 caller 完全不會有推送。

### 2. 推送與拉取送的是同一個東西

推送的 body 就是 `GET /v1/events` 回應裡 `items[]` 的**一個元素**，逐欄相同：

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

caller 寫**一份 parser**，兩條路都能吃。兩個形狀就是兩份程式碼、兩組 bug，
而其中一份平常不會執行 —— 那是最糟的一種程式碼。

**一次一筆，不批次。** 批次要回答「五筆裡第三筆失敗怎麼辦」，那個問題沒有便宜的答案。

### 3. 至少一次，不保證順序

- caller **必須**用 `id` 去重（`bigserial`，天然單調）
- 重試會讓後到的先送達，caller 必須容忍
- 回 2xx = 收下了；其他任何回應（含 timeout）= 重試

「caller 回非 2xx 就重試」把純拉取失去的東西補了回來 —— 純拉取的游標一推進就再也回不去了。

---

## 對外 API

全部前綴 `/v1`、驗 `X-API-Key`，與現有端點一致。新增兩個 scope：
`webhooks:read`、`webhooks:write`。

| 端點 | scope | 說明 |
|---|---|---|
| `PUT /v1/webhook-endpoint` | `webhooks:write` | 註冊／更新推送網址。回應帶簽章密鑰 |
| `GET /v1/webhook-endpoint` | `webhooks:read` | 查目前設定（含密鑰） |
| `DELETE /v1/webhook-endpoint` | `webhooks:write` | 停用推送。事件照樣落地，拉得到 |
| `POST /v1/webhook-endpoint/test` | `webhooks:write` | 送一筆合成的 `ping`，**不落地** |
| `GET /v1/deliveries?event_id=&status=&limit=` | `webhooks:read` | 投遞紀錄 |
| `POST /v1/events/{id}/redeliver` | `webhooks:write` | 重新排一次投遞 |

### 端點清單怎麼管

**今天：一個 caller 一個端點。** 但資料表從第一天就用 `uuid` 當 PK、
`caller_id` 上掛**唯一索引**，日後要放寬只要拿掉那個索引 —— 不用改 PK、不用回填、
`deliveries.endpoint_id` 早就存在。

API 對應今天的形狀，用**單數**路徑（`/v1/webhook-endpoint`）。
日後若真的開放多端點，那是一組新的複數路徑（`/v1/webhook-endpoints`），
單數路徑保留成「預設端點」的捷徑或直接退場 —— 屆時再談，不預先設計。

**`PUT` 是 upsert，保留既有的 `id`。**

```json
{ "url": "https://line-translate-bot-xxxx.a.run.app/pay/events" }
```

回應：

```json
{
  "id": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
  "url": "https://line-translate-bot-xxxx.a.run.app/pay/events",
  "secret": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
  "active": true,
  "updated_at": "2026-08-25T03:14:15.926Z"
}
```

**`DELETE` 是軟停用（`active = false`），不刪列。** 三個理由：
`deliveries.endpoint_id` 的外鍵不會斷、「這筆當初送去哪」永遠答得出來、
caller 停用再啟用時 `id` 不變。重新 `PUT` 同一個 caller 會把 `active` 設回 `true`。

**密鑰每次都給**（它是推導出來的，隨時算得回來，見〈簽章〉）——
所以不需要「只顯示一次」那套儀式，也不存在「密鑰弄丟了」這條路。

`PUT` 一律回 **200**（不是 201）—— 它是 upsert，caller 不需要分辨這次是建立還是更新。
`DELETE` 回 200 帶更新後的物件（`active: false`），不是 204 ——
caller 因此不必再打一次 `GET` 確認。

推送未設定時（缺 `WEBHOOK_SIGNING_KEY` 或 `INTERNAL_KEY`），
`PUT`／`GET`／`DELETE`／`test` **一律回 503**：沒有簽章密鑰就算不出 `secret`，
回一個沒有 `secret` 的物件只會讓 caller 拿著空字串去驗簽。

### 其餘兩支的回應形狀

`GET /v1/deliveries?event_id=&status=&limit=`（`limit` 預設 100、上限 500，
依 `created_at DESC` 排序，只看得到自己的）：

```json
{
  "items": [{
    "id": "…", "event_id": 1234, "endpoint_id": "…",
    "url": "https://…/pay/events",
    "status": "failed", "attempts": 3,
    "last_status": 500, "last_error": "…",
    "created_at": "…", "updated_at": "…", "delivered_at": null
  }]
}
```

`POST /v1/events/{id}/redeliver` 回 **202** 與新建的那一列（形狀同上，
`status: "pending"`、`attempts: 0`）。202 而不是 200：排進去了不等於送到了。

### 網址驗證擋在進門處

註冊時就擋，不要等第一次投遞才失敗。以下一律回 400：

- 非 `https://`
- 帶 userinfo（`https://user:pass@host/`）
- host 是 IP 字面值且落在 loopback（`127/8`、`::1`）、私有網段
  （`10/8`、`172.16/12`、`192.168/16`、`fc00::/7`）或 link-local（`169.254/16`、`fe80::/10`）
- host 是 `metadata.google.internal` 或以 `.internal` 結尾

⚠️ **這擋不住 SSRF 的全部，要誠實寫進 README。**
caller 完全可以註冊一個公開網域，讓它的 A record 指到 `169.254.169.254`，
或註冊之後才改 DNS。所以**送出當下還要再擋一次**（見〈投遞〉）。
即便如此仍有 DNS TOCTOU 的殘餘風險 —— 這是「持有效 API key 的 caller 才做得到」的
風險，我們接受它，但不用「已擋 SSRF」的語氣描述。

網址變更寫進 log：誰、什麼時候、從哪改到哪。

### `redeliver` 要擋跨 caller

`events.id` 是全域 `bigserial`，所有 caller 共用同一個序號空間 ——
不擋的話，caller 可以拿別人的 id 去試探。
**別人的事件（含 `caller_id IS NULL`）一律回 404，不是 403**，
跟既有的 `app/errors.py:not_found()` 慣例一致。

`redeliver` 建的是**新的一列** `deliveries`，不是重置舊的那列 ——
`GET /v1/deliveries?event_id=` 因此看得到完整的投遞史。

---

## 簽章

### 密鑰怎麼來：推導，不儲存

```
secret = hex( HMAC-SHA256( key = WEBHOOK_SIGNING_KEY, msg = caller_id ) )
```

`WEBHOOK_SIGNING_KEY` 是**每個服務、每個環境一把**，放 Secret Manager。

**為什麼不是「每個端點隨機一把存進 DB」**：API key 是**入站**認證，服務只需要
*驗證*它，所以存 sha256 就夠了；簽章密鑰是**出站**的，服務必須*持有*它才簽得出來
—— 存 hash 沒有意義，存明文就是資料庫裡多一欄機密。推導的話資料庫裡一個字都沒有。

**為什麼綁 `caller_id` 而不是 `endpoint_id`**：日後開放多端點時，同一個 caller
的每個端點共用同一把密鑰 —— 那是同一個信任邊界，分開沒有帶來任何隔離，
卻要 caller 記住「哪一把對應哪一個端點」。而且推導不必先讀 DB。

⚠️ **代價要寫進 README**：換掉 `WEBHOOK_SIGNING_KEY` 等於**同時**換掉所有 caller
的密鑰。這是刻意的取捨 —— 逐 caller 輪替換來的是一欄要保護的明文，不划算。
真的要輪替就是一次全部，並事先通知 caller。

### 格式

```
X-Signature: t=1756090455,v1=5257a869e7ecebeda32affa62cdca3fa51cad7e77a7e3e0a…
```

- `t` = Unix 秒
- `v1` = `HMAC-SHA256(secret, "{t}." + raw_body)` 的**小寫 hex**
- 簽的是**原始 bytes**，不是重新序列化過的 JSON

caller 驗證時：`|now - t| > 300` 直接拒（防重放），hex 比對用 constant-time。

### 其他 header

| Header | 值 |
|---|---|
| `Content-Type` | `application/json` |
| `X-Event-Id` | 事件 id（`ping` 是 `0`） |
| `X-Event-Type` | 同 body 的 `event_type` |
| `X-Delivery-Id` | `deliveries.id`，用來對 `GET /v1/deliveries` 查案 |
| `X-Delivery-Attempt` | `deliveries.attempts`（遞增後的值），從 `1` 起算 |
| `User-Agent` | `payment-ecpay/1` |

⚠️ **`X-Delivery-Attempt` 的來源是我們自己的欄位，不是 Cloud Tasks 的 header。**
sweep 重排過的 delivery 會拿到一個**全新的** task，它的
`X-CloudTasks-TaskRetryCount` 從 0 重新算 —— 用它的話 caller 會看到
「第 1 次嘗試」出現在已經失敗二十次的 delivery 上。
`deliveries.attempts` 跨 task 累加，才是 caller 真正想知道的那個數字。

`X-Event-Id` 放 header 是為了讓 caller 在**還沒驗簽之前**就記得下 log。
它不可信，**別拿它當去重鍵** —— 去重要用驗過簽的 body 裡的 `id`。

### 簽章向量

固定 key / `t` / body → 固定 hex，寫進 README，讓 caller 拿去驗自己的實作。

⚠️ **向量由 `payment-paypal` 產生，`payment-ecpay` 逐字複製。**
各自產生一組「應該相同」的向量，等於沒有向量。

---

## 投遞機制

Cloud Run scale to zero，沒有常駐 process 可以跑重試迴圈。
這是整份設計唯一真正困難的地方，而 Cloud Tasks 就是答案 —— **是租的不是寫的**。

### 排程

事件落地後，建一個 Cloud Task 指向服務自己的一支內部端點：

```
POST https://<服務網址>/internal/deliveries/{delivery_id}
X-Internal-Key: <env 裡的機密>
```

task 的 body 是空的 —— 需要的東西全部在 `deliveries` 那一列裡，
把 payload 塞進 task 只會多出一份會過期的副本。

### ⚠️ 排程時機：不是「commit 之後」，是「handler 全部做完之後」

這一條要對著 `app/db.py` 看才成立。`get_conn()`（`app/db.py:76`）在每次
`db.query()` 結束時就 commit —— 也就是說 `events_store.record()` 一回傳就已經
commit 了，「commit 之後才排程」自動成立，**但它保護不了真正的 race**。

`app/routers/callbacks.py:191` 拿到 `new_id`，**之後**才跑
`subs_store.mark_active()`（`:203`）、`record_charge()`、`set_totals()` ——
而那些是**各自獨立的交易**。一拿到 id 就排程的話，Cloud Tasks 可以在幾十毫秒內送達，
caller 收到推送立刻回頭打 `GET /v1/subscriptions/{id}`，讀到的是**還沒 active 的訂閱**。

這比「事件那一列還看不到、端點回 404」難查得多 —— 它不是壞掉，是偶爾看到舊狀態。

> **規則：排程是交易 commit 之後、`return PlainTextResponse(ACK)` 之前的最後一件事。**

三支回呼都一樣。

⚠️ **`record()` 回 `None` 的路徑不能只是早退。**
原本 `None` 的意思是「綠界重送，什麼都不用做」。但〈突發韌性〉第 5 項讓
`/ecpay/order-result` 成為第二個入口之後，`None` 多了一個意思：
**「這筆已經被導回那條路處理掉了」**。

如果照舊早退，就沒有人排推送 —— 靜默退化成 sweep 的一小時延遲，
而且正好發生在活動期間。

> **規則：`/ecpay/return` 的 `None` 路徑改成「確保 delivery 列存在」**
> —— 一次便宜的查詢，沒有就補排。

`order-result` 本身**不排推送**（導回路徑要快，而幕後回呼一定會到）。

⚠️ **enqueue 要有短 timeout（2 秒）。**
建 task 是一次對外 HTTP，而它就在回綠界 `1|OK` 的路徑上。Cloud Tasks API 一慢，
ACK 就慢，綠界超時就重送 —— 事故當下再加一輪流量。逾時就標
`failed`/`attempts = 0`，交給 sweep 補。

「其實排成功了但我們以為失敗」會產生重複投遞 —— 至少一次的語意本來就涵蓋它，
caller 用 `id` 去重。

### ⚠️ 排程失敗不可以讓回呼回非 `1|OK`

上游的重送是為了「事件沒收到」，不是為了「我們沒轉給 caller」。
事件已經落地了，排程失敗就 log ERROR、把 delivery 標成 `failed`，然後**照樣回 `1|OK`**。
補救靠 sweep（見下節）。

反過來做的話：綠界重送 → `dedupe_key` 擋掉 → `record()` 回 `None` → 早退 →
**永遠不會補推**，而且我們還白白讓綠界重送了四次。

### ⚠️ 一個 caller 一個 queue

前提是**多個 caller、行銷活動集中付款**（見
`2026-08-26-burst-resilience-design.md`）。`--max-concurrent-dispatches`
是**每個 queue** 的設定，所以共用一個 queue 意味著：一個 caller 的端點 timeout
10 秒就能佔滿全部派送槽位，**排隊擋住其他所有 caller 的通知**。
活動當天這等於「A 公司的活動把 B 公司的通知全排隊了」。

queue 名字由 `caller_id` 推導：`payment-ecpay-deliveries-{sanitized}`
（Cloud Tasks 的 queue id 只收 `[A-Za-z0-9-]`、上限 100 字元，
其餘字元換成 `-`，再接 `caller_id` 的 sha256 前 8 碼避免消毒後撞名）。

**queue 在 `scripts/add-caller.sh` 上線 caller 時建，不由服務動態建。**
caller 上線本來就是人工步驟，多一行 `gcloud tasks queues create` 就換到完全隔離
—— 而且 runtime SA **不需要任何建 queue 的權限**。對一個 `allow_unauthenticated`
的服務來說，不給 `cloudtasks.admin` 是值得的。

⚠️ **找不到 caller 的 queue 就退回共用的預設 queue，並 log ERROR。**
沒有人會因為漏跑一行 gcloud 就掉事件；但那一行 ERROR 要吵，
否則所有 caller 會靜靜地退化回共用 queue，公平性消失而沒有人知道。

被否決的替代：固定 8 個 shard，`crc32(caller_id) % 8` 分配。
runbook 固定、不隨 caller 增加，但隔離不完美 —— 同 shard 的 caller 仍然互相影響。
既然 caller 上線已經是人工的，就沒有理由接受不完美的隔離。

### queue 設定

每個 queue（共用的與 per-caller 的）都用同一組參數：

```bash
gcloud tasks queues create "${QUEUE}" \
  --location=asia-east1 --project=adamsourceinfo-dev \
  --max-retry-duration=12h \
  --max-attempts=30 \
  --min-backoff=10s --max-backoff=1h --max-doublings=5 \
  --max-concurrent-dispatches=10
```

⚠️ **`max-concurrent-dispatches` 的上限實際上是 DB 連線數決定的，不是 Cloud Tasks。**
每一次投遞都會打回自己的 `/internal/deliveries/{id}`，變成一個新的 inbound 請求、
借走一條 DB 連線。所以總併發（`queue 數 × 10`）要放進
`實例數 × DB_POOL_MAX ≤ Cloud SQL 連線預算` 一起算。
在〈突發韌性〉的第 1、2 項修好之前，拉高這個數字沒有意義。

⚠️ **主要旋鈕是 `--max-retry-duration`，不是 `--max-attempts`。**
caller 反饋原文寫「八次、10 秒起跳退避到一小時封頂，總時長約 12 小時」——
那組參數算出來**不是** 12 小時，是 **21 分鐘**。Cloud Tasks 的退避規則是：
從 `minBackoff` 起跳、加倍 `maxDoublings` 次，之後**線性**加 `2^maxDoublings × minBackoff`，
最後才封頂在 `maxBackoff`。代進 `--min-backoff=10s --max-doublings=5 --max-attempts=8`
（7 次重試）：

```
10 + 20 + 40 + 80 + 160 + 320 + 640 = 1270 秒 ≈ 21 分鐘
```

`--max-backoff=1h` 根本沒被碰到。caller 做一次平常的部署就會把那段期間的事件全打成死信。

用 `--max-retry-duration=12h` 之後，牆鐘時間直接寫在設定裡，不需要任何人去累加
等比級數。`--max-attempts=30` 只是失控保險，實際上永遠不會綁到（12 小時內約 23 次）。
實際重試點：

```
10s 20s 40s 80s 2m40s 5m20s 10m40s
16m 21m 27m 32m 37m 43m 48m 53m 59m
之後每小時一次，直到滿 12 小時
```

### 送出時再擋一次 SSRF

- `follow_redirects=False`（httpx 預設就是，寫死別讓人改）
- 送出前用 `socket.getaddrinfo` 解析 host，任何一個解析結果落在
  loopback／私有／link-local 就**不送**，直接標 `failed` 並記下原因
- timeout 由 `WEBHOOK_TIMEOUT_SECONDS` 控制（預設 10 秒）

### 內部端點怎麼擋

服務是 `allow_unauthenticated: true`（綠界的回呼必須打得到），
所以 `/internal/*` 對公網開著，必須在**應用層**擋：建 task 時帶
`X-Internal-Key: <env 裡的機密>`，端點用 constant-time 比對，不符回 401。

**為什麼不是 OIDC**：要引入 `google-auth` 驗簽。這個 repo 連 DB driver 都挑 `pg8000`
是為了不編譯，為一支內部端點拉進整包驗證函式庫不划算。而且 Cloud Tasks 用 OIDC 時
runtime SA 對自己**沒有**隱含的 `actAs`，那個 IAM 授權還要另外補進 runbook。

靜態 header 跟 API key 是同一個安全模型（一個共享機密，只存在服務與呼叫方之間），
而這裡的呼叫方就是服務自己。

⚠️ **「零新相依」只有在建 task 也不裝套件時才成立。**
官方的 `google-cloud-tasks` 會把 `google-auth` 整包拉進來 —— 那正是拒絕 OIDC
想省掉的東西。**直接打 REST**：

```
POST https://cloudtasks.googleapis.com/v2/projects/{p}/locations/{l}/queues/{q}/tasks
GET  https://cloudtasks.googleapis.com/v2/projects/{p}/locations/{l}/queues/{q}
```

access token 用 `app/db.py:44` 已經在用的 `iam_token()` —— Cloud Run 的
metadata default token 本來就是 `cloud-platform` scope，**不需要換 scope**。
專案 id 也向 metadata server 要（`/computeMetadata/v1/project/project-id`），
不多一個環境變數 —— 跟 `db_status()` 那條「回音環境變數證明不了任何事」同一個精神。
出站 HTTP 用 `httpx`，已經在 `requirements.txt` 裡。**整條路一個新套件都不需要。**

---

## 安全網：sweep

Cloud Tasks 的重試是有界的，而有界的重試不等於保證送達。加上綠界不會給我們
第二次機會（見上文），兩條會漏的路必須由服務自己補：

| 漏法 | 什麼時候發生 |
|---|---|
| 事件已落地，但 `deliveries` 那一列還沒建 | 事件 commit 之後、排程之前掛掉 |
| `deliveries` 列建了，但 task 沒建成 | Cloud Tasks API 當下不可用 |
| 重試用完仍失敗 | caller 壞掉超過 12 小時 |

一支端點加一個 Cloud Scheduler job 全部處理掉：

```
POST /internal/deliveries/sweep      ← Cloud Scheduler 每小時，帶 X-Internal-Key
```

做三件事：

**一、補漏。** 有 active 端點、事件已落地、卻連一列 `deliveries` 都沒有的，補排一次。

```sql
SELECT e.id, e.caller_id, w.id AS endpoint_id, w.url
FROM events e
JOIN webhook_endpoints w ON w.caller_id = e.caller_id AND w.active
LEFT JOIN deliveries d ON d.event_id = e.id AND d.endpoint_id = w.id
WHERE e.caller_id IS NOT NULL
  AND e.received_at >  now() - interval '48 hours'
  AND e.received_at <  now() - interval '5 minutes'
  AND d.id IS NULL
ORDER BY e.id
LIMIT 500
```

⚠️ **`received_at < now() - 5 minutes` 那一行不能省。** 沒有它，sweep 會跟正常路徑
（回呼裡剛落地、正要排程的那一筆）賽跑，結果是同一筆事件排兩次。
`48 hours` 的上界則是為了讓這個查詢有界 —— 更舊的漏掉已經無望，那是 `redeliver` 的事。

⚠️ **`LIMIT 500` 是個靜默上限，要嘛掃乾淨、要嘛吵。**
突發如果漏了 10,000 筆，每小時補 500 就要 **20 小時**才排乾，
而過程中沒有任何人知道它正在追進度 —— 從外面看起來就像「已經全部補完了」。

作法：迴圈掃到當輪回不滿 `LIMIT` 為止，並且**設一個每輪總量上限**
（`_SWEEP_MAX_PER_RUN = 5000`）避免一次 sweep 跑到 Cloud Run 的請求逾時。
撞到那個上限時 log 一筆 WARNING 說明還剩多少 —— caller 反饋原文自己的原則：
有上限就要說出來。

⚠️ **這個查詢會回填新註冊的端點，那是刻意的。**
它只看「有沒有 delivery 列」，不看「事件落地當下端點在不在」——
所以 caller 第一次 `PUT` 之後（或 `DELETE` 之後再啟用），下一輪 sweep 會把它
**過去 48 小時內的事件補推一次**。這跟〈什麼事件會被推送〉那張表不衝突：
那張表講的是**落地當下**排不排程，這裡講的是事後補。

之所以不加 `activated_at` 去排除它們：

- 契約上安全 —— 原則 3 已經要求 caller 用 `id` 去重，重複收到是預期內的
- 對新 caller 是**功能不是 bug**：接上推送之前那兩天的續期扣款不會憑空消失
- 要排除的話得多一個 `activated_at` 欄位（不能用 `updated_at`，改網址也會動到它），
  而它存在的唯一理由是關掉一個有用的行為

README 必須寫清楚：**剛註冊完會收到一批過去 48 小時的事件，用 `id` 去重。**

**二、標死信。** 佇列已經放棄的，標成 `dead` 並 log 一筆 ERROR。
不標的話，「送不出去的事件」只存在於 Cloud Tasks 的統計裡，**服務自己答不出來**——
而那正是這個欄位存在的唯一理由。

**三、重排從未派送成功的。** `status = 'failed' AND attempts = 0` 唯一地代表
「`deliveries` 列建了但 task 沒建成」。重建一次 task，沿用同一列。

### ⚠️ `dead` 的門檻向 queue 本人問，不要抄

caller 反饋原文要求 `WEBHOOK_MAX_ATTEMPTS` 與 queue 的 `--max-attempts` 保持一致，
並自己標註了風險：改一邊沒跟著改另一邊，症狀是**死信永遠不會被標記**。
那個一致性沒有任何東西在守 —— 所以這份設計不留那個 env。

sweep 每次執行時 `GET` queue 自己的設定，門檻取 `2 × retryConfig.maxRetryDuration`：

```python
window = tasks.queue_retry_window()        # GET .../queues/{q}，讀 maxRetryDuration
threshold = 2 * window                     # 12h → 24h
```

queue 的 `retryConfig` 因此是**唯一真相**。改 queue 不用改 code，也不可能漂移。
取不到（API 暫時不可用）就退回 24 小時常數並 log WARNING —— 抓寬在兩個方向都安全：
不會誤判還在重試的，真死信最晚 24 小時內也看得到。

```sql
UPDATE deliveries SET status = 'dead', updated_at = now()
WHERE status IN ('pending', 'failed')
  AND created_at < now() - %s::interval
RETURNING id, event_id, caller_id, url, attempts, last_status, last_error
```

### 順帶：判斷「最後一次」的 header 也要換掉

caller 反饋原文說用 `X-CloudTasks-TaskExecutionCount` 判斷是不是最後一次嘗試。
那個 header 的官方定義**不計入 5XX 造成的失敗** —— 而 `/internal/deliveries/{id}`
自己就可能回 5xx（DB 抽一下），那些次數不會被算進去，
`count + 1 >= max_attempts` 於是永遠不成立。症狀恰好就是原文自己標的那個 ⚠️：
死信永遠不會被標記，只是原因不是 env 漂移，是選錯了 header。

這份設計根本不在投遞當下判死信（交給 sweep），所以這個坑自然消失。

而且**兩個 Cloud Tasks 的計數 header 都不用**：`X-Delivery-Attempt` 取自
`deliveries.attempts`（見〈簽章〉）。整條路因此不依賴任何 Cloud Tasks 專有 header，
只依賴 `X-Internal-Key` —— 這也讓 sweep 重排、`redeliver`、以及日後若換掉佇列，
三種情況下的計數語意保持一致。

---

## 資料模型

`migrations/006_webhook_delivery.sql`。

```sql
-- 今天：一個 caller 一個端點。
-- 但 PK 用 uuid、唯一性靠下面那個索引 —— 日後放寬只要拿掉索引，
-- 不用改 PK、不用回填，deliveries.endpoint_id 從第一天就存在。
CREATE TABLE IF NOT EXISTS webhook_endpoints (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  caller_id  text NOT NULL,
  url        text NOT NULL,
  active     boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
-- ⚠️ 這一行就是「一個 caller 一個端點」的全部實作。要開放多端點時刪掉它。
CREATE UNIQUE INDEX IF NOT EXISTS webhook_endpoints_caller
  ON webhook_endpoints (caller_id);
-- 刻意沒有 secret 欄位：簽章密鑰由 WEBHOOK_SIGNING_KEY 推導，
-- 資料庫裡一個機密都不留。

CREATE TABLE IF NOT EXISTS deliveries (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  -- NULL = 這是 POST /v1/webhook-endpoint/test 送的 ping，沒有對應事件。
  -- ping 因此走的是跟真事件**完全相同**的佇列與端點，不是同步送一次。
  event_id        bigint REFERENCES events(id),
  endpoint_id     uuid NOT NULL REFERENCES webhook_endpoints(id),
  caller_id       text NOT NULL,
  -- 排程當下的網址。caller 之後改了網址，「這筆當初送去哪」還答得出來。
  url             text NOT NULL,
  -- pending  已建列、task 已排、還沒有任何投遞結果
  -- delivered caller 回了 2xx
  -- failed   至少一次失敗，佇列還在重試（attempts = 0 代表 task 根本沒建成）
  -- dead     放棄。只由 sweep 標記，投遞當下不標
  status          text NOT NULL DEFAULT 'pending',
  attempts        integer NOT NULL DEFAULT 0,
  last_status     integer,            -- caller 回的 HTTP 狀態碼
  last_error      text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  delivered_at    timestamptz
);
CREATE INDEX IF NOT EXISTS deliveries_event  ON deliveries (event_id);
CREATE INDEX IF NOT EXISTS deliveries_caller ON deliveries (caller_id, created_at DESC);
-- sweep 標死信用
CREATE INDEX IF NOT EXISTS deliveries_open
  ON deliveries (created_at) WHERE status IN ('pending', 'failed');
-- sweep 補漏用：events 依落地時間掃，只掃認得出 caller 的
CREATE INDEX IF NOT EXISTS events_recent
  ON events (received_at) WHERE caller_id IS NOT NULL;
```

`deliveries` 沒有 `(event_id, endpoint_id)` 唯一約束 —— `redeliver` 本來就要建新列。
至少一次的語意允許同一筆事件有多列。

### 狀態機

```
  schedule()
     │  INSERT pending
     ├── task 建成 ─────────────────────► pending
     └── task 建失敗 ──► failed (attempts = 0, last_error = "enqueue: …")
                              │
                              └── sweep 重排 ──► pending

  /internal/deliveries/{id}     attempts += 1
     ├── caller 回 2xx ────────► delivered   （端點回 200，佇列停手）
     └── 其他 ─────────────────► failed      （端點回 502，佇列重試）

  sweep（每小時）
     pending / failed 且超過 2 × queue 窗口 ──► dead + ERROR log
```

⚠️ **內部端點的回應碼是給 Cloud Tasks 看的，不是給人看的。**
投遞失敗必須回非 2xx，佇列才會重試 —— 但 DB 的更新要先做完。
delivery 已經是 `delivered` 或 `dead` 時（佇列重複派送，至少一次）
**直接回 200 且不重送給 caller**。

---

## 什麼事件會被推送

服務目前只有三個事件落地點，全部在 `app/routers/callbacks.py`：

| 端點 | `event_type` | 說明 |
|---|---|---|
| `/ecpay/return` | `payment.return` | 付款結果，**含定期定額首期** |
| `/ecpay/period-return` | `subscription.charge` | 定期定額第二期起 |
| `/ecpay/payment-info` | `payment.info` | ATM／超商取號 —— **還沒收到錢** |

推不推的判斷：

| 條件 | 推不推 |
|---|---|
| `events.record()` 回了新 id | ✅ 推 |
| `record()` 回 `None`（綠界重送、被去重擋掉） | ❌ 不推 |
| `caller_id IS NULL`（對應不到 caller） | ❌ **不推** —— 那筆對每個 caller 都不可見，推了就是洩漏 |
| caller 沒註冊端點，或 `active = false` | ❌ 不推 |
| 推送未設定（缺 secret） | ❌ 不推，log 一次 WARNING |

⚠️ 這張表講的是**事件落地當下**排不排程。caller 事後才註冊端點的話，
sweep 會把過去 48 小時的事件補推一次 —— 見〈安全網〉的那條說明。

### ping

`POST /v1/webhook-endpoint/test` 建一列 `event_id IS NULL` 的 `deliveries`，
**走跟真事件完全相同的佇列與內部端點**。body：

```json
{ "id": 0, "event_type": "ping", "subject_kind": null,
  "subject_id": null, "payload": {}, "received_at": "…" }
```

⚠️ **為什麼堅持走真佇列**：如果 ping 是同步直送，它就跳過了 Cloud Tasks、
內部端點、`X-Internal-Key`、重試 —— 而那四樣正好是最會壞的部分。
「在沒有任何真實金流的情況下驗完整條路」如果驗不到那四樣，這支端點就沒有存在的意義。
`deliveries.event_id` 可為 NULL 就是為了這件事。

⚠️ **ping 的 `id` 固定是 `0`。** caller 照原則 3 用 `id` 去重的話，
第二次 ping 會被自己的去重擋掉，看起來像沒送到。
README 必須寫：**`event_type == "ping"` 要在去重之前就 return。**

`events` 表**不會多出任何一列**。

---

## 模組切分

跟既有的三層切分（`ecpay/` 上游、`store/` 資料、`routers/` 對外）一致。

```
app/webhooks/__init__.py
app/webhooks/signing.py     密鑰推導、簽章產生（純函式，好寫向量測試）
app/webhooks/targets.py     網址驗證：註冊時的字面檢查 + 送出時的解析後 IP 檢查
                            （不叫 urls.py —— app/urls.py 是回呼網址推導，別混淆）
app/webhooks/tasks.py       Cloud Tasks REST：建 task、讀 queue retryConfig
app/webhooks/dispatch.py    schedule() / deliver() / sweep() —— 不碰 FastAPI
app/store/webhook_endpoints.py
app/store/deliveries.py
app/routers/webhooks.py     /v1/webhook-endpoint*、/v1/deliveries、/v1/events/{id}/redeliver
app/routers/internal.py     /internal/deliveries/{id}、/internal/deliveries/sweep
migrations/006_webhook_delivery.sql
scripts/grant-scope.sh      給既有 caller 補 scope（見 runbook）
```

`callbacks.py` 每支回呼在交易 commit 之後、`return ACK` 之前呼叫
`dispatch.schedule(new_id, caller_id)`；`None` 路徑改呼叫
`dispatch.ensure(dedupe_key, caller_id)`。

⚠️ **`schedule()` 與 `ensure()` 永遠不對外拋例外。**
它們自己 try/except、log ERROR、標 `failed`。回呼的 `1|OK` 不能因為推送而失守。

⚠️ **併發模型：`app/webhooks/` 底下全部是同步程式碼。**
它們由回呼的 threadpool 路徑呼叫（見〈突發韌性〉第 1 項），
出站 HTTP 用同步的 `httpx.Client`，不是 `AsyncClient`。
不要在這裡引入 async —— 混用正是那份文件第 1 項那個 bug 的來源。

---

## 設定

```
.cicd/env.common
  WEBHOOK_TIMEOUT_SECONDS=10
  WEBHOOK_ENQUEUE_TIMEOUT_SECONDS=2
  TASKS_QUEUE_PREFIX=payment-ecpay-deliveries
  TASKS_LOCATION=asia-east1
  # 兩個環境一模一樣，所以放 common —— 抄兩遍的東西遲早會分歧。
  # 環境靠 project ID 識別（向 metadata server 要），queue 名字不需要跟著分。
  # per-caller queue 是 {prefix}-{sanitized-caller}；prefix 本身是退路用的共用 queue。

.cicd/secrets.dev
  WEBHOOK_SIGNING_KEY=payment-ecpay-webhook-signing-key-dev:latest
  INTERNAL_KEY=payment-ecpay-internal-key-dev:latest
.cicd/secrets.prod
  （同上，換成 -prod）
```

**沒有 `WEBHOOK_MAX_ATTEMPTS`。** 重試次數與窗口的唯一真相是 queue 的 `retryConfig`；
sweep 需要那個數字時向 queue 本人要。

### ⚠️ 缺席時降級，不是啟動失敗

`app/config.py` 的開頭寫著「缺少必要變數就啟動失敗」，但這兩把要跟
`ECPAY_CREDIT_CHECK_CODE` 一樣走 `Optional`：

- 缺 `WEBHOOK_SIGNING_KEY` 或 `INTERNAL_KEY` → 推送整個關閉
- `/health` 的 `push` 回 `"unconfigured"`
- `/v1/webhook-endpoint` 四支、`/v1/deliveries`、`redeliver` 一律回 503
- 回呼照常運作，事件照常落地，`GET /v1/events` 照常拉得到

理由跟既有那個一模一樣：第一次部署時 secret 還沒建，硬性必填會讓服務起不來，
而**沒有推送的服務仍然是完全可用的服務**。

新增 `Settings.push_configured` property，判斷集中在一處。

### 記得補 `_RedactFilter`

`app/main.py:20` 現在只遮 `hash_key` / `hash_iv`。
`internal_key` 與 `webhook_signing_key` 要加進去。

⚠️ 推導出來的**逐 caller 密鑰**不在這個名單裡（它是無界集合，遮不完）。
所以規則是：**`PUT`／`GET /v1/webhook-endpoint` 的回應 body 永遠不進 log。**
router 裡不要有任何 `log.debug(response)` 之類的東西。

---

## 健康檢查

`/health` 的 `ecpay` 區塊旁邊多一個 `push`：

```json
"push": {
  "configured": true,
  "queue": "payment-ecpay-deliveries",
  "endpoints_active": 1,
  "dead_last_24h": 0
}
```

⚠️ **`dead > 0` 不讓 `/health` 回 503。**
503 的意思是「這個服務壞了」，而積壓的死信通常代表**caller** 壞了。
用 503 表達它會讓 ci 的 smoke 因為別人的故障而紅燈 ——
那個檢查就從「我們部署成功了嗎」變成「所有 caller 今天都好嗎」，而後者不是它的工作。
報告它，不要用它決定成敗。

---

## 一次性 runbook（人跑，每個環境一次）

```bash
ENV=dev; PROJECT=adamsourceinfo-${ENV}; SVC=payment-ecpay; REGION=asia-east1
URL="$(gcloud run services describe "$SVC" --region="$REGION" \
        --project="$PROJECT" --format='value(status.url)')"

# 1. 開 API
gcloud services enable cloudtasks.googleapis.com cloudscheduler.googleapis.com \
  --project="$PROJECT"

# 2. 兩把機密
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' | \
  gcloud secrets create "${SVC}-webhook-signing-key-${ENV}" \
    --replication-policy=automatic --data-file=- --project="$PROJECT"
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' | \
  gcloud secrets create "${SVC}-internal-key-${ENV}" \
    --replication-policy=automatic --data-file=- --project="$PROJECT"

# 3. 授權給執行身分
#    ⚠️ 是這個服務的 runtime_sa（.cicd/config.yml 的 run-payment-ecpay），
#    不是共用的 run-runtime。provision-service.sh 的第 4 步只在「當初跑腳本時」
#    授權過一次 —— 現在補的機密沒有人會替你授權，漏了的話新 revision 直接起不來。
for S in "${SVC}-webhook-signing-key-${ENV}" "${SVC}-internal-key-${ENV}"; do
  gcloud secrets add-iam-policy-binding "$S" --project="$PROJECT" \
    --member="serviceAccount:run-${SVC}@${PROJECT}.iam.gserviceaccount.com" \
    --role=roles/secretmanager.secretAccessor
done

# 4. 共用的退路 queue（per-caller 的那些由 add-caller.sh 建）
#    ⚠️ 主要旋鈕是 max-retry-duration。max-attempts 只是失控保險。
#    12 小時內實際會派送約 23 次，永遠碰不到 30。
gcloud tasks queues create "${SVC}-deliveries" --location="$REGION" \
  --project="$PROJECT" \
  --max-retry-duration=12h --max-attempts=30 \
  --min-backoff=10s --max-backoff=1h --max-doublings=5 \
  --max-concurrent-dispatches=10

# 5. 讓執行身分排得進 task、也讀得到 queue 設定（sweep 判死信要）
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:run-${SVC}@${PROJECT}.iam.gserviceaccount.com" \
  --role=roles/cloudtasks.enqueuer --condition=None
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:run-${SVC}@${PROJECT}.iam.gserviceaccount.com" \
  --role=roles/cloudtasks.viewer --condition=None

# 6. Scheduler：每小時掃一次
#    ⚠️ INTERNAL_KEY 會出現在 job 設定裡，任何有 scheduler.viewer 的人看得到。
#    它靠專案的 IAM 保護，不是靠對專案成員保密 —— 跟 Cloud Task 的 header 同一個模型。
KEY="$(gcloud secrets versions access latest \
        --secret="${SVC}-internal-key-${ENV}" --project="$PROJECT")"
gcloud scheduler jobs create http "${SVC}-deliveries-sweep" \
  --location="$REGION" --project="$PROJECT" \
  --schedule="7 * * * *" --time-zone=Asia/Taipei \
  --uri="${URL}/internal/deliveries/sweep" --http-method=POST \
  --headers="X-Internal-Key=${KEY}" \
  --attempt-deadline=300s

# 7. 既有 caller 補 scope（新 scope 不會自己長出來）
./scripts/grant-scope.sh "$ENV" line-translate-bot webhooks:read,webhooks:write

# 8. 複查 —— ci README 那條「併發下 add-iam-policy-binding 可能靜靜掉一筆」的教訓
gcloud projects get-iam-policy "$PROJECT" --flatten="bindings[].members" \
  --filter="bindings.members:run-${SVC}@" --format="value(bindings.role)"
```

`scripts/grant-scope.sh` 是新的一支，形狀照抄 `scripts/add-caller.sh`
（cloud-sql-proxy + IAM token 當密碼）。`add-caller.sh` 只能新增不能改，
而這次要改的是既有的 key。

⚠️ **它必須是「附加」，不是「覆寫」。**

```sql
UPDATE api_keys
   SET scopes = ARRAY(SELECT DISTINCT unnest(scopes || %s::text[]))
 WHERE caller_id = %s AND active;
```

runbook 第 7 步只傳 `webhooks:read,webhooks:write` ——
如果實作寫成 `SET scopes = %s`，那一行會把 `line-translate-bot` 在 prod 的
`orders:*` 與 `events:read` **整組刪掉**，當場打斷他們正在跑的金流。
一支只在 runbook 裡出現一次的腳本，錯了不會有人在 code review 抓到，
所以語意寫進設計文件，不是留給實作決定。

執行後必須印出更新結果讓人肉眼確認：

```
  caller : line-translate-bot
  scopes : orders:read, orders:write, events:read, webhooks:read, webhooks:write
           （新增 2 個）
```

---

## 測試要求

沿用 `tests/conftest.py` 的 `FakeSettings` + monkeypatch store 的作法，
不碰真的 DB、不碰真的 Cloud Tasks。

**簽章**
- 固定 key / `t` / body → 固定 hex（與 `payment-paypal` **逐字相同**的向量）
- 簽的是原始 bytes：body 含中文時，重新 `json.dumps` 的結果不得影響簽章

**網址驗證**（全部回 400）
- `http://…`、`https://user:pass@host/`
- `https://127.0.0.1`、`https://[::1]`
- `https://10.0.0.1`、`https://192.168.1.1`、`https://172.16.0.1`
- `https://169.254.169.254`、`https://metadata.google.internal`
- 送出時解析到私有 IP → 不送，標 `failed`

**端點清單**
- `PUT` 兩次同一個 caller → 只有一列，`id` 不變，`url` 換了
- `DELETE` 之後 `active = false`，列還在，`id` 不變
- `DELETE` 之後再 `PUT` → `active` 回 `true`，`id` 仍然不變
- `GET` 回的 `secret` 與 `PUT` 回的逐字相同（推導的，不是存的）
- 兩個不同 caller 的 `secret` 不同
- 推送未設定時，四支 `/v1/webhook-endpoint` 全部回 503（不是回一個沒有 `secret` 的物件）

**排程判斷**
- `record()` 回 `None` 時**不**建 delivery
- `caller_id IS NULL` 時**不**建 delivery
- caller 沒註冊端點 / `active = false` 時**不**建 delivery
- 推送未設定時**不**建 delivery，且回呼仍回 `1|OK`

**排程時機**（這條是這個 repo 特有的，一定要有）
- `payment_return` 收到訂閱首期成功回呼時，`schedule()` 被呼叫的時間點
  **晚於** `subs_store.mark_active()`

**回呼不受推送影響**
- `schedule()` 內部拋例外時，`/ecpay/return` **仍然**回 `1|OK`（不是 500）
- 且該筆 delivery 是 `failed`、`attempts = 0`

**投遞**
- caller 回 200 → `delivered`，內部端點回 200
- caller 回 500 → `failed`，內部端點回 **502**（讓佇列重試）
- caller timeout → `failed`，內部端點回 502
- delivery 已是 `delivered` 時再派送一次 → 不重送給 caller，內部端點回 200
- `X-Delivery-Attempt` 取自 `deliveries.attempts`：sweep 重排之後那個數字**繼續累加**，
  不因為換了新 task 而重置

**內部端點**
- 沒帶 `X-Internal-Key` → 401
- 帶錯的 → 401
- `/internal/deliveries/sweep` 同上

**sweep**
- 有 active 端點、事件已落地、沒有 delivery → 補建一列
- 剛落地（5 分鐘內）的事件**不**補建（避免跟正常路徑賽跑）
- 48 小時前的事件**不**補建
- `failed` 且 `attempts = 0` → 重排，狀態回 `pending`
- `pending`／`failed` 且超過 2 × queue 窗口 → `dead`
- 讀不到 queue 設定時退回 24 小時常數，且 log WARNING
- **剛註冊的端點會被回填**：事件落地時 caller 還沒註冊，之後 `PUT` 了，
  下一輪 sweep 補推那些事件（48 小時內的）—— 這是刻意行為，要有測試釘住它
- `DELETE`（`active = false`）之後，sweep **不**補推 —— 停用就是停用

- 撞到 `_SWEEP_MAX_PER_RUN` 時 log WARNING 並說出還剩多少（不可靜默截斷）
- 一輪之內會迴圈掃到回不滿 `LIMIT` 為止，不是只掃 500 筆就收工

**per-caller queue**
- queue 名字由 `caller_id` 推導，且對同一個 `caller_id` **穩定**
- 消毒後可能相同的兩個 `caller_id`（例如 `a.b` 與 `a-b`）→ 名字**不同**
- queue 不存在（Cloud Tasks 回 NOT_FOUND）→ 退回共用 queue，且 log ERROR

**`grant-scope.sh`**
- 既有 scope 是 `orders:read, orders:write`，補 `webhooks:write` 之後
  是**三個**，不是一個 —— 這條測試存在的唯一理由是擋掉覆寫式實作
- 重複補同一個 scope 不會產生重複值

**ping**
- body 的 `id` 是 `0`、`event_type` 是 `ping`
- `events` 表**沒有多出任何一列**
- `deliveries` 多出一列且 `event_id IS NULL`
- 它走的是**真的**佇列（`tasks.create` 被呼叫），不是同步直送

**redeliver**
- 別人的 `event_id` → 404
- `caller_id IS NULL` 的事件 → 404
- 自己的 → 建**新的一列** delivery，舊的那列不動

**`GET /v1/events` 沒有變**
- 既有的 events API 測試全部原樣通過

---

## 非目標

寫出來是為了擋住之後「順便加一下」的壓力：

| 不做 | 為什麼 |
|---|---|
| 一個 caller 多個端點 | 沒有人要。schema 已經預留，要的時候拿掉一個索引就好 |
| 事件類型過濾 | caller 自己 `if` 一行就好；服務端的過濾表是第二份真相 |
| 批次推送 | 「五筆裡第三筆失敗」沒有便宜的答案 |
| 保證順序 | 要順序就要 head-of-line blocking，一個壞掉的 caller 會卡住自己所有事件。`id` 已經足夠讓 caller 自己排 |
| 推 `caller_id IS NULL` 的事件 | 那是別的系統的事件，推了是洩漏 |
| 自動停用一直失敗的端點 | 停用是營運決策。先給 `GET /v1/deliveries` 讓人**看得到**，不要替人決定 |
| 為退款補事件 | `app/refunds.py` 目前不落地事件。那是既有的缺口，不是這次的範圍 |
| 逐 caller 輪替簽章密鑰 | 換來的是資料庫裡一欄要保護的明文，不划算 |

---

## 驗收

- [ ] `GET /v1/events` 的行為與回應一個位元組都沒變
- [ ] 沒註冊端點的 caller 完全無感
- [ ] `POST /v1/webhook-endpoint/test` 能在**沒有任何真實金流**的情況下走完
      Cloud Tasks → 內部端點 → 簽章 → caller 的整條路
- [ ] 排程失敗時，`/ecpay/return` 仍然回 `1|OK`，且該筆事後被 sweep 補上
- [ ] `/health` 說得出推送有沒有設定、有沒有積壓的 `dead`
- [ ] 死信只由 sweep 標記，門檻來自 queue 本人，程式裡沒有第二個 max-attempts
- [ ] 與 `payment-paypal` 的 API、簽章格式、payload 形狀**逐欄相同**
- [ ] README 補上「怎麼接推送」，含：簽章向量、必須用原始 bytes 驗簽、
      `ping` 要在去重之前 return、`payment.info` 不代表收到錢、
      **剛註冊完會收到一批過去 48 小時的事件**

## 要一併修正的既有文字

- `docs/superpowers/specs/2026-08-24-payment-ecpay-design.md` 的
  「服務不主動推送事件」決策 —— 改寫成指向這份文件，並保留原本的理由與推翻的原因
- `README.md` 的 API 表格與「快速上手」
- `app/routers/events.py:16` 的 docstring（現在寫著「服務**不主動推送**」）

---

## 決策紀錄

**推送不取代拉取。** 兩條出口共用同一張 `events` 表、同一個 payload 形狀。
caller 寫一份 parser。

**綠界不會給第二次機會，所以安全網放服務端。**
`record()` 成功之後綠界的重送會被去重擋掉 —— 「落地了但沒排出去」無法自癒。
一支 sweep 端點加一個 Cloud Scheduler 解決，代價是多一個 GCP 元件，
換掉的是「每個 caller 都要自己開一個 Scheduler 對帳」。

**排程是回呼的最後一件事，不是拿到 event id 的那一刻。**
`app/db.py` 每次 query 就 commit，所以「commit 之後」這個條件保護不了
「後續的狀態更新還沒寫進去」這個 race。

**重試窗口用 `max-retry-duration` 表達，不用 `max-attempts`。**
牆鐘時間寫在設定裡，不需要任何人去累加等比級數 —— caller 反饋原文那個
「約 12 小時」實際是 21 分鐘，就是這樣算錯的。

**死信的門檻向 queue 本人問。** 不留 `WEBHOOK_MAX_ATTEMPTS`。
沒有第二份真相，就沒有漂移。

**密鑰推導不入庫，綁 `caller_id`。**
入站認證只需驗證所以存 hash；出站簽章必須持有所以推導。
綁 caller 而非 endpoint：同一 caller 的多個端點是同一個信任邊界。

**端點清單今天一對一，schema 從第一天就是多端點的形狀。**
唯一索引是「一個 caller 一個端點」的全部實作，拿掉它就放寬了。

**`DELETE` 是軟停用。** 投遞紀錄的外鍵不斷、「當初送去哪」答得出來、`id` 穩定。

**ping 走真佇列。** 同步直送會跳過最會壞的四樣東西，那支端點就沒有意義了。
`deliveries.event_id` 可為 NULL 就是為了它。

**內部端點用靜態 header，不用 OIDC。** 跟 API key 同一個安全模型，
而且省掉 `google-auth` —— 這個 repo 連 DB driver 都挑不用編譯的。
建 task 也直接打 REST，理由相同，`iam_token()` 已經在手上。

**`/health` 報告死信但不因此回 503。** 死信通常代表 caller 壞了，
用 503 表達會讓 ci 的 smoke 變成「所有 caller 今天都好嗎」。
