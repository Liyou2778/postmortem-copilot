#!/usr/bin/env bash
# Roll back to the systemd core and restore loopback catalog URLs.
set -eu
systemctl disable --now copilot-k8s-proxy.service 2>/dev/null || true
rm -f /etc/systemd/system/copilot-k8s-proxy.service
systemctl daemon-reload
cd /opt/copilot
.venv/bin/python scripts/switch_catalog_host.py --host 127.0.0.1 --apply
systemctl enable copilot
systemctl start copilot
sleep 3
curl -sf http://127.0.0.1:8000/healthz
echo
echo "rollback complete: systemd core is serving :8000"
