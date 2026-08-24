-- 付款完成時 checkout_token 會被清掉（避免重開付款頁在綠界建立另一筆交易）。
-- 但這樣一來，使用者拿著舊連結回來只會看到 404，以為系統壞了。
-- 把用掉的 token 留在另一個欄位，就分得出「已付款」與「根本沒這個連結」。
ALTER TABLE orders ADD COLUMN IF NOT EXISTS used_checkout_token text;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS used_checkout_token text;
CREATE INDEX IF NOT EXISTS orders_used_token ON orders (used_checkout_token);
CREATE INDEX IF NOT EXISTS subscriptions_used_token ON subscriptions (used_checkout_token);
