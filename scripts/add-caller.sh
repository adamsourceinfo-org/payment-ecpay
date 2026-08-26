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

# ⚠️ 一個 caller 一個 Cloud Tasks queue —— 這是推送的公平性隔離。
# max-concurrent-dispatches 是**每個 queue** 的設定，共用一個 queue 的話，
# 一個 caller 的端點 timeout 10 秒就能佔滿全部派送槽位，排隊擋住其他所有 caller。
# 行銷活動當天，那等於「A 公司的活動把 B 公司的通知全排隊了」。
#
# queue 名字**向服務要**，不要在這裡自己抄一份消毒規則 ——
# 抄一份的話，某天改了規則就會建出一個服務永遠找不到的 queue，
# 而症狀是「靜靜地退回共用 queue」，沒有人會發現。
QUEUE_PREFIX="$(grep -E '^TASKS_QUEUE_PREFIX=' "$(dirname "$0")/../.cicd/env.common" | cut -d= -f2)"
QUEUE_LOCATION="$(grep -E '^TASKS_LOCATION=' "$(dirname "$0")/../.cicd/env.common" | cut -d= -f2)"
QUEUE="$(cd "$(dirname "$0")/.." && python3 -c "
import sys
from app.webhooks.tasks import build_queue_name
print(build_queue_name(sys.argv[1], sys.argv[2]))
" "$QUEUE_PREFIX" "$CALLER_ID")"

# 主要旋鈕是 max-retry-duration，不是 max-attempts —— 後者只是失控保險。
# 12 小時內實際會派送約 23 次，永遠碰不到 30。
# ⚠️ 時長只收**秒**：43200s 不能寫成 12h，API 會回
#    「Illegal duration format; duration must end with 's'」。
if gcloud tasks queues describe "$QUEUE" --location="$QUEUE_LOCATION" \
     --project="$PROJECT" >/dev/null 2>&1; then
  echo "  queue ${QUEUE} 已存在，略過"
else
  gcloud tasks queues create "$QUEUE" --location="$QUEUE_LOCATION" \
    --project="$PROJECT" \
    --max-retry-duration=43200s --max-attempts=30 \
    --min-backoff=10s --max-backoff=3600s --max-doublings=5 \
    --max-concurrent-dispatches=10
fi

cat <<MSG

  caller : ${CALLER_ID}
  queue  : ${QUEUE}
  環境   : ${ENVIRONMENT}
  scopes : ${SCOPES}
  建立者 : ${DB_USER}

  API key（只會顯示這一次，DB 裡只有 sha256）：

    ${KEY}

MSG
