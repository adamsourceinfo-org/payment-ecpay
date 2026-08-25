-- 綠界端的定期定額執行狀態（0 已終止 / 1 執行中 / 2 執行完成）。
-- 這是「下個月還會不會扣款」的權威答案 —— 本地的 status 只是我們自己的紀錄，
-- 對帳時要能把綠界怎麼說也給 caller 看。
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS ecpay_exec_status text;
