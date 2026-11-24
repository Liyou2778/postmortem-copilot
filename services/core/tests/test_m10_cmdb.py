"""M10.2: self-hosted CMDB + change registry tests."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Database  # noqa: E402
from services.cmdb.store import CmdbStore, create_app  # noqa: E402


class TestCmdbStore(unittest.TestCase):
    def setUp(self):
        self.store = CmdbStore(Database.sqlite(":memory:"))

    def test_seed_has_real_topology_and_changes(self):
        services = self.store.list_services()
        self.assertIn("ecs-copilot", {s["name"] for s in services})
        topology = self.store.topology("ecs-copilot")
        self.assertIn("rds-mysql", topology["downstream"])
        changes = self.store.changes("ecs-copilot", "2h")
        self.assertTrue(changes)
        self.assertIn("CHG-M10", "".join(c["change_id"] for c in changes))

    def test_add_change_requires_registered_service(self):
        with self.assertRaises(Exception):
            self.store.add_change("not-registered", "bad")

    def test_add_change_roundtrip(self):
        added = self.store.add_change(
            "ecs-copilot", "deploy test", change_id="CHG-TEST-001",
            operator="tester",
        )
        self.assertEqual(added["change_id"], "CHG-TEST-001")
        found = self.store.changes("ecs-copilot", "2h")
        self.assertTrue(any(c["change_id"] == "CHG-TEST-001" for c in found))

    def test_delete_change_cleanup_helper(self):
        self.store.add_change("ecs-copilot", "temporary", change_id="CHG-TMP-1")
        self.assertTrue(self.store.delete_change("CHG-TMP-1"))
        self.assertFalse(self.store.delete_change("CHG-TMP-1"))
        ids = {c["change_id"] for c in self.store.changes("ecs-copilot", "2h")}
        self.assertNotIn("CHG-TMP-1", ids)

    def test_topology_404_for_unknown(self):
        self.assertIsNone(self.store.topology("nope"))


class TestCmdbApi(unittest.TestCase):
    def setUp(self):
        self.store = CmdbStore(Database.sqlite(":memory:"))
        self.client = TestClient(create_app(self.store))

    def test_health_and_topology(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        resp = self.client.get("/api/v1/topology/ecs-copilot")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["source"], "cmdb")
        self.assertIn("downstream", resp.json())

    def test_changes_api_and_registration(self):
        resp = self.client.post("/api/v1/changes", json={
            "service": "ecs-copilot", "summary": "M10.2 deploy",
            "operator": "ci", "change_id": "CHG-TEST-002",
        })
        self.assertEqual(resp.status_code, 201)
        listed = self.client.get("/api/v1/changes?service=ecs-copilot&window=2h")
        self.assertEqual(listed.status_code, 200)
        self.assertTrue(any(c["change_id"] == "CHG-TEST-002"
                            for c in listed.json()["changes"]))

    def test_unknown_service_404(self):
        self.assertEqual(
            self.client.get("/api/v1/topology/does-not-exist").status_code, 404
        )
        self.assertEqual(
            self.client.post("/api/v1/changes", json={
                "service": "does-not-exist", "summary": "x"
            }).status_code, 400
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
