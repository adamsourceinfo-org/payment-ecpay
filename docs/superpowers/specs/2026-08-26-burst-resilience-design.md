# payment-ecpay 突發韌性設計

2026-08-26

## 前提

多個 caller 呼叫這個服務收款，而且**通常是行銷活動** —— 大量使用者集中在某個時刻付款。
服務必須在那個時刻仍然做到三件事：

| | | 突發時會壞在哪 |
|---|---|---|
| **1. 快** | 錢確定 → caller 知道 | 回呼處理慢 → 通知晚 |
| **2. 公平** | 一個 caller 慢，不能拖到別人 | 見〈事件推送設計〉的 per-caller queue |
| **3. 查得到** | 「這筆怎麼了」隨時問得到 | `GET /v1/orders/{id}` 跟回呼搶同一批連線 |

**責任邊界**：服務只負責通知 caller。caller 拿到通知之後要推 LINE、開 SSE、
還是讓前端輪詢，是 caller 的設計空間，不在這份文件裡。

這份處理的是**既有程式碼**在突發下的韌性；事件推送本身見
`2026-08-25-webhook-delivery-design.md`。**這份要先做** ——
其中的 `db.transaction()` 是那份的先決條件。

---

## 1. ⚠️ 四支綠界回呼阻塞事件迴圈

```
app/routers/callbacks.py:169  async def payment_return
app/routers/callbacks.py:237  async def period_return
app/routers/callbacks.py:280  async def payment_info
app/routers/callbacks.py:314  async def order_result
```

**全服務只有這四支是 `async def`，其餘全是 `def`。**
其餘的由 FastAPI 丟進 threadpool，沒事；這四支跑在事件迴圈上，
而每一支都直接呼叫同步的 `db.query()`（pg8000 是同步 driver）。

阻塞呼叫在 `async def` 裡會卡住**整個事件迴圈** —— 那個實例上所有請求跟著排隊，
包括 caller 正在同步查詢的 `GET /v1/orders/{id}`。一筆回呼六次 DB round trip，
就是幾十毫秒的全實例停擺。而推送設計還要在同一個 handler 裡建 Cloud Task
（一次對外 HTTP）。

### 決定：handler 只做 `await request.body()`，其餘丟 threadpool

```python
@router.post("/return")
async def payment_return(request: Request):
    raw = await request.body()
    return await run_in_threadpool(_payment_return, raw)


def _payment_return(raw: bytes):        # 同步，可以自由用 db
    ...
```

`_form()` 拆成 `_parse(raw)`（純函式，同步）與呼叫端的 `await request.body()`。

**併發模型（寫下來，之後不要再猶豫）**：
`app/` 底下**除了四支回呼的最外層之外，一律是同步程式碼**。
需要在請求裡跑同步 I/O 就用 `run_in_threadpool`。
不引入 async DB driver、不混用 async httpx —— 混用是這一類 bug 的來源。

---

## 2. `DB_POOL_MAX` 沒有在限制併發

`app/db.py:78`：

```python
try:
    conn = pool.get_nowait()
except Empty:
    conn = _new_conn()        # ← 池空了就直接開新的
```

`LifoQueue(maxsize=3)` 限制的是**歸還**數量（`put_nowait` 失敗就 `close()`），
不是同時開幾條。突發下連線數無上限。

而 `apps-pg` 是一個環境一台、服務只靠 database 分隔（`.cicd/config.yml`）——
連線耗盡會把**同一台上的其他服務**一起拖下水。這是跨服務的爆炸半徑。

### 決定：改成有界等待 + 明確的耗盡錯誤

池預先不建連線（維持懶惰建立），但**同時在外的連線數**用一個計數器封頂：

- 借得到就借
- 借不到且未達上限 → 建新的
- 借不到且已達上限 → 等 `DB_POOL_TIMEOUT_SECONDS`（預設 5 秒）
- 等逾時 → 拋 `PoolExhausted`，回 503

⚠️ **逾時要回 503 而不是無限等**。無限等會讓 threadpool 的 worker 全部卡住，
症狀從「慢」變成「整個實例沒反應」，而且健康檢查也跟著死 —— 那時候連
「哪裡壞了」都答不出來。回 503 讓綠界重送（它本來就會），讓 caller 重試。

**容量規則**：`實例數 × DB_POOL_MAX ≤ 本服務的 Cloud SQL 連線預算`。
所以 `max-instances` 與 `DB_POOL_MAX` 要一起看，任一邊單獨調都是錯的。

---

## 3. ⚠️ 回呼要交易化（同時修掉一個既有的資料正確性 bug）

`app/routers/callbacks.py:191`：

