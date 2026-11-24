#!/usr/bin/env bash
set -euo pipefail
PW="$(cat /root/keycloak-admin.txt)"
docker exec keycloak /opt/keycloak/bin/kcadm.sh config credentials \
  --server http://127.0.0.1:8080 --realm master --user admin --password "$PW"
docker exec keycloak /opt/keycloak/bin/kcadm.sh update realms/master -s sslRequired=NONE
docker exec keycloak /opt/keycloak/bin/kcadm.sh get realms/master | head -5
