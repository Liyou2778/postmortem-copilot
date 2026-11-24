#!/usr/bin/env bash
set -u
NODE=k3d-copilot-server-0
docker cp /tmp/stress-cpu.yaml "$NODE:/tmp/stress-cpu.yaml"
docker exec "$NODE" kubectl apply -f /tmp/stress-cpu.yaml
sleep 6
echo "== chaos =="
docker exec "$NODE" kubectl get stresschaos -n copilot
echo "== public =="
for i in $(seq 1 6); do
  curl -s -o /dev/null -w '%{http_code} %{time_total}\n' -m 5 http://127.0.0.1:8000/healthz || true
  sleep 1
done
docker exec "$NODE" kubectl delete -f /tmp/stress-cpu.yaml --timeout=20s >/dev/null 2>&1 || true
sleep 2
if docker exec "$NODE" kubectl get stresschaos copilot-cpu-stress -n copilot >/dev/null 2>&1; then
  docker exec "$NODE" kubectl patch stresschaos copilot-cpu-stress -n copilot \
    --type=merge -p '{"metadata":{"finalizers":[]}}' >/dev/null 2>&1 || true
fi
docker exec "$NODE" kubectl get stresschaos -n copilot 2>/dev/null || true
