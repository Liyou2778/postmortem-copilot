#!/usr/bin/env bash
set -u
echo "== webhook file =="
ls -l /tmp/grafana-alerts.jsonl 2>/dev/null || true
tail -2 /tmp/grafana-alerts.jsonl 2>/dev/null || true
echo "== grafana rules =="
PW="$(cat /root/grafana-admin.txt)"
curl -s -u "admin:$PW" http://127.0.0.1:3000/api/v1/provisioning/alert-rules | head -c 1200
echo
echo "== health =="
curl -s -o /dev/null -w 'app=%{http_code}\n' http://127.0.0.1:8000/healthz
systemctl is-active copilot-alert-webhook
