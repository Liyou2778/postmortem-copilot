#!/usr/bin/env bash
# Baseline / regression bench for the MCP gateway.
#
# Creates one analysis task, runs it, and samples how many MCP subprocesses and
# how much cgroup memory the service holds while the analysis is in flight.
# Run on the ECS:
#   bash scripts/bench_gateway.sh order-service 30m
#   bash scripts/bench_gateway.sh ecs-copilot 10m
set -u

BASE=${BASE:-http://127.0.0.1:8000}
SERVICE=${1:-order-service}
WINDOW=${2:-30m}
PY=/opt/copilot/.venv/bin/python

TASK=$(curl -s -X POST "$BASE/api/v1/tasks" \
  -H 'content-type: application/json' \
  -d "{\"title\":\"gateway bench $SERVICE\"}" \
  | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["task_id"])')
echo "task=$TASK service=$SERVICE window=$WINDOW"

START=$(date +%s%3N)
curl -s -X POST "$BASE/api/v1/tasks/$TASK/run" \
  -H 'content-type: application/json' \
  -d "{\"service_name\":\"$SERVICE\",\"window\":\"$WINDOW\"}" > /tmp/gateway-bench.json &
CURL_PID=$!
MAX_MCP=0
MAX_MEM=0
while kill -0 "$CURL_PID" 2>/dev/null; do
  N=$(ps -eo args | grep -c '[p]ython.*mcp_servers' || true)
  [ "$N" -gt "$MAX_MCP" ] && MAX_MCP=$N
  MEM=$(systemctl show copilot -p MemoryCurrent --value 2>/dev/null || echo 0)
  [ "$MEM" -gt "$MAX_MEM" ] 2>/dev/null && MAX_MEM=$MEM
  sleep 0.2
done
wait "$CURL_PID"
END=$(date +%s%3N)
ELAPSED=$((END - START))

"$PY" - "$ELAPSED" "$MAX_MCP" "$MAX_MEM" <<'PY'
import json
import sys

elapsed, max_mcp, max_mem = map(int, sys.argv[1:4])
with open("/tmp/gateway-bench.json", encoding="utf-8") as fh:
    data = json.load(fh)
print(json.dumps({
    "elapsed_ms": elapsed,
    "max_mcp_processes": max_mcp,
    "peak_service_memory_mb": round(max_mem / 1024 / 1024, 1),
    "status": data.get("status"),
    "claims": len(data.get("claims") or []),
    "evidence": len(data.get("evidence") or {}),
    "tool_calls": len(data.get("tool_calls") or []),
    "model_mode": data.get("model_mode"),
}, ensure_ascii=False, indent=2))
PY
