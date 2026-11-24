#!/usr/bin/env bash
# Four concurrent analyses against the shared gateway.
# Run on the ECS: bash scripts/concurrent_check.sh
set -u
cd /opt/copilot
rm -f /tmp/m9-concurrent-*.json
for i in 1 2 3 4; do
  .venv/bin/python scripts/run_analysis.py --service ecs-copilot --window 3m \
    > "/tmp/m9-concurrent-$i.json" 2>&1 &
done
wait
echo "== results =="
for f in /tmp/m9-concurrent-*.json; do
  echo "-- $f"
  grep -E '"(status|claims|elapsed_s)"' "$f" | head -3
done
echo "== mcp process count =="
ps -eo args | grep -c '[p]ython.*mcp_servers'
echo "== gateway metrics =="
curl -s http://127.0.0.1:8000/metrics | grep -E \
  'copilot_mcp_tool_calls_total|copilot_mcp_server_restarts_total' | head -10
