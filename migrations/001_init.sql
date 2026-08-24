-- 每一張業務表都有 caller_id。隔離是查詢層的預設，不是靠呼叫端記得帶參數。
CREATE TABLE IF NOT EXISTS api_keys (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  caller_id    text NOT NULL,
  key_hash     text NOT NULL UNIQUE,          -- sha256，不存明文
  scopes       text[] NOT NULL,
  active       boolean NOT NULL DEFAULT true,
  note         text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz
);
CREATE INDEX IF NOT EXISTS api_keys_hash_active ON api_keys (key_hash) WHERE active;

-- 金額是整數：綠界只收 TWD 且 TotalAmount 不可有小數。
-- 用 numeric 會讓「這裡到底能不能有小數」永遠是個懸念。
CREATE TABLE IF NOT EXISTS orders (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  caller_id         text NOT NULL,
  reference_id      text NOT NULL,             -- caller 提供的冪等鍵
  merchant_trade_no text NOT NULL UNIQUE,      -- 送去綠界的單號，高熵亂碼
  ecpay_trade_no    text,                      -- 綠界回的交易編號
  amount            integer NOT NULL CHECK (amount > 0),
  currency          text NOT NULL DEFAULT 'TWD',
  choose_payment    text NOT NULL,
  status            text NOT NULL,
  -- 付款導轉頁的網址 token。那一頁不驗 API key，所以不能用 id 當網址。
  checkout_token    text UNIQUE,
  return_url        text,                      -- caller 的導回位置
  -- 建單當下簽好的表單原樣存起來。導轉頁重新產生的話 MerchantTradeDate 會變，
  -- 就會簽出另一份 —— 回給 caller 的 form 與實際 POST 出去的就不一致了。
  checkout_fields   jsonb,
  payment_type      text,                      -- 綠界回報的實際付款方式
  paid_at           timestamptz,
  -- 已關帳與否決定退款要送 R（退刷）還是 N（放棄授權）。
  -- 綠界每日 20:15~20:30 自動關帳，所以當天付款的單通常還沒關帳。
  closed            boolean NOT NULL DEFAULT false,
  refunded_amount   integer NOT NULL DEFAULT 0,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (caller_id, reference_id)             -- 網路重試不會變成兩筆收款
);
CREATE INDEX IF NOT EXISTS orders_caller ON orders (caller_id, created_at DESC);

-- ATM／超商的取號結果。信用卡不會有這一筆。
CREATE TABLE IF NOT EXISTS order_payment_info (
  order_id    uuid PRIMARY KEY REFERENCES orders(id) ON DELETE CASCADE,
  bank_code   text,
  v_account   text,
  payment_no  text,
  expire_date text,
  raw         jsonb NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- 綠界沒有 plan 物件，週期參數直接存在訂閱上。
CREATE TABLE IF NOT EXISTS subscriptions (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  caller_id            text NOT NULL,
  reference_id         text NOT NULL,
  merchant_trade_no    text NOT NULL UNIQUE,
  ecpay_trade_no       text,
  period_amount        integer NOT NULL CHECK (period_amount > 0),
  period_type          text NOT NULL,
  frequency            integer NOT NULL,
  exec_times           integer NOT NULL,
  status               text NOT NULL,
  checkout_token       text UNIQUE,
  return_url           text,
  checkout_fields      jsonb,
  total_success_times  integer NOT NULL DEFAULT 0,
  total_success_amount integer NOT NULL DEFAULT 0,
  first_charged_at     timestamptz,
  cancelled_at         timestamptz,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  UNIQUE (caller_id, reference_id)
);
CREATE INDEX IF NOT EXISTS subscriptions_caller
  ON subscriptions (caller_id, created_at DESC);

-- 每期扣款。gwsr 是綠界每次授權的交易號，天然的去重鍵。
CREATE TABLE IF NOT EXISTS subscription_charges (
  id              bigserial PRIMARY KEY,
  subscription_id uuid NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
  gwsr            text NOT NULL UNIQUE,
  amount          integer NOT NULL,
  rtn_code        text NOT NULL,
  auth_code       text,
  process_date    text,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS subscription_charges_sub
  ON subscription_charges (subscription_id, id);

CREATE TABLE IF NOT EXISTS events (
  id           bigserial PRIMARY KEY,     -- 就是對外的游標
  -- 綠界沒有全域 event id，去重鍵是自己造的（見 routers/callbacks.py）。
  dedupe_key   text NOT NULL UNIQUE,
  event_type   text NOT NULL,
  -- NULL = 對應不到 caller。照樣落地保留原文，但對每個 caller 都不可見：
  -- WHERE caller_id = :me 不匹配 NULL。
  caller_id    text,
  -- **落地當下就要判定好**。綠界的原文分辨不出訂閱首期與一次性付款，
  -- 只存原文的話事後永遠救不回來。
  subject_kind text,
  subject_id   text,
  payload      jsonb NOT NULL,
  received_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS events_caller_cursor ON events (caller_id, id);
