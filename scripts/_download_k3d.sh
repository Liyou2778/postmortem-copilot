#!/usr/bin/env bash
set -u
URL="https://ghproxy.net/https://github.com/k3d-io/k3d/releases/download/v5.7.4/k3d-linux-amd64"
SIZE=24215704
for i in $(seq 1 20); do
  curl -sL -C - -m 120 -o /tmp/k3d "$URL"
  sz=$(stat -c%s /tmp/k3d 2>/dev/null || echo 0)
  echo "attempt=$i size=$sz"
  if [ "$sz" -ge "$SIZE" ]; then
    break
  fi
done
ls -lh /tmp/k3d
file /tmp/k3d
