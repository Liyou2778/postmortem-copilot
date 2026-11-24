#!/usr/bin/env bash
set -u
NODE=k3d-copilot-server-0
docker exec "$NODE" kubectl delete pod chaos-client -n copilot --ignore-not-found >/dev/null 2>&1 || true
docker exec "$NODE" kubectl run chaos-client -n copilot --image=postmortem-copilot:m10.7z \
  --restart=Never --labels=role=chaos-client --command -- sleep 3600 >/dev/null
docker exec "$NODE" kubectl wait --for=condition=Ready pod/chaos-client -n copilot --timeout=60s >/dev/null
docker cp /tmp/chaos_client_probe_direct.py "$NODE:/tmp/chaos_client_probe_direct.py"
docker exec "$NODE" kubectl cp /tmp/chaos_client_probe_direct.py copilot/chaos-client:/tmp/
echo "== baseline direct =="
docker exec "$NODE" kubectl exec -n copilot chaos-client -- python /tmp/chaos_client_probe_direct.py
docker cp /tmp/http-delay.yaml "$NODE:/tmp/http-delay.yaml"
docker exec "$NODE" kubectl apply -f /tmp/http-delay.yaml
sleep 5
echo "== during direct =="
docker exec "$NODE" kubectl exec -n copilot chaos-client -- python /tmp/chaos_client_probe_direct.py
docker exec "$NODE" kubectl delete -f /tmp/http-delay.yaml --timeout=20s >/dev/null 2>&1 || true
sleep 2
if docker exec "$NODE" kubectl get httpchaos copilot-http-delay -n copilot >/dev/null 2>&1; then
  docker exec "$NODE" kubectl patch httpchaos copilot-http-delay -n copilot \
    --type=merge -p '{"metadata":{"finalizers":[]}}' >/dev/null 2>&1 || true
fi
docker exec "$NODE" kubectl delete pod chaos-client -n copilot --ignore-not-found >/dev/null 2>&1 || true
echo "== cleanup =="
docker exec "$NODE" kubectl get httpchaos -n copilot 2>/dev/null || true