```python
new_id = events_store.record(...)   # ← 這一行回來時事件已經 commit 了
if new_id is None:
    return PlainTextResponse(ACK)   # 綠界重送走這裡
if sub:
    subs_store.mark_active(...)     # ← 另一個交易
```

`db.query()` 每次呼叫就是一個交易（`get_conn()` 在 context 結束時 commit），
所以 `record()` 一回傳，事件就已經落地。如果 process 在兩個 commit 之間死掉
（實例回收、OOM、逾時），綠界沒收到 `1|OK` 會重送 —— 但重送會被 `dedupe_key`
擋掉，走 `new_id is None` 早退，**`mark_active()` 永遠不會執行**。

去重鍵正在做它該做的事，同時堵死了唯一的復原路徑。突發正是它發作的時候。

嚴重性要講公道：這是**衍生狀態過時，不是資料遺失**。`events` 那一列保留了綠界原文，
`?refresh=true` 也拿得到綠界端的權威狀態。但服務自己的答案會永遠是錯的。

### 決定：`db.transaction()`，一個交易涵蓋「落地事件 + 更新本地狀態」

```python
with db.transaction() as tx:
    new_id = events_store.record(..., tx=tx)
    if new_id is None:
        return ACK                      # 這次真的什麼都沒做
    subs_store.mark_active(..., tx=tx)
    ...
# 出了 with 才 commit
```

store 層的每個函式多一個 `tx=None` 參數：給了就用那條連線、不自己 commit；
沒給就維持現狀（自己借一條、自己 commit）。既有呼叫端一個字都不用改。

⚠️ **`db.query()` 不可以在 `transaction()` 裡面被呼叫** —— 它會另外借一條連線，
那筆寫入就落在交易外面，等於白做。store 函式一律改成走同一個內部 helper。

排程推送放在 `with` 區塊**之外**（commit 之後）—— 這一條讓推送設計裡
「commit 之後才排程」重新是有意義的規則。

---

## 4. 冷啟動：`pg_advisory_lock` 讓擴容變成序列的

`app/main.py` 的 lifespan 每個實例啟動都跑 `db.run_migrations()`，
而 `app/db.py:139` 用的是**阻塞式**的 `pg_advisory_lock`。

行銷活動開始時服務是 0 個實例，第一波同時開 20 個 —— 20 個實例搶同一把鎖，
一個跑、其餘全部**排隊等**，而且每個都已經先付了一次 IAM token + TLS 握手。
Cloud Run 的 startup probe 是 TCP，lifespan 沒跑完就不會開始服務。

### 決定：`pg_try_advisory_lock`，拿不到就跳過

拿不到代表**別的實例正在跑 migration**，那正是我們要的結果 —— 沒有理由等它。
跳過時 log INFO 說明原因（不是 WARNING，這是正常的擴容行為）。

⚠️ 代價要寫清楚：跳過的實例**可能在 schema 還沒套用完的情況下開始服務**。
這在這個 repo 是可接受的，因為 migration 一律是 `IF NOT EXISTS` / 加欄位的相容變更，
而且部署是 rolling 的（舊 revision 本來就在跑舊 schema）。
**如果哪天要做破壞性 migration，這條假設就不成立** —— 那種 migration 要走
CI 的獨立 job，不是 runtime。

---

## 5. 讓 `/ecpay/order-result` 成為第二個入口

`app/ecpay/orders.py:56` 的註解寫著「我們也有機會**先更新狀態**」，
但 `app/routers/callbacks.py:314` 明說「**不在這裡改訂單狀態**」。兩邊是矛盾的。

原本不改的理由是「瀏覽器導回是使用者**可以偽造**、也可能根本不發生的」。
前半段**在驗簽通過時不成立** —— `order_result` 已經在算 `_verified(params)`，
而綠界簽過的導回參數跟 `ReturnURL` 的一樣可信（同一把 HashKey/HashIV）。
後半段仍然成立，但那只說明「不能只靠它」，不是「不能用它」。

綠界的 `OrderResultURL` 與 `ReturnURL` 收到的參數集相同，
訂單（`app/ecpay/orders.py:57`）與訂閱（`app/routers/subscriptions.py:90`）都有帶。

### 決定：驗簽通過就走跟 `/ecpay/return` 完全相同的冪等處理

同一個 `dedupe_key`（`return:{trade_no}:{rtn_code}`），誰先到誰生效，
另一個拿到 `None`。這把「錢確定」到「我們的狀態正確」的窗口從
「回呼佇列長度」縮到**零**，而且負載天然分散 —— 使用者是陸續回來的。

理由不是「使用者的網頁體驗」（那是 caller 的事），是**它讓我們自己的狀態更早正確，
所以更早通知得出去**。

