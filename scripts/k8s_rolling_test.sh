#!/usr/bin/env bash
set -u
NODE=k3d-copilot-server-0
docker exec "$NODE" kubectl rollout restart deployment/postmortem-copilot -n copilot >/dev/null
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 2 http://127.0.0.1:8000/healthz || true)
  echo "t=$i code=$code"
  sleep 2
done
docker exec "$NODE" kubectl rollout status deployment/postmortem-copilot -n copilot --timeout=120s
docker exec "$NODE" kubectl get pods -n copilot
