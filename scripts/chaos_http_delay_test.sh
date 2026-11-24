#!/usr/bin/env bash
set -u
NODE=k3d-copilot-server-0
docker exec "$NODE" kubectl delete pod chaos-client -n copilot --ignore-not-found >/dev/null 2>&1 || true
docker cp /tmp/http-delay.yaml "$NODE:/tmp/http-delay.yaml"
docker exec "$NODE" kubectl apply -f /tmp/http-delay.yaml
sleep 5
echo "== chaos =="
docker exec "$NODE" kubectl get httpchaos -n copilot
echo "== public health timings =="
for i in $(seq 1 8); do
  curl -s -o /dev/null -w '%{http_code} %{time_total}\n' -m 5 http://127.0.0.1:8000/healthz || true
  sleep 1
done
docker exec "$NODE" kubectl delete -f /tmp/http-delay.yaml --timeout=20s >/dev/null 2>&1 || true
sleep 2
if docker exec "$NODE" kubectl get httpchaos copilot-http-delay -n copilot >/dev/null 2>&1; then
  docker exec "$NODE" kubectl patch httpchaos copilot-http-delay -n copilot \
    --type=merge -p '{"metadata":{"finalizers":[]}}' >/dev/null 2>&1 || true
fi
echo "== cleanup =="
docker exec "$NODE" kubectl get httpchaos -n copilot 2>/dev/null || true
