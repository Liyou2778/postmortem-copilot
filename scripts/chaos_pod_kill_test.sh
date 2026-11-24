#!/usr/bin/env bash
set -u
NODE=k3d-copilot-server-0
docker cp /tmp/pod-kill.yaml "$NODE:/tmp/pod-kill.yaml"
docker exec "$NODE" kubectl apply -f /tmp/pod-kill.yaml
sleep 8
echo "== chaos =="
docker exec "$NODE" kubectl get podchaos -n copilot
echo "== pods =="
docker exec "$NODE" kubectl get pods -n copilot
echo "== public =="
for i in $(seq 1 8); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:8000/healthz || true)
  echo -n "$code "
  sleep 2
done
echo
sleep 20
echo "== pods after =="
docker exec "$NODE" kubectl get pods -n copilot
docker exec "$NODE" kubectl get events -n copilot --sort-by=.lastTimestamp | tail -8
docker exec "$NODE" kubectl delete -f /tmp/pod-kill.yaml
