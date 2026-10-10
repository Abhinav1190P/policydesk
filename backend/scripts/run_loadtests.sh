#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
USERS=${USERS:-60}
DURATION=${DURATION:-60s}
OUT=${OUT:-bench/load}
mkdir -p "$OUT"
export RATE_LIMIT_PER_MINUTE=1000000 RATE_LIMIT_BURST=100000 TENANT_RATE_LIMIT_PER_MINUTE=10000000 LOGIN_ATTEMPTS_PER_MINUTE=100000 DAILY_TOKEN_BUDGET=1000000000
run() {
  local name=$1 instances=$2 mode=$3 users=$4
  bash scripts/local_cluster.sh "$instances" 1 > /dev/null
  redis-cli -n 0 --scan --pattern 'ans:*' | xargs -r redis-cli -n 0 del > /dev/null
  if [ "$mode" = "hit" ]; then
    LOAD_MODE=hit locust -f loadtest/locustfile.py --headless -u 4 -r 4 -t 10s --host http://localhost:8080 --only-summary > /dev/null 2>&1 || true
  fi
  LOAD_MODE=$mode locust -f loadtest/locustfile.py --headless -u "$users" -r 20 -t "$DURATION" --host http://localhost:8080 --csv "$OUT/$name" --only-summary 2>&1 | grep "requests per instance" > "$OUT/$name.instances" || true
  echo "$name done"
}
run miss_1x 1 miss "$USERS"
run miss_3x 3 miss "$USERS"
run hit_1x 1 hit "$USERS"
run hit_3x 3 hit "$USERS"
export STUB_LATENCY_MS=400
run slowllm_1x 1 miss 150
run slowllm_3x 3 miss 150
unset STUB_LATENCY_MS
python scripts/summarize_load.py "$OUT"
