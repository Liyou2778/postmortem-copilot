#!/usr/bin/env bash
set -euo pipefail
URL="https://53330eddb4407d49-8-136-102-215.serveousercontent.com"
ADMIN_PW="$(cat /root/keycloak-admin.txt)"

docker stop keycloak >/dev/null
rm -rf /tmp/keycloak-data
mkdir -p /tmp/keycloak-data
docker cp keycloak:/opt/keycloak/data/. /tmp/keycloak-data/
docker volume create keycloak-data >/dev/null
docker run --rm --entrypoint sh \
  -v keycloak-data:/data -v /tmp/keycloak-data:/src \
  quay.io/keycloak/keycloak:26.0 \
  -c 'rm -rf /data/*; cp -a /src/. /data/; ls /data | head'
docker rm keycloak >/dev/null

docker run -d --name keycloak --restart unless-stopped \
  -p 8080:8080 \
  -v keycloak-data:/opt/keycloak/data \
  -e KC_BOOTSTRAP_ADMIN_USERNAME=admin \
  -e KC_BOOTSTRAP_ADMIN_PASSWORD="$ADMIN_PW" \
  -e KC_HOSTNAME="$URL" \
  -e KC_HOSTNAME_STRICT=false \
  -e KC_PROXY_HEADERS=xforwarded \
  quay.io/keycloak/keycloak:26.0 start-dev

sleep 12
docker ps --filter name=keycloak --format '{{.Status}}'
curl -s -o /dev/null -w 'local_master=%{http_code}\n' \
  http://127.0.0.1:8080/realms/master/.well-known/openid-configuration
curl -s -o /dev/null -w 'local_realm=%{http_code}\n' \
  http://127.0.0.1:8080/realms/copilot/.well-known/openid-configuration
curl -s -o /dev/null -w 'public_realm=%{http_code}\n' \
  "$URL/realms/copilot/.well-known/openid-configuration"
