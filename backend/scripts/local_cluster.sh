#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
N=${1:-3}
WORKERS=${2:-1}
LB_PORT=${LB_PORT:-8080}
RUN_DIR=${RUN_DIR:-/tmp/policydesk-cluster}
mkdir -p "$RUN_DIR"
for f in "$RUN_DIR"/*.pid; do [ -e "$f" ] && kill "$(cat "$f")" 2>/dev/null || true; rm -f "$f"; done
sleep 1
UPSTREAMS=""
for i in $(seq 1 "$N"); do
  PORT=$((8100 + i))
  INSTANCE_ID="api-$i" nohup uvicorn app.main:app --port "$PORT" --log-level warning > "$RUN_DIR/api-$i.log" 2>&1 &
  echo $! > "$RUN_DIR/api-$i.pid"
  UPSTREAMS="$UPSTREAMS    server 127.0.0.1:$PORT;\n"
done
for i in $(seq 1 "$WORKERS"); do
  INSTANCE_ID="worker-$i" nohup python -m app.worker > "$RUN_DIR/worker-$i.log" 2>&1 &
  echo $! > "$RUN_DIR/worker-$i.pid"
done
printf "worker_processes 1;\npid $RUN_DIR/nginx.pid;\nerror_log $RUN_DIR/nginx-error.log;\nevents { worker_connections 4096; }\nhttp {\n  access_log off;\n  upstream api {\n    least_conn;\n$UPSTREAMS    keepalive 64;\n  }\n  server {\n    listen $LB_PORT;\n    location / {\n      proxy_pass http://api;\n      proxy_http_version 1.1;\n      proxy_set_header Connection \"\";\n      proxy_set_header X-Forwarded-For \$remote_addr;\n      proxy_buffering off;\n    }\n  }\n}\n" > "$RUN_DIR/nginx.conf"
nginx -s stop -c "$RUN_DIR/nginx.conf" 2>/dev/null || true
sleep 0.5
nginx -c "$RUN_DIR/nginx.conf"
sleep 4
for i in $(seq 1 "$N"); do curl -sf "localhost:$((8100 + i))/api/health" > /dev/null && echo "api-$i up"; done
curl -s "localhost:$LB_PORT/api/health"; echo
