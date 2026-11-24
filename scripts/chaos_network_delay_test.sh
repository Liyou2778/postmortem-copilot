#!/usr/bin/env bash
set -u
NODE=k3d-copilot-server-0
docker cp /tmp/network-delay.yaml "$NODE:/tmp/network-delay.yaml"
docker exec "$NODE" kubectl apply -f /tmp/network-delay.yaml
sleep 8
echo "== chaos =="
docker exec "$NODE" kubectl get networkchaos -n copilot
echo "== public timings =="
for i in $(seq 1 8); do
  curl -s -o /dev/null -w '%{http_code} %{time_total}\n' -m 5 http://127.0.0.1:8000/healthz || true
  sleep 1
done
docker exec "$NODE" kubectl delete -f /tmp/network-delay.yaml
