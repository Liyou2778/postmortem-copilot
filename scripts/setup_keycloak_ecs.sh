#!/usr/bin/env bash
set -euo pipefail
docker pull quay.io/keycloak/keycloak:26.0
PW="$(openssl rand -hex 16)"
printf '%s\n' "$PW" > /root/keycloak-admin.txt
chmod 600 /root/keycloak-admin.txt
docker rm -f keycloak >/dev/null 2>&1 || true
docker run -d --name keycloak --restart unless-stopped \
  -p 8080:8080 \
  -e KC_BOOTSTRAP_ADMIN_USERNAME=admin \
  -e KC_BOOTSTRAP_ADMIN_PASSWORD="$PW" \
  -e KC_HOSTNAME=http://8.136.102.215:8080 \
  -e KC_HOSTNAME_STRICT=false \
  quay.io/keycloak/keycloak:26.0 start-dev
sleep 10
docker ps --filter name=keycloak --format '{{.Status}}'
curl -s -o /dev/null -w 'local=%{http_code}\n' \
  http://127.0.0.1:8080/realms/master/.well-known/openid-configuration