⚠️ **推送的責任歸屬要指定，否則有一個正好落在活動場景的洞。**
如果 `order-result` 贏了競態，`/ecpay/return` 那邊 `record()` 回 `None` → 早退
→ **沒有人排推送**，靜默退化成 sweep 的一小時延遲。

> **規則：`/ecpay/return` 的 `None` 路徑，從「什麼都不做」改成「確保 delivery 列存在」。**

幕後回呼一定會到（綠界保證送 `ReturnURL`），所以排推送這件事留在幕後那條路，
不放進使用者的導回路徑上 —— 導回路徑要快。

`order-result` 本身**不排推送**，只更新狀態。

⚠️ 這條依賴第 3 項。沒有交易化的話，`order-result` 只是多開一個會撞上同一個
「commit 之後掛掉」窗口的入口，等於放大 bug。**順序不能反。**

---

## 6. 一次性設定

- `.cicd/env.common`：新增 `DB_POOL_TIMEOUT_SECONDS=5`
- `max-instances` 與 `min-instances` 目前不在 `.cicd/config.yml` 裡 ——
  活動前依預估峰值設定，並同時檢查 `實例數 × DB_POOL_MAX` 是否還在
  Cloud SQL 的連線預算內

---

## 測試要求

**併發模型**
- 四支回呼的 route function 是 `async def`，但實際處理跑在 threadpool
  （斷言處理函式收到的是 bytes，且不在事件迴圈執行緒上）

**連線池**
- 同時借超過 `DB_POOL_MAX` 條 → 第 N+1 條等待
- 等待超過 `DB_POOL_TIMEOUT_SECONDS` → 拋 `PoolExhausted`
- 歸還之後等待中的那條借得到
- 連線壞掉被丟棄時，在外計數要正確遞減（否則池會慢慢「漏」到永久耗盡）

**交易**
- `transaction()` 區塊內拋例外 → 事件與狀態更新**都**沒有落地
- 區塊正常結束 → 兩者都在
- store 函式不給 `tx` 時行為不變（既有測試全過）

**migration 鎖**
- 拿不到 `pg_try_advisory_lock` → 直接回，不阻塞，log INFO
- 拿得到 → 照常套用

**order-result 雙入口**
- 驗簽通過 + `RtnCode=1` + 本地 `pending` → 狀態變 `paid`
- 驗簽**不**通過 → 狀態不動（只導回）
- 沒有 `CheckMacValue` → 狀態不動
- `order-result` 先到、`/ecpay/return` 後到 → 狀態只變一次，
  且 `/ecpay/return` 的 `None` 路徑**仍然確保 delivery 列存在**
- `/ecpay/return` 先到、`order-result` 後到 → 狀態只變一次，
  `order-result` 不重複寫
- `order-result` **不**排推送

---

## 非目標

| 不做 | 為什麼 |
|---|---|
| 換成 async DB driver | 混用 async/sync 正是第 1 項那個 bug 的來源。同步 + threadpool 是一致的模型 |
| 把 migration 移出 runtime | 是對的方向，但那是 ci repo 的改動，跨 repo。先用 `try_advisory_lock` 把急性症狀解掉 |
| 快取 `GET /v1/orders/{id}` | 金流狀態的快取會製造「查到舊狀態」的爭議。先把連線與阻塞修好 |
| 讀寫分離 / read replica | 目前的瓶頸是連線數與事件迴圈，不是 DB 本身的吞吐 |
| 在 `order-result` 主動去綠界對帳 | 突發時等於對綠界放大流量。導回路徑要快，對帳是 `?refresh=true` 的事 |

---

## 決策紀錄

**同步 + threadpool，不引入 async driver。** 四支回呼是全服務唯一的
`async def`，也是唯一直接呼叫同步 DB 的地方 —— 那個組合會卡住整個實例。
統一成一種模型，之後不必每次判斷「這裡能不能 await」。

**連線耗盡回 503，不無限等。** 無限等會讓症狀從「慢」變成「沒反應」，
而且健康檢查跟著死。綠界本來就會重送，caller 本來就會重試。

**`transaction()` 是 store 層的選配參數，不是全面改寫。**
給了 `tx` 就共用連線，沒給就維持現狀 —— 既有呼叫端零改動。

**migration 拿不到鎖就跳過。** 拿不到代表別人正在跑，等它沒有意義。
代價是實例可能在 schema 套用完之前開始服務，而這依賴「migration 都是相容變更」
這個前提 —— 前提破了就要改成 CI job。

**`order-result` 更新狀態但不排推送。** 驗簽通過的導回參數跟 `ReturnURL`
一樣可信，所以拿它更早把狀態弄對；但推送留給一定會到的幕後回呼，
導回路徑要快。`/ecpay/return` 的 `None` 路徑因此要改成「確保 delivery 列存在」。
