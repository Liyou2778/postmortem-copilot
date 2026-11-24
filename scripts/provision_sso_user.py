"""Provision the local RBAC user used by the Keycloak OIDC test."""
import secrets
import sys

sys.path.insert(0, "services/core")
from app.config import load_settings
from app.db import from_settings
from app.security import SecurityStore


def main() -> int:
    store = SecurityStore(from_settings(load_settings()))
    username = "sso-test"
    if store.principal_for_username(username):
        print("sso-test already exists")
        return 0
    store.create_user(username, secrets.token_urlsafe(24), "team-core", "operator")
    print("sso-test created in team-core/operator")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
