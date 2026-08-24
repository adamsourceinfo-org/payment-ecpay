-- 綠界的 MerchantTradeNo **送出過就不能再用**（回 10300028「訂單編號重覆」）。
-- 使用者在付款頁跳開再回來是最常見的行為，所以每次進導轉頁都要換一個新單號。
--
-- 但舊單號的回呼還是可能進來（使用者開了兩個分頁、或綠界延遲重送），
-- 所以每一次嘗試都要留下來，回呼一律透過這張表找回本地紀錄。
CREATE TABLE IF NOT EXISTS trade_attempts (
  merchant_trade_no text PRIMARY KEY,
  subject_kind      text NOT NULL,          -- 'order' | 'subscription'
  subject_id        uuid NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS trade_attempts_subject
  ON trade_attempts (subject_kind, subject_id, created_at DESC);

-- 既有資料補進來（第一次部署時是空的，但重跑要安全）
INSERT INTO trade_attempts (merchant_trade_no, subject_kind, subject_id)
SELECT merchant_trade_no, 'order', id FROM orders
ON CONFLICT (merchant_trade_no) DO NOTHING;
INSERT INTO trade_attempts (merchant_trade_no, subject_kind, subject_id)
SELECT merchant_trade_no, 'subscription', id FROM subscriptions
ON CONFLICT (merchant_trade_no) DO NOTHING;
