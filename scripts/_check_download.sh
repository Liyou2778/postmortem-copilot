#!/usr/bin/env bash
for u in \
  "https://ghproxy.net/https://github.com/k3d-io/k3d/releases/download/v5.7.4/k3d-linux-amd64" \
  "https://mirror.ghproxy.com/https://github.com/k3d-io/k3d/releases/download/v5.7.4/k3d-linux-amd64" \
  "https://gh.api.99988866.xyz/https://github.com/k3d-io/k3d/releases/download/v5.7.4/k3d-linux-amd64"; do
  echo "== $u"
  curl -sIL -m 20 "$u" | grep -iE 'HTTP/|content-length' | tail -4
done
echo "== aliyun kubectl rpm?"
curl -sIL -m 20 https://mirrors.aliyun.com/kubernetes/yum/repos/kubernetes-el7-x86_64/repodata/repomd.xml | head -3
