-- 事件主動推送。設計見 docs/superpowers/specs/2026-08-25-webhook-delivery-design.md
--
-- events 表與 GET /v1/events 一個位元組都沒動 —— 推送是第二條出口，不是取代。

-- 今天：一個 caller 一個端點。
-- 但 PK 用 uuid、唯一性靠底下那個索引 —— 日後放寬只要拿掉索引，
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
-- 刻意沒有 secret 欄位：簽章密鑰由 WEBHOOK_SIGNING_KEY 推導。
-- API key 是**入站**認證，只需要驗證所以存 sha256 就夠；簽章密鑰是**出站**的，
-- 服務必須持有它才簽得出來 —— 存 hash 沒有意義，存明文就是資料庫裡多一欄機密。

CREATE TABLE IF NOT EXISTS deliveries (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  -- NULL = 這是 POST /v1/webhook-endpoint/test 送的 ping，沒有對應事件。
  -- ping 因此走跟真事件**完全相同**的佇列與端點，而不是同步直送 ——
  -- 同步直送會跳過 Cloud Tasks、內部端點、X-Internal-Key、重試，
  -- 而那四樣正好是最會壞的部分。
  event_id        bigint REFERENCES events(id),
  endpoint_id     uuid NOT NULL REFERENCES webhook_endpoints(id),
  caller_id       text NOT NULL,
  -- 排程當下的網址。caller 之後改了網址，「這筆當初送去哪」還答得出來。
  url             text NOT NULL,
  -- pending   已建列、task 已排、還沒有任何投遞結果
  -- delivered caller 回了 2xx
  -- failed    至少一次失敗，佇列還在重試
  --           （attempts = 0 代表 task 根本沒建成，sweep 會重排）
  -- dead      放棄。**只由 sweep 標記**，投遞當下不標
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
