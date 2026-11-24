#!/usr/bin/env bash
set -u
NODE=k3d-copilot-server-0
docker exec "$NODE" kubectl patch networkchaos copilot-network-delay -n copilot \
  --type=merge -p '{"metadata":{"finalizers":[]}}' || true
sleep 2
docker exec "$NODE" kubectl get networkchaos -n copilot || true
