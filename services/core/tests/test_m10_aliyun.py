"""Tests for the Aliyun discovery mapper (no cloud calls)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from app.db import Database  # noqa: E402
from services.cmdb.aliyun import (  # noqa: E402
    CloudResource, sync_changes, sync_inventory,
)
from services.cmdb.graph import GraphStore  # noqa: E402
from services.cmdb.store import CmdbStore  # noqa: E402


class FakeDiscovery:
    region = "cn-hangzhou"
    default_service = "ecs-copilot"

    def __init__(self, resources):
        self._resources = resources

    def inventory(self):
        return {"resources": self._resources, "errors": [], "region": self.region}

    def recent_changes(self, hours=2, max_results=50):
        return [{
            "event_id": "EVT-1", "event_name": "ModifyInstanceAttribute",
            "event_time": "2026-11-22T17:00:00Z",
            "service": "Ecs", "resource_name": "i-bp-test",
            "operator": "root", "region": self.region,
        }]


class TestAliyunSync(unittest.TestCase):
    def setUp(self):
        self.legacy = CmdbStore(Database.sqlite(":memory:"))
        self.graph = GraphStore(self.legacy.db)
        self.graph.seed_from_legacy(self.legacy)

    def test_tagged_resources_create_ci_and_relation(self):
        discovery = FakeDiscovery([
            CloudResource("rm-1", "ACS::RDS::DBInstance", "cn-hangzhou",
                          "db1", {"service": "ecs-copilot", "owner": "DBA",
                                  "tier": "P0"}),
            CloudResource("bucket-1", "ACS::OSS::Bucket", "cn-hangzhou",
                          "bucket-1", {"service": "ecs-copilot"}),
        ])
        result = sync_inventory(self.graph, discovery, dry_run=False)
        self.assertEqual(result["created"], 2)
        self.assertEqual(result["relations"], 2)
        self.assertEqual(result["unowned"], 0)
        rds = self.graph.get_ci("aliyun-database-rm-1")
        self.assertEqual(rds["ci_type"], "database")
        self.assertEqual(rds["owner"], "DBA")
        logical_relations = self.graph.relations("rds-mysql", "downstream")
        self.assertIn("aliyun-database-rm-1",
                      {r["to_ci"] for r in logical_relations})
        self.assertIn("realized_by",
                      {r["relation_type"] for r in logical_relations})
        service_relations = self.graph.relations("ecs-copilot", "downstream")
        self.assertNotIn("aliyun-database-rm-1",
                         {r["to_ci"] for r in service_relations})

    def test_untagged_resource_is_inventory_only(self):
        discovery = FakeDiscovery([
            CloudResource("rm-2", "ACS::RDS::DBInstance", "cn-hangzhou",
                          "db2", {}),
        ])
        result = sync_inventory(self.graph, discovery, dry_run=False)
        self.assertEqual(result["unowned"], 1)
        self.assertEqual(result["relations"], 0)
        ci = self.graph.get_ci("aliyun-database-rm-2")
        self.assertTrue(ci["attributes"]["unowned"])

    def test_second_sync_is_idempotent(self):
        discovery = FakeDiscovery([
            CloudResource("rm-3", "ACS::RDS::DBInstance", "cn-hangzhou",
                          "db3", {"service": "ecs-copilot"}),
        ])
        sync_inventory(self.graph, discovery, dry_run=False)
        second = sync_inventory(self.graph, discovery, dry_run=False)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["updated"], 0)
        self.assertEqual(second["relations"], 0)

    def test_global_actiontrail_events_are_skipped_when_graph_present(self):
        discovery = FakeDiscovery([])
        result = sync_changes(self.legacy, discovery, graph=self.graph, dry_run=False)
        self.assertEqual(result["written"], 0)
        self.assertEqual(result["skipped"], 1)

    def test_actiontrail_changes_are_written_to_legacy(self):
        discovery = FakeDiscovery([])
        result = sync_changes(self.legacy, discovery, dry_run=False)
        self.assertEqual(result["changes"], 1)
        changes = self.legacy.changes("ecs-copilot", "72h")
        actiontrail = [c for c in changes if c["change_id"] == "AT-EVT-1"]
        self.assertTrue(actiontrail)
        self.assertEqual(actiontrail[0]["kind"], "cloud_change")


if __name__ == "__main__":
    unittest.main(verbosity=2)
