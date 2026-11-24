#!/usr/bin/env bash
# Read-only probe of the local Prometheus + node_exporter on the ECS.
# Used to prove we are reading real metrics before switching the tool backend.
set -u

PROM=http://127.0.0.1:9090

echo "== versions =="
curl -s --max-time 5 "$PROM/api/v1/status/buildinfo" | head -c 300; echo
echo "== targets (health) =="
curl -s --max-time 5 "$PROM/api/v1/targets" | tr ',' '\n' | grep -E '"health"|"job"|"service"' | head -12

echo "== up =="
curl -s --max-time 5 "$PROM/api/v1/query?query=up" | head -c 500; echo

echo "== node_load1 (instant) =="
curl -s --max-time 5 --get "$PROM/api/v1/query" --data-urlencode 'query=node_load1{service="ecs-copilot"}' | head -c 400; echo

echo "== node_load1 (15m range) =="
NOW=$(date -u +%s)
START=$((NOW - 900))
curl -s --max-time 5 --get "$PROM/api/v1/query_range" \
  --data-urlencode 'query=node_load1{service="ecs-copilot"}' \
  --data-urlencode "start=$START" --data-urlencode "end=$NOW" --data-urlencode "step=60" \
  | head -c 900; echo

echo "== selected node metric names =="
curl -s --max-time 5 "$PROM/api/v1/label/__name__/values" \
  | tr ',' '\n' | grep -E 'node_(load|cpu|memory|filesystem|network)' | head -30

echo "== prometheus retention / storage =="
curl -s --max-time 5 "$PROM/api/v1/status/runtimeinfo" | head -c 500; echo

echo "== app health =="
curl -s --max-time 5 http://127.0.0.1:8000/healthz; echo

echo "== listeners =="
ss -ltnp 2>/dev/null | grep -E ':(9090|9100|8000|9101)\b' || true

echo "== copilot env (secrets masked) =="
grep -vE 'KEY|TOKEN|PASSWORD|SECRET' /opt/copilot/.env 2>/dev/null | grep -v '^#' | grep -v '^$' || true
