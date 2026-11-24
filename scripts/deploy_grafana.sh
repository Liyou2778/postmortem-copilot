#!/usr/bin/env bash
set -euo pipefail
PW="$(openssl rand -hex 16)"
printf '%s\n' "$PW" > /root/grafana-admin.txt
chmod 600 /root/grafana-admin.txt
docker rm -f grafana >/dev/null 2>&1 || true
docker run -d --name grafana --restart unless-stopped \
  -p 3000:3000 \
  -e GF_SECURITY_ADMIN_USER=admin \
  -e GF_SECURITY_ADMIN_PASSWORD="$PW" \
  -e GF_USERS_ALLOW_SIGN_UP=false \
  -v /opt/copilot/deploy/grafana/provisioning:/etc/grafana/provisioning:ro \
  -v /opt/copilot/deploy/grafana/dashboards:/var/lib/grafana/dashboards:ro \
  grafana/grafana:11.2.0
sleep 8
docker ps --filter name=grafana --format '{{.Status}}'
curl -s -o /dev/null -w 'grafana_health=%{http_code}\n' http://127.0.0.1:3000/api/health
docker logs --tail=12 grafana 2>&1 | tail -12
