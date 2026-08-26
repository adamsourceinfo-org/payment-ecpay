#!/usr/bin/env bash
# 給既有的 caller 補 scope。
#
# 為什麼需要這一支：add-caller.sh 只能**新增** key，不能改。而新增 scope
# （例如推送上線時的 webhooks:read / webhooks:write）要動的是既有的那把 ——
# 重發一把新的 key 等於逼每個 caller 改設定重新部署。
#
# ⚠️⚠️ **這支是「附加」，不是「覆寫」。**
# runbook 只會傳新增的那幾個 scope。如果實作寫成 `SET scopes = %s`，
# 那一行會把 caller 在 prod 的 orders:* 與 events:read **整組刪掉**，
# 當場打斷正在跑的金流。一支只在 runbook 裡出現一次的腳本，
# 錯了不會有人在 code review 抓到 —— 所以 SQL 寫死成 append + dedupe。
#
# 用法：
#   ./scripts/grant-scope.sh dev line-translate-bot "webhooks:read,webhooks:write"
set -euo pipefail

ENVIRONMENT="${1:?用法: grant-scope.sh <dev|prod> <caller_id> <scopes>}"
CALLER_ID="${2:?缺少 caller_id}"
SCOPES="${3:?缺少要新增的 scopes（逗號分隔）}"

PROJECT="adamsourceinfo-${ENVIRONMENT}"
INSTANCE="apps-pg"
DB="payment_ecpay"
PORT="${PGPORT_LOCAL:-5433}"

DB_USER="$(gcloud config get-value account 2>/dev/null)"
if [[ -z "$DB_USER" ]]; then
  echo "gcloud 沒有登入帳號。先跑 gcloud auth login。" >&2
  exit 1
fi

SCOPES_SQL="$(python3 -c "
import sys
items = [s.strip() for s in sys.argv[1].split(',') if s.strip()]
if not items:
    raise SystemExit('scopes 不能是空的')
print('ARRAY[' + ','.join(\"'\" + i.replace(\"'\", \"''\") + \"'\" for i in items) + ']::text[]')
" "$SCOPES")"

TOKEN="$(gcloud auth print-access-token)"

cloud-sql-proxy "${PROJECT}:asia-east1:${INSTANCE}" --port "$PORT" \
  --token "$TOKEN" >/tmp/grant-scope-proxy.log 2>&1 &
PROXY_PID=$!
trap 'kill $PROXY_PID 2>/dev/null || true' EXIT

for _ in $(seq 1 30); do nc -z 127.0.0.1 "$PORT" 2>/dev/null && break; sleep 1; done

# ⚠️ scopes || 新的，再 DISTINCT —— **附加**，不是覆寫。
# RETURNING 讓下面印得出「改完長什麼樣」，人可以肉眼確認沒有東西不見。
PGPASSWORD="$TOKEN" psql -h 127.0.0.1 -p "$PORT" -U "$DB_USER" -d "$DB" \
  -v ON_ERROR_STOP=1 <<SQL
\\set QUIET on
WITH before AS (
  SELECT id, scopes FROM api_keys WHERE caller_id = '${CALLER_ID}' AND active
), updated AS (
  UPDATE api_keys k
     SET scopes = ARRAY(SELECT DISTINCT unnest(k.scopes || ${SCOPES_SQL}) ORDER BY 1)
    FROM before b
   WHERE k.id = b.id
  RETURNING k.id, b.scopes AS was, k.scopes AS now
)
SELECT id,
       array_to_string(was, ', ') AS "原本",
       array_to_string(now, ', ') AS "現在",
       cardinality(now) - cardinality(was) AS "新增幾個"
  FROM updated;
SQL

echo
echo "  caller : ${CALLER_ID}"
echo "  環境   : ${ENVIRONMENT}"
echo "  操作者 : ${DB_USER}"
echo
echo "  ⚠️ 上表沒有列出任何一列 = 這個 caller 沒有 active 的 key，什麼都沒改。"
echo
