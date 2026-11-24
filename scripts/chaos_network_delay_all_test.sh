#!/usr/bin/env bash
set -u
NODE=k3d-copilot-server-0
docker cp /tmp/network-delay-all.yaml "$NODE:/tmp/network-delay-all.yaml"
docker exec "$NODE" kubectl apply -f /tmp/network-delay-all.yaml
sleep 6
echo "== chaos =="
docker exec "$NODE" kubectl get networkchaos -n copilot
echo "== public timings =="
for i in $(seq 1 8); do
  curl -s -o /dev/null -w '%{http_code} %{time_total}\n' -m 5 http://127.0.0.1:8000/healthz || true
  sleep 1
done
docker exec "$NODE" kubectl delete -f /tmp/network-delay-all.yaml --timeout=20s >/dev/null 2>&1 || true
sleep 3
if docker exec "$NODE" kubectl get networkchaos copilot-network-delay-all -n copilot >/dev/null 2>&1; then
  docker exec "$NODE" kubectl patch networkchaos copilot-network-delay-all -n copilot \
    --type=merge -p '{"metadata":{"finalizers":[]}}' >/dev/null 2>&1 || true
fi
echo "== cleanup =="
docker exec "$NODE" kubectl get networkchaos -n copilot 2>/dev/null || true
