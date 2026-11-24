#!/usr/bin/env bash
# 端到端试跑：转学日期更正 + 重启后查询旧批清单。
# 用法：./scripts/smoke.sh [端口]
set -euo pipefail

PORT="${1:-8010}"
BASE="http://127.0.0.1:${PORT}/api"
DATA_DIR="$(mktemp -d)"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

cleanup() {
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$DATA_DIR"
}
trap cleanup EXIT

start_server() {
  DATABASE_URL="sqlite:///${DATA_DIR}/status.db" \
    "$ROOT/.venv/bin/uvicorn" app.main:app --host 127.0.0.1 --port "$PORT" \
    >"${DATA_DIR}/server.log" 2>&1 &
  SERVER_PID=$!
  for _ in $(seq 1 50); do
    if curl -fsS "${BASE}/health" >/dev/null 2>&1; then return 0; fi
    sleep 0.2
  done
  echo "服务启动失败" >&2
  cat "${DATA_DIR}/server.log" >&2
  exit 1
}

stop_server() {
  kill "$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
  SERVER_PID=""
}

json() { jq -c "$@"; }

echo "== 启动服务（端口 ${PORT}）"
start_server

echo
echo "== 批 B1：登记两张转学单并整批生效"
curl -fsS -X POST "$BASE/batches" -H 'content-type: application/json' \
  -d '{"batch_no":"B1"}' >/dev/null
for pair in "Z1 S1 2026-09-01" "Z2 S2 2026-09-01"; do
  read -r slip student day <<<"$pair"
  curl -fsS -X POST "$BASE/batches/B1/slips" -H 'content-type: application/json' \
    -d "{\"slip_no\":\"${slip}\",\"student_no\":\"${student}\",\"school_id\":\"SCHOOL-A\",\"effective_date\":\"${day}\"}" >/dev/null
done
RECEIPT=$(
  curl -fsS -X POST "$BASE/batches/B1/commit"
)
echo "$RECEIPT" | json '{batch_no, slip_count, basis_record_count, basis_prior_record_count, basis_batch_record_count}'

echo
echo "== B1 在 2026-09-30 的 SCHOOL-A 补报名单"
curl -fsS "$BASE/batches/B1/corrections?school_id=SCHOOL-A&statistics_date=2026-09-30" \
  | json '{addition_count, withdrawal_count, basis_records_read}'

echo
echo "== 批 B2：把 S1 的 Z1 生效日由 09-01 更正为 09-15（单号与学生不变）"
curl -fsS -X POST "$BASE/batches" -H 'content-type: application/json' \
  -d '{"batch_no":"B2"}' >/dev/null
curl -fsS -X POST "$BASE/batches/B2/slips" -H 'content-type: application/json' \
  -d '{"slip_no":"Z1","student_no":"S1","school_id":"SCHOOL-A","effective_date":"2026-09-15"}' \
  | json '{action, slip: .slip.effective_date}'
curl -fsS -X POST "$BASE/batches/B2/commit" | json '{batch_no, basis_record_count}'

echo
echo "== B2 在 2026-09-10（更正后尚未生效）的 SCHOOL-A 名单"
curl -fsS "$BASE/batches/B2/corrections?school_id=SCHOOL-A&statistics_date=2026-09-10" \
  | json '{withdrawals: [.withdrawals[].student_no], basis_records_read}'
echo "== B2 在 2026-09-20（更正后已生效，归属仍是 A）的 SCHOOL-A 名单"
curl -fsS "$BASE/batches/B2/corrections?school_id=SCHOOL-A&statistics_date=2026-09-20" \
  | json '{addition_count, withdrawal_count, basis_records_read, unmatched_student_count}'

echo
echo "== 保存旧批清单快照，重启服务"
B1_BEFORE=$(curl -fsS "$BASE/batches/B1/corrections?school_id=SCHOOL-A&statistics_date=2026-09-30")
stop_server
start_server
B1_AFTER=$(curl -fsS "$BASE/batches/B1/corrections?school_id=SCHOOL-A&statistics_date=2026-09-30")

if [[ "$B1_BEFORE" == "$B1_AFTER" ]]; then
  echo "重启后 B1 清单内容完全一致"
else
  echo "重启后 B1 清单发生变化！" >&2
  diff <(echo "$B1_BEFORE" | jq -S .) <(echo "$B1_AFTER" | jq -S .) >&2 || true
  exit 1
fi

echo
echo "== 未提交批次：提交失败不产生清单"
curl -fsS -X POST "$BASE/batches" -H 'content-type: application/json' \
  -d '{"batch_no":"B3"}' >/dev/null
curl -fsS -X POST "$BASE/batches/B3/slips" -H 'content-type: application/json' \
  -d '{"slip_no":"Z3","student_no":"S1","school_id":"SCHOOL-B","effective_date":"2026-09-15"}' >/dev/null
CODE=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BASE/batches/B3/commit")
echo "提交返回码：${CODE}（预期 409）"
CODE=$(curl -sS -o /dev/null -w '%{http_code}' \
  "$BASE/batches/B3/corrections?school_id=SCHOOL-B&statistics_date=2026-09-30")
echo "查询草稿批次返回码：${CODE}（预期 409）"

echo
echo "全部试跑通过。"
