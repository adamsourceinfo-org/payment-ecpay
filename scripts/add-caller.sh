#!/usr/bin/env bash
# 新增一個 caller 的 API key。
#
# 刻意沒有 admin API、也沒有 bootstrap admin key —— 一把能製造其他 key 的
# 萬能鑰匙，它的長期風險大於它省下的麻煩。代價是這個動作要人跑，
# 所以把它寫成腳本，讓「手動」不等於「痛苦」。
#
# **沒有密碼。** 跟服務端同一個機制：Cloud SQL IAM 資料庫認證，
# 拿自己的 access token 當密碼。整套設計裡不存在 DB 密碼這種東西。
#
# 用法：
#   ./scripts/add-caller.sh dev my-service "orders:read,orders:write" "備註"
set -euo pipefail

ENVIRONMENT="${1:?用法: add-caller.sh <dev|prod> <caller_id> <scopes> [note]}"
CALLER_ID="${2:?缺少 caller_id}"
SCOPES="${3:?缺少 scopes（逗號分隔）}"
NOTE="${4:-}"

PROJECT="adamsourceinfo-${ENVIRONMENT}"
INSTANCE="apps-pg"
DB="payment_ecpay"
PORT="${PGPORT_LOCAL:-5433}"

DB_USER="$(gcloud config get-value account 2>/dev/null)"
if [[ -z "$DB_USER" ]]; then
  echo "gcloud 沒有登入帳號。先跑 gcloud auth login。" >&2
  exit 1
fi

# 一次性前置（每個環境做一次）：把自己加成這個 instance 的 IAM 使用者，
# 並讓自己能存取 app 建的表 —— 表的擁有者是 run-payment-ecpay，不是你。
#
#   gcloud sql users create "$DB_USER" --instance=apps-pg \
#     --project="$PROJECT" --type=cloud_iam_user
#   psql ... -c 'GRANT "run-payment-ecpay@'"$PROJECT"'.iam" TO "'"$DB_USER"'"'
#
# 還需要專案層級的 roles/cloudsql.instanceUser 與 roles/cloudsql.client。

# 產生 key 並算 hash。DB 裡只存 hash，明文只在這裡出現一次。
KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
KEY_HASH="$(printf '%s' "$KEY" | shasum -a 256 | cut -d' ' -f1)"

SCOPES_SQL="$(python3 -c "
import sys
items = [s.strip() for s in sys.argv[1].split(',') if s.strip()]
print('ARRAY[' + ','.join(\"'\" + i.replace(\"'\", \"''\") + \"'\" for i in items) + ']::text[]')
" "$SCOPES")"

TOKEN="$(gcloud auth print-access-token)"

cloud-sql-proxy "${PROJECT}:asia-east1:${INSTANCE}" --port "$PORT" \
  --token "$TOKEN" >/tmp/add-caller-proxy.log 2>&1 &
PROXY_PID=$!
trap 'kill $PROXY_PID 2>/dev/null || true' EXIT

for _ in $(seq 1 30); do nc -z 127.0.0.1 "$PORT" 2>/dev/null && break; sleep 1; done

# access token 當密碼 —— 跟服務端一模一樣的機制
PGPASSWORD="$TOKEN" psql -h 127.0.0.1 -p "$PORT" -U "$DB_USER" -d "$DB" \
  -v ON_ERROR_STOP=1 -q <<SQL
INSERT INTO api_keys (caller_id, key_hash, scopes, note)
VALUES ('${CALLER_ID}', '${KEY_HASH}', ${SCOPES_SQL}, $(
  if [[ -n "$NOTE" ]]; then echo "'${NOTE}'"; else echo "NULL"; fi));
SQL

cat <<MSG

  caller : ${CALLER_ID}
  環境   : ${ENVIRONMENT}
  scopes : ${SCOPES}
  建立者 : ${DB_USER}

  API key（只會顯示這一次，DB 裡只有 sha256）：

    ${KEY}

MSG
