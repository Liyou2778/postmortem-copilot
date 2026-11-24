#!/usr/bin/env bash
# Blue-green cutover: systemd core -> k3d NodePort, behind a local TCP proxy.
# Production data stays in RDS/OSS; systemd unit is stopped, not deleted.
set -eu
K3D_NODE_URL=${K3D_NODE_URL:-http://127.0.0.1:18000}

if ! curl -sf "$K3D_NODE_URL/healthz" >/dev/null; then
  echo "K8s app is not healthy at $K3D_NODE_URL; refusing cutover" >&2
  exit 1
fi

cat > /etc/systemd/system/copilot-k8s-proxy.service <<'UNIT'
[Unit]
Description=TCP proxy :8000 -> k3d NodePort :18000
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
ExecStart=/usr/bin/socat TCP-LISTEN:8000,fork,reuseaddr TCP:127.0.0.1:18000
Restart=always
RestartSec=2
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload

systemctl disable copilot
systemctl stop copilot
systemctl enable --now copilot-k8s-proxy.service
sleep 3
curl -sf http://127.0.0.1:8000/healthz
echo
echo "cutover complete: public :8000 -> k3d :18000"
echo "rollback: bash /opt/copilot/scripts/k8s_rollback.sh"
