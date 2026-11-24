#!/usr/bin/env bash
set -euo pipefail
ADMIN_PW="$(cat /root/keycloak-admin.txt)"
docker rm -f keycloak >/dev/null 2>&1 || true
docker run -d --name keycloak --restart unless-stopped \
  -p 8080:8080 \
  -v keycloak-data:/opt/keycloak/data \
  -e KC_BOOTSTRAP_ADMIN_USERNAME=admin \
  -e KC_BOOTSTRAP_ADMIN_PASSWORD="$ADMIN_PW" \
  -e KC_HOSTNAME_STRICT=false \
  -e KC_HOSTNAME_STRICT_BACKCHANNEL=false \
  -e KC_PROXY_HEADERS=xforwarded \
  quay.io/keycloak/keycloak:26.0 start-dev
sleep 12
docker ps --filter name=keycloak --format '{{.Status}}'
echo "local issuer:"
curl -s http://127.0.0.1:8080/realms/copilot/.well-known/openid-configuration | \
  python3 -c 'import json,sys; print(json.load(sys.stdin).get("issuer"))'
