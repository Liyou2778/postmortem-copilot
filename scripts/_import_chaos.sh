#!/usr/bin/env bash
set -u
timeout 120 docker pull ghcr.nju.edu.cn/chaos-mesh/chaos-coredns:v0.2.8 2>&1 | tail -3 || true
docker tag ghcr.nju.edu.cn/chaos-mesh/chaos-coredns:v0.2.8 ghcr.io/chaos-mesh/chaos-coredns:v0.2.8 2>/dev/null || true
k3d image import \
  ghcr.io/chaos-mesh/chaos-mesh:v2.8.8 \
  ghcr.io/chaos-mesh/chaos-daemon:v2.8.8 \
  ghcr.io/chaos-mesh/chaos-dashboard:v2.8.8 \
  ghcr.io/chaos-mesh/chaos-coredns:v0.2.8 \
  -c copilot 2>&1 | tail -5
