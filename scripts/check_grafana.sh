#!/usr/bin/env bash
set -euo pipefail
PW="$(cat /root/grafana-admin.txt)"
echo "== datasources =="
curl -s -u "admin:$PW" http://127.0.0.1:3000/api/datasources | head -c 500
echo
echo "== dashboards =="
curl -s -u "admin:$PW" 'http://127.0.0.1:3000/api/search?query=Copilot' | head -c 800
echo
echo "== health =="
curl -s http://127.0.0.1:3000/api/health
