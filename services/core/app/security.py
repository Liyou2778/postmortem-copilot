"""Local identity, RBAC, API keys, session auth and tenant scoping.

Why local first: M10.6 needs enforceable tenant/role boundaries before SSO. The
model is deliberately boring and auditable:

* tenants are explicit rows (`team-core`, ...);
* users have one tenant and one role;
* API keys are stored as SHA-256 hashes and shown once;
* session tokens are opaque random strings stored as hashes with expiry;
* every auth event is written to `auth_audit`;
* rate limiting uses a DB-backed fixed window, so it is shared by replicas.

Roles: platform_admin > tenant_admin > operator > viewer.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Any

from .db import Database

ROLE_LEVEL = {
    "viewer": 1,
    "operator": 2,
    "tenant_admin": 3,
    "platform_admin": 4,
}

DDL = """
CREATE TABLE IF NOT EXISTS app_tenant (
    tenant_id VARCHAR(64) NOT NULL,
    name VARCHAR(128) NOT NULL,
    created_at DOUBLE NOT NULL,
    PRIMARY KEY (tenant_id)
);
CREATE TABLE IF NOT EXISTS app_user (
    user_id VARCHAR(64) NOT NULL,
    username VARCHAR(128) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(32) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL,
    active INT NOT NULL DEFAULT 1,
    created_at DOUBLE NOT NULL,
    PRIMARY KEY (user_id),
    UNIQUE (username)
);
CREATE TABLE IF NOT EXISTS api_key (
    key_id VARCHAR(64) NOT NULL,
    key_hash VARCHAR(128) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL,
    name VARCHAR(128) NOT NULL,
    role VARCHAR(32) NOT NULL,
    created_at DOUBLE NOT NULL,
    last_used_at DOUBLE NULL,
    PRIMARY KEY (key_id),
    UNIQUE (key_hash)
);
CREATE TABLE IF NOT EXISTS app_session (
    session_id VARCHAR(64) NOT NULL,
    token_hash VARCHAR(128) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL,
    role VARCHAR(32) NOT NULL,
    created_at DOUBLE NOT NULL,
    expires_at DOUBLE NOT NULL,
    PRIMARY KEY (session_id),
    UNIQUE (token_hash)
);
CREATE TABLE IF NOT EXISTS auth_audit (
    audit_id VARCHAR(64) NOT NULL,
    actor VARCHAR(128) NOT NULL,
    role VARCHAR(32) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL,
    action VARCHAR(64) NOT NULL,
    at DOUBLE NOT NULL,
    PRIMARY KEY (audit_id)
);
CREATE TABLE IF NOT EXISTS auth_rate (
    identity VARCHAR(128) NOT NULL,
    window_start INT NOT NULL,
    count INT NOT NULL,
    PRIMARY KEY (identity, window_start)
);
"""


@dataclass
class Principal:
    user_id: str
    username: str
    tenant_id: str
    role: str
    kind: str = "session"  # api_key | session | basic | anonymous

    @property
    def level(self) -> int:
        return ROLE_LEVEL.get(self.role, 0)


def _hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 240_000
    ).hex()
    return f"pbkdf2_sha256$240000${salt}${digest}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, digest = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        calculated = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(rounds)
        ).hex()
        return hmac.compare_digest(calculated, digest)
    except (ValueError, TypeError):
        return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SecurityStore:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.db.executescript(DDL)

    def _rows(self, sql: str, params: list[Any] | tuple[Any, ...] = ()) -> list[dict]:
        with self.db.lock:
            cur = self.db.conn.cursor()
            cur.execute(self.db.sql(sql), tuple(params))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def _one(self, sql: str, params: list[Any] | tuple[Any, ...] = ()) -> dict | None:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    def _write(self, sql: str, params: list[Any] | tuple[Any, ...]) -> None:
        with self.db.lock:
            cur = self.db.conn.cursor()
            cur.execute(self.db.sql(sql), tuple(params))
            self.db.commit()

    def _audit(self, actor: str, role: str, tenant: str, action: str) -> None:
        self._write(
            self.db.upsert_sql("auth_audit", (
                "audit_id", "actor", "role", "tenant_id", "action", "at",
            )),
            [secrets.token_hex(16), actor[:128], role[:32], tenant[:64],
             action[:64], time.time()],
        )

    # ------------------------------------------------------------------ tenants
    def ensure_tenant(self, tenant_id: str, name: str) -> dict:
        existing = self.get_tenant(tenant_id)
        if existing:
            return existing
        self._write(
            self.db.upsert_sql("app_tenant", ("tenant_id", "name", "created_at")),
            [tenant_id, name, time.time()],
        )
        return self.get_tenant(tenant_id) or {}

    def get_tenant(self, tenant_id: str) -> dict | None:
        return self._one("SELECT * FROM app_tenant WHERE tenant_id = ?", [tenant_id])

    def list_tenants(self) -> list[dict]:
        return self._rows("SELECT * FROM app_tenant ORDER BY tenant_id")

    # -------------------------------------------------------------------- users
    def create_user(self, username: str, password: str, tenant_id: str,
                    role: str = "operator") -> dict:
        if role not in ROLE_LEVEL:
            raise ValueError(f"unknown role {role}")
        self.ensure_tenant(tenant_id, tenant_id)
        if self._one("SELECT user_id FROM app_user WHERE username = ?", [username]):
            raise ValueError(f"user {username} already exists")
        user_id = secrets.token_hex(16)
        self._write(
            self.db.upsert_sql("app_user", (
                "user_id", "username", "password_hash", "role", "tenant_id",
                "active", "created_at",
            )),
            [user_id, username[:128], _hash_password(password), role,
             tenant_id, 1, time.time()],
        )
        self._audit(username, role, tenant_id, "user.create")
        return {"user_id": user_id, "username": username,
                "tenant_id": tenant_id, "role": role}

    def authenticate(self, username: str, password: str) -> Principal | None:
        user = self._one(
            "SELECT * FROM app_user WHERE username = ? AND active = 1", [username]
        )
        if not user or not _verify_password(password, user["password_hash"]):
            return None
        self._audit(username, user["role"], user["tenant_id"], "login.password")
        return Principal(user["user_id"], user["username"], user["tenant_id"],
                         user["role"], "session")

    def bootstrap(self, admin_password: str, tenant_id: str = "team-core") -> None:
        self.ensure_tenant(tenant_id, tenant_id)
        self.ensure_tenant("platform", "platform")
        if not self._one("SELECT user_id FROM app_user LIMIT 1") and admin_password:
            self.create_user("admin", admin_password, "platform", "platform_admin")

    # ------------------------------------------------------------------ API keys
    def create_api_key(self, tenant_id: str, role: str, name: str) -> dict:
        if role not in ROLE_LEVEL:
            raise ValueError(f"unknown role {role}")
        raw = "cpk_" + secrets.token_urlsafe(32)
        key_id = secrets.token_hex(12)
        self._write(
            self.db.upsert_sql("api_key", (
                "key_id", "key_hash", "tenant_id", "name", "role", "created_at",
            )),
            [key_id, _hash_token(raw), tenant_id, name[:128], role, time.time()],
        )
        self._audit(name, role, tenant_id, "api_key.create")
        return {"key_id": key_id, "api_key": raw, "tenant_id": tenant_id,
                "role": role, "name": name}

    def principal_for_username(self, username: str) -> Principal | None:
        """Resolve an already-authenticated SSO identity to local RBAC.

        The trusted-header path never creates users implicitly; an operator
        provisions the local account and role first, so an IdP compromise
        cannot mint platform_admin by inventing a header value.
        """
        user = self._one(
            "SELECT * FROM app_user WHERE username = ? AND active = 1", [username]
        )
        if not user:
            return None
        self._audit(user["username"], user["role"], user["tenant_id"], "login.sso")
        return Principal(user["user_id"], user["username"], user["tenant_id"],
                         user["role"], "sso")

    def principal_for_api_key(self, raw: str) -> Principal | None:
        row = self._one(
            "SELECT * FROM api_key WHERE key_hash = ?", [_hash_token(raw)]
        )
        if not row:
            return None
        self._write(
            "UPDATE api_key SET last_used_at = ? WHERE key_id = ?",
            [time.time(), row["key_id"]],
        )
        self._audit(row["name"], row["role"], row["tenant_id"], "auth.api_key")
        return Principal(row["key_id"], row["name"], row["tenant_id"],
                         row["role"], "api_key")

    # ----------------------------------------------------------------- sessions
    def create_session(self, user: dict, hours: int = 12) -> str:
        raw = "sess_" + secrets.token_urlsafe(32)
        now = time.time()
        self._write(
            self.db.upsert_sql("app_session", (
                "session_id", "token_hash", "user_id", "tenant_id", "role",
                "created_at", "expires_at",
            )),
            [secrets.token_hex(16), _hash_token(raw), user["user_id"],
             user["tenant_id"], user["role"], now, now + max(1, hours) * 3600],
        )
        return raw

    def principal_for_session(self, raw: str) -> Principal | None:
        row = self._one(
            "SELECT s.*, u.username AS username FROM app_session s "
            "LEFT JOIN app_user u ON u.user_id = s.user_id "
            "WHERE s.token_hash = ?", [_hash_token(raw)]
        )
        if not row or float(row["expires_at"]) < time.time():
            return None
        return Principal(row["user_id"], row.get("username") or "session",
                         row["tenant_id"], row["role"], "session")

    # --------------------------------------------------------------- rate limit
    def check_rate(self, identity: str, limit_per_min: int) -> bool:
        window = int(time.time() // 60)
        with self.db.lock:
            row = self._one(
                "SELECT count FROM auth_rate WHERE identity = ? AND window_start = ?",
                [identity, window],
            )
            if row is None:
                self._write(
                    self.db.upsert_sql("auth_rate", (
                        "identity", "window_start", "count",
                    )),
                    [identity[:128], window, 1],
                )
                return True
            count = int(row["count"]) + 1
            self._write(
                "UPDATE auth_rate SET count = ? WHERE identity = ? AND window_start = ?",
                [count, identity[:128], window],
            )
            return count <= max(1, limit_per_min)


__all__ = ["Principal", "SecurityStore", "ROLE_LEVEL"]
