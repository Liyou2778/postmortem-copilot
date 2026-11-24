#!/usr/bin/env bash
set -u
for img in chaos-mesh chaos-daemon chaos-dashboard chaos-coredns; do
  echo "== $img"
  timeout 180 docker pull "ghcr.nju.edu.cn/chaos-mesh/${img}:v2.8.8" 2>&1 | tail -3
  docker tag "ghcr.nju.edu.cn/chaos-mesh/${img}:v2.8.8" "ghcr.io/chaos-mesh/${img}:v2.8.8" 2>/dev/null || true
done
docker images --format '{{.Repository}}:{{.Tag}} {{.Size}}' | grep -E 'chaos'
