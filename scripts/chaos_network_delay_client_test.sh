#!/usr/bin/env bash
set -u
NODE=k3d-copilot-server-0
MEASURE='import time,urllib.request
for _ in range(6):
    s=time.perf_counter()
    urllib.request.urlopen("http://postmortem-copilot:8000/healthz", timeout=5).read()
    print(round(time.perf_counter()-s,4))
'
docker exec "$NODE" kubectl delete pod chaos-client -n copilot --ignore-not-found >/dev/null 2>&1 || true
docker exec "$NODE" kubectl run chaos-client -n copilot --image=postmortem-copilot:m10.7x \
  --restart=Never --labels=role=chaos-client --command -- sleep 3600 >/dev/null
docker exec "$NODE" kubectl wait --for=condition=Ready pod/chaos-client -n copilot --timeout=60s >/dev/null
echo "== baseline =="
docker exec "$NODE" kubectl exec -n copilot chaos-client -- python -c "$MEASURE"

docker cp /tmp/network-delay-client.yaml "$NODE:/tmp/network-delay-client.yaml"
docker exec "$NODE" kubectl apply -f /tmp/network-delay-client.yaml
sleep 5
echo "== chaos =="
docker exec "$NODE" kubectl get networkchaos -n copilot
echo "== during =="
docker exec "$NODE" kubectl exec -n copilot chaos-client -- python -c "$MEASURE"

docker exec "$NODE" kubectl delete -f /tmp/network-delay-client.yaml --timeout=20s >/dev/null 2>&1 || true
sleep 2
if docker exec "$NODE" kubectl get networkchaos copilot-delay-to-client -n copilot >/dev/null 2>&1; then
  docker exec "$NODE" kubectl patch networkchaos copilot-delay-to-client -n copilot \
    --type=merge -p '{"metadata":{"finalizers":[]}}' >/dev/null 2>&1 || true
fi
docker exec "$NODE" kubectl delete pod chaos-client -n copilot --ignore-not-found >/dev/null 2>&1 || true
echo "== cleanup =="
docker exec "$NODE" kubectl get networkchaos,pod -n copilot 2>/dev/null | head
