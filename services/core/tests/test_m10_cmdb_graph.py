"""M10.2b: enterprise CI graph, change approval, RBAC and audit."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Database  # noqa: E402
from services.cmdb.graph import GraphStore  # noqa: E402
from services.cmdb.store import CmdbStore, create_app  # noqa: E402

TOKENS = {"viewer": "v-token", "editor": "e-token", "approver": "a-token"}


class TestGraphStore(unittest.TestCase):
    def setUp(self):
        self.graph = GraphStore(Database.sqlite(":memory:"))
        for name, ctype in (("svc-a", "service"), ("db", "database"),
                            ("cache", "middleware")):
            self.graph.upsert_ci(
                name, name=name, ci_type=ctype, owner="SRE", tier="P0",
                actor="tester", role="editor",
            )

    def test_upsert_writes_history_and_version(self):
        first = self.graph.get_ci("svc-a")
        self.assertEqual(first["version"], 1)
        self.graph.upsert_ci(
            "svc-a", name="svc-a", ci_type="service", owner="DBA", tier="P0",
            actor="tester", role="editor",
        )
        second = self.graph.get_ci("svc-a")
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["owner"], "DBA")
        rows = self.graph._rows("SELECT version, actor FROM ci_history")  # noqa: SLF001
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["version"], 1)

    def test_relation_and_multi_hop_impact(self):
        self.graph.upsert_ci("svc-b", name="svc-b", ci_type="service")
        self.graph.upsert_ci("svc-c", name="svc-c", ci_type="service")
        self.graph.upsert_relation("svc-a", "svc-b", relation_type="calls",
                                   criticality="high", actor="tester")
        self.graph.upsert_relation("svc-b", "svc-c", relation_type="calls",
                                   criticality="medium", actor="tester")
        topo = self.graph.topology("svc-a")
        self.assertEqual(topo["downstream"], ["svc-b"])
        impact = self.graph.impact("svc-a", "downstream", depth=3)
        affected = {item["ci_id"]: item["depth"] for item in impact["affected"]}
        self.assertEqual(affected["svc-b"], 1)
        self.assertEqual(affected["svc-c"], 2)

    def test_canonical_relation_id_is_idempotent(self):
        self.graph.upsert_relation("svc-a", "db", relation_type="stores_in",
                                   actor="tester")
        self.graph.upsert_relation("svc-a", "db", relation_type="stores_in",
                                   criticality="critical", actor="tester")
        rels = self.graph.relations("svc-a", "downstream")
        self.assertEqual(len(rels), 1)
        self.assertEqual(rels[0]["criticality"], "critical")
        self.assertEqual(len(self.graph._rows(  # noqa: SLF001
            "SELECT * FROM ci_relation_history")), 1)

    def test_quality_report_flags_unowned_cis(self):
        self.graph.upsert_ci(
            "unowned-ci", name="unowned", ci_type="external",
            owner="", attributes={"unowned": True},
        )
        quality = self.graph.quality_report()
        self.assertGreaterEqual(quality["unowned"], 1)
        self.assertIn("unowned-ci", quality["unowned_sample"])
        self.assertFalse(quality["ok"])

    def test_change_approval_state_machine(self):
        change = self.graph.create_change(
            service_ci="svc-a", title="deploy v2", risk="high", actor="ci",
        )
        self.assertEqual(change["status"], "draft")
        change = self.graph.submit_change(change["change_id"], "ci")
        self.assertEqual(change["status"], "pending_approval")
        with self.assertRaises(Exception):
            self.graph.approve_change(change["change_id"], "viewer", "approve",
                                      role="editor")
        change = self.graph.approve_change(
            change["change_id"], "approver", "approve", "LGTM", role="approver"
        )
        self.assertEqual(change["status"], "approved")
        self.assertEqual(change["approvals"][0]["decision"], "approved")
        change = self.graph.execute_change(change["change_id"], "ci", True)
        self.assertEqual(change["status"], "succeeded")
        audit = self.graph.audit_log()
        actions = {row["action"] for row in audit}
        self.assertIn("change.create", actions)
        self.assertIn("change.execute", actions)


class TestV2ApiAndRbac(unittest.TestCase):
    def setUp(self):
        self.legacy = CmdbStore(Database.sqlite(":memory:"))
        self.graph = GraphStore(self.legacy.db)
        self.graph.seed_from_legacy(self.legacy)
        self.client = TestClient(create_app(self.legacy, self.graph, TOKENS))

    def test_v2_requires_token(self):
        self.assertEqual(self.client.get("/api/v2/cis").status_code, 401)
        resp = self.client.get("/api/v2/cis",
                               headers={"X-CMDB-Token": TOKENS["viewer"]})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("ecs-copilot", {ci["ci_id"] for ci in resp.json()["cis"]})

    def test_editor_can_update_ci_and_viewer_cannot(self):
        body = {"name": "ecs-copilot", "ci_type": "service", "owner": "new-owner"}
        denied = self.client.put(
            "/api/v2/cis/ecs-copilot", json=body,
            headers={"X-CMDB-Token": TOKENS["viewer"]},
        )
        self.assertEqual(denied.status_code, 401)
        ok = self.client.put(
            "/api/v2/cis/ecs-copilot", json=body,
            headers={"X-CMDB-Token": TOKENS["editor"],
                     "X-CMDB-Actor": "alice"},
        )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["owner"], "new-owner")

    def test_change_requires_approver_token(self):
        headers = {"X-CMDB-Token": TOKENS["editor"]}
        change = self.client.post("/api/v2/changes", json={
            "service_ci": "ecs-copilot", "title": "deploy v2", "risk": "high",
        }, headers=headers)
        self.assertEqual(change.status_code, 201)
        cid = change.json()["change_id"]
        self.assertEqual(self.client.post(
            f"/api/v2/changes/{cid}/submit", headers=headers).status_code, 200)
        denied = self.client.post(
            f"/api/v2/changes/{cid}/approve", json={"decision": "approve"},
            headers=headers,
        )
        self.assertEqual(denied.status_code, 401)
        approved = self.client.post(
            f"/api/v2/changes/{cid}/approve", json={"decision": "approve"},
            headers={"X-CMDB-Token": TOKENS["approver"]},
        )
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json()["status"], "approved")

    def test_quality_endpoint(self):
        resp = self.client.get(
            "/api/v2/quality", headers={"X-CMDB-Token": TOKENS["viewer"]}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("unowned", resp.json())

    def test_v1_topology_uses_graph(self):
        resp = self.client.get("/api/v1/topology/ecs-copilot")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["source"], "cmdb")
        self.assertIn("prometheus", resp.json()["downstream"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestCmdbTenantScope(unittest.TestCase):
    def setUp(self):
        self.legacy = CmdbStore(Database.sqlite(":memory:"))
        self.graph = GraphStore(self.legacy.db)
        self.graph.upsert_ci("team-a-svc", name="team-a", tenant_id="team-a")
        self.graph.upsert_ci("team-b-svc", name="team-b", tenant_id="team-b")
        self.client = TestClient(create_app(
            self.legacy, self.graph, TOKENS,
            tenant="team-a", platform_token="p-token",
        ))

    def test_team_token_only_sees_own_cis(self):
        headers = {"X-CMDB-Token": TOKENS["viewer"]}
        rows = self.client.get("/api/v2/cis", headers=headers).json()["cis"]
        self.assertEqual({row["ci_id"] for row in rows}, {"team-a-svc"})
        self.assertEqual(
            self.client.get("/api/v2/cis/team-b-svc", headers=headers).status_code,
            404,
        )

    def test_platform_token_sees_all_cis(self):
        rows = self.client.get(
            "/api/v2/cis", headers={"X-CMDB-Token": "p-token"},
        ).json()["cis"]
        self.assertEqual({row["ci_id"] for row in rows}, {"team-a-svc", "team-b-svc"})
