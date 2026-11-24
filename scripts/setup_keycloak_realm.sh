#!/usr/bin/env bash
set -euo pipefail
BASE=http://127.0.0.1:8080
ISSUER=http://8.136.102.215:8080/realms/copilot
REDIRECT=http://121.41.224.245:8000/api/v1/auth/oidc/callback
APP_ORIGIN=http://121.41.224.245:8000
KC=/opt/keycloak/bin/kcadm.sh
ADMIN_PW="$(cat /root/keycloak-admin.txt)"
docker exec keycloak "$KC" config credentials \
  --server http://127.0.0.1:8080 --realm master --user admin --password "$ADMIN_PW"

# Idempotent realm + client.
docker exec keycloak "$KC" get realms/copilot >/dev/null 2>&1 || \
  docker exec keycloak "$KC" create realms -s realm=copilot -s enabled=true -s sslRequired=NONE
docker exec keycloak "$KC" get clients -r copilot -q clientId=postmortem-copilot --fields id \
  --format csv --noquotes 2>/dev/null | tail -1 > /tmp/client_id_raw || true
CLIENT_ID="$(tail -1 /tmp/client_id_raw 2>/dev/null || true)"
if [ -z "${CLIENT_ID:-}" ] || [ "$CLIENT_ID" = "id" ]; then
  docker exec keycloak "$KC" create clients -r copilot \
    -s clientId=postmortem-copilot \
    -s enabled=true \
    -s publicClient=false \
    -s standardFlowEnabled=true \
    -s directAccessGrantsEnabled=false \
    -s serviceAccountsEnabled=false \
    -s protocol=openid-connect \
    -s "redirectUris=[\"$REDIRECT\"]" \
    -s "webOrigins=[\"$APP_ORIGIN\"]" >/dev/null
  CLIENT_ID="$(docker exec keycloak "$KC" get clients -r copilot -q clientId=postmortem-copilot --fields id --format csv --noquotes | tail -1)"
fi

SECRET="$(docker exec keycloak "$KC" get clients/"$CLIENT_ID"/client-secret \
  -r copilot --fields value --format csv --noquotes | tail -1)"

TEST_PW="$(openssl rand -hex 12)"
printf '%s\n' "$TEST_PW" > /root/keycloak-test-user.txt
chmod 600 /root/keycloak-test-user.txt
docker exec keycloak "$KC" get users -r copilot -q username=sso-test --fields id \
  --format csv --noquotes 2>/dev/null | tail -1 > /tmp/user_raw || true
if ! grep -qE '^[0-9a-f-]{36}$' /tmp/user_raw 2>/dev/null; then
  docker exec keycloak "$KC" create users -r copilot \
    -s username=sso-test -s enabled=true -s email=sso-test@example.com \
    -s firstName=SSO -s lastName=Test >/dev/null
fi
docker exec keycloak "$KC" set-password -r copilot --username sso-test \
  --new-password "$TEST_PW"

cat > /root/keycloak-oidc.env <<EOF
OIDC_ISSUER=$ISSUER
OIDC_CLIENT_ID=postmortem-copilot
OIDC_CLIENT_SECRET=$SECRET
OIDC_REDIRECT_URI=$REDIRECT
OIDC_TEST_USER=sso-test
EOF
chmod 600 /root/keycloak-oidc.env
echo "realm/client/user configured"
echo "client_id=$CLIENT_ID"
echo "oidc env written to /root/keycloak-oidc.env (secret not printed)"
curl -s -o /dev/null -w "realm_discovery=%{http_code}\n" \
  "$BASE/realms/copilot/.well-known/openid-configuration"
