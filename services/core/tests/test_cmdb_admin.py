"""M10.2c: CMDB admin page rendering and proxy wiring."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.render import cmdb_admin_page, cmdb_ci_page  # noqa: E402


class TestCmdbAdminRender(unittest.TestCase):
    def test_page_renders_quality_ci_changes_audit(self):
        html = cmdb_admin_page(
            cis=[{
                "ci_id": "ecs-copilot", "ci_type": "service", "name": "Copilot",
                "owner": "SRE", "tier": "P0", "env": "prod", "status": "active",
            }],
            quality={"cis": 9, "relations": 8, "unowned": 0,
                     "missing_tier": 0, "ok": True, "unowned_sample": []},
            changes=[{
                "change_id": "CHG-1", "service_ci": "ecs-copilot",
                "title": "deploy", "risk": "high", "status": "draft",
            }],
            audit=[{
                "actor": "alice", "role": "editor", "action": "ci.upsert",
                "object_type": "ci", "object_id": "ecs-copilot",
            }],
        )
        self.assertIn("企业级 CMDB", html)
        self.assertIn("数据质量", html)
        self.assertIn("CHG-1", html)
        self.assertIn("提交审批", html)
        self.assertIn("alice", html)

    def test_ci_page_escapes_attributes(self):
        html = cmdb_ci_page(
            {"ci_id": "ecs-copilot", "ci_type": "service", "name": "<x>",
             "attributes": {"note": "<script>"}},
            [{"from_ci": "ecs-copilot", "relation_type": "depends_on",
              "to_ci": "rds-mysql", "criticality": "high"}],
            {"affected": [{"ci_id": "rds-mysql", "depth": 1}]},
        )
        self.assertIn("ecs-copilot", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
