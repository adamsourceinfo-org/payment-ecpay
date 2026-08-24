-- 信用卡授權的識別資料。綠界的付款結果通知會帶，但先前只有訂閱在存 ——
-- 一次性訂單同樣需要：查授權明細要 gwsr，人工對帳與客服查詢要授權碼。
ALTER TABLE orders ADD COLUMN IF NOT EXISTS gwsr text;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS auth_code text;
