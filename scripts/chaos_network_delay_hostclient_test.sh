#!/usr/bin/env bash
set -u
NODE=k3d-copilot-server-0
docker cp /tmp/network-delay-client.yaml "$NODE:/tmp/network-delay-client.yaml"
docker exec "$NODE" kubectl apply -f /tmp/network-delay-client.yaml
sleep 5
echo "== chaos =="
docker exec "$NODE" kubectl get networkchaos -n copilot
echo "== during =="
docker exec "$NODE" kubectl exec -n copilot chaos-client -- python /tmp/chaos_client_probe.py
docker exec "$NODE" kubectl delete -f /tmp/network-delay-client.yaml --timeout=20s >/dev/null 2>&1 || true
sleep 2
if docker exec "$NODE" kubectl get networkchaos copilot-delay-to-client -n copilot >/dev/null 2>&1; then
  docker exec "$NODE" kubectl patch networkchaos copilot-delay-to-client -n copilot \
    --type=merge -p '{"metadata":{"finalizers":[]}}' >/dev/null 2>&1 || true
fi
echo "== cleanup =="
docker exec "$NODE" kubectl get networkchaos -n copilot 2>/dev/null || true
