#!/usr/bin/env bash
cd "$(dirname "$0")/.."
for p in $(pgrep -f "uvicorn app.main:app"); do kill $p; done
for p in $(pgrep -f "python -m app.worker"); do kill $p; done
sleep 1
nohup uvicorn app.main:app --port 8000 > /tmp/api.log 2>&1 &
nohup python -m app.worker > /tmp/worker.log 2>&1 &
sleep 4
curl -s localhost:8000/api/health
