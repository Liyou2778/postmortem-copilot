#!/usr/bin/env bash
set -u
NODE=k3d-copilot-server-0
docker cp /tmp/io-chaos-client.yaml "$NODE:/tmp/io-chaos-client.yaml"
docker cp /tmp/io-latency.yaml "$NODE:/tmp/io-latency.yaml"
docker exec "$NODE" kubectl delete pod io-chaos-client -n copilot --ignore-not-found >/dev/null 2>&1 || true
docker exec "$NODE" kubectl apply -f /tmp/io-chaos-client.yaml >/dev/null
docker exec "$NODE" kubectl wait --for=condition=Ready pod/io-chaos-client -n copilot --timeout=60s >/dev/null
sleep 4
echo "== baseline writer =="
docker exec "$NODE" kubectl logs -n copilot io-chaos-client --tail=6
docker exec "$NODE" kubectl apply -f /tmp/io-latency.yaml
sleep 6
echo "== iochaos =="
docker exec "$NODE" kubectl get iochaos -n copilot
echo "== during writer =="
docker exec "$NODE" kubectl logs -n copilot io-chaos-client --tail=8
docker exec "$NODE" kubectl delete -f /tmp/io-latency.yaml --timeout=20s >/dev/null 2>&1 || true
sleep 2
if docker exec "$NODE" kubectl get iochaos copilot-io-latency -n copilot >/dev/null 2>&1; then
  docker exec "$NODE" kubectl patch iochaos copilot-io-latency -n copilot \
    --type=merge -p '{"metadata":{"finalizers":[]}}' >/dev/null 2>&1 || true
fi
docker exec "$NODE" kubectl delete pod io-chaos-client -n copilot --ignore-not-found >/dev/null 2>&1 || true
echo "== cleanup =="
docker exec "$NODE" kubectl get iochaos -n copilot 2>/dev/null || true
