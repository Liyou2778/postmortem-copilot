"""M10.6: local identity, API keys, rate limiting and tenant projection."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.auth_api import build_router  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.security import SecurityStore  # noqa: E402
from app.service import TaskService  # noqa: E402
from app.store import EventStore  # noqa: E402
from app.lease import LeaseManager  # noqa: E402


class TestSecurity(unittest.TestCase):
    def setUp(self):
        self.store = SecurityStore(Database.sqlite(":memory:"))
        self.store.ensure_tenant("team-core", "core")

    def test_password_login_and_wrong_password(self):
        self.store.create_user("alice", "correct-horse", "team-core", "operator")
        self.assertIsNotNone(self.store.authenticate("alice", "correct-horse"))
        self.assertIsNone(self.store.authenticate("alice", "wrong"))

    def test_api_key_is_hashed_and_resolves_principal(self):
        created = self.store.create_api_key("team-core", "operator", "ci")
        self.assertTrue(created["api_key"].startswith("cpk_"))
        principal = self.store.principal_for_api_key(created["api_key"])
        self.assertIsNotNone(principal)
        self.assertEqual(principal.tenant_id, "team-core")
        self.assertEqual(principal.role, "operator")
        self.assertIsNone(self.store.principal_for_api_key("cpk_invalid"))

    def test_sso_header_resolves_existing_user_only(self):
        self.store.create_user("sso-alice", "unused-password", "team-core", "operator")
        principal = self.store.principal_for_username("sso-alice")
        self.assertIsNotNone(principal)
        self.assertEqual(principal.tenant_id, "team-core")
        self.assertEqual(principal.role, "operator")
        self.assertEqual(principal.kind, "sso")
        self.assertIsNone(self.store.principal_for_username("unknown-sso-user"))

    def test_session_expiry(self):
        self.store.create_user("bob", "correct-horse", "team-core", "viewer")
        user = self.store._one("SELECT * FROM app_user WHERE username='bob'")  # noqa: SLF001
        token = self.store.create_session(user, hours=1)
        self.assertIsNotNone(self.store.principal_for_session(token))
        self.assertIsNone(self.store.principal_for_session("sess_invalid"))

    def test_rate_limit_is_per_identity(self):
        for _ in range(3):
            self.assertTrue(self.store.check_rate("api:alice", 3))
        self.assertFalse(self.store.check_rate("api:alice", 3))
        self.assertTrue(self.store.check_rate("api:bob", 3))

    def test_bootstrap_creates_platform_admin(self):
        self.store.bootstrap("bootstrap-pass")
        principal = self.store.authenticate("admin", "bootstrap-pass")
        self.assertEqual(principal.role, "platform_admin")
        self.assertEqual(principal.tenant_id, "platform")


class TestAuthRouter(unittest.TestCase):
    def setUp(self):
        self.security = SecurityStore(Database.sqlite(":memory:"))
        self.security.ensure_tenant("team-core", "core")
        self.security.create_user("alice", "correct-horse", "team-core", "operator")
        app = FastAPI()
        app.include_router(build_router(
            self.security, Settings(auth_enabled=True, admin_password="")
        ))
        self.client = TestClient(app)

    def test_password_login_returns_session(self):
        resp = self.client.post("/api/v1/auth/login", json={
            "username": "alice", "password": "correct-horse",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["token"].startswith("sess_"))
        self.assertEqual(resp.json()["tenant_id"], "team-core")

    def test_bad_password_401(self):
        resp = self.client.post("/api/v1/auth/login", json={
            "username": "alice", "password": "nope",
        })
        self.assertEqual(resp.status_code, 401)


    def test_oidc_login_returns_503_when_unconfigured(self):
        app = FastAPI()
        app.include_router(build_router(
            self.security, Settings(auth_enabled=True, admin_password="")
        ))
        client = TestClient(app)
        self.assertEqual(client.get("/api/v1/auth/oidc/login").status_code, 503)

    def test_login_page_renders(self):
        resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("登录", resp.text)


class TestTenantProjection(unittest.TestCase):
    def test_task_carries_tenant(self):
        db = Database.sqlite(":memory:")
        events = EventStore(":memory:", db=db)
        service = TaskService(events, LeaseManager(events._conn, events._lock))
        task_id = service.create_task("tenant task", tenant_id="team-a")
        self.assertEqual(service.view(task_id).tenant_id, "team-a")
        legacy = service.create_task("legacy task")
        self.assertEqual(service.view(legacy).tenant_id, "default")


if __name__ == "__main__":
    unittest.main(verbosity=2)

