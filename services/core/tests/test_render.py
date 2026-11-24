"""Tests for the server-rendered pages.

These matter because the "traceable" promise is delivered by HTML: if the
evidence link is not generated, the report is just prose.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.render import (  # noqa: E402
    catalog_detail,
    catalog_list,
    datasource_list,
    evidence_page,
    landing,
    md_to_html,
    report_page,
)

TASK = {"task_id": "11111111-2222-3333-4444-555555555555", "title": "订单服务超时复盘",
        "status": "REVIEWING", "plan_version": 1, "last_seq": 14, "steps": {
            "telemetry": {"role": "telemetry", "status": "SUCCEEDED", "attempt": 1, "fence": 1}}}


class TestMarkdownRenderer(unittest.TestCase):
    def test_headings_bold_code(self):
        out = md_to_html("# 标题\n\n**粗** 与 `code`")
        self.assertIn("<h1>标题</h1>", out)
        self.assertIn("<strong>粗</strong>", out)
        self.assertIn("<code>code</code>", out)

    def test_table_rendering(self):
        md = "| a | b |\n|---|---|\n| 1 | 2 |"
        out = md_to_html(md)
        self.assertIn("<table>", out)
        self.assertIn("<th>a</th>", out)
        self.assertIn("<td>2</td>", out)

    def test_html_is_escaped(self):
        out = md_to_html("<script>alert(1)</script>")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_evidence_ids_become_links_when_task_given(self):
        out = md_to_html("依据 `E-TLM-001` 判断", task_id=TASK["task_id"])
        self.assertIn(f'href="/tasks/{TASK["task_id"]}/evidence/E-TLM-001"', out)
        self.assertIn(">E-TLM-001</a>", out)

    def test_evidence_ids_stay_plain_without_task(self):
        out = md_to_html("依据 E-TLM-001 判断")
        self.assertNotIn("<a", out)

    def test_unrelated_uppercase_tokens_are_not_linked(self):
        out = md_to_html("状态 OK 与 ABC-123", task_id=TASK["task_id"])
        self.assertNotIn("evidence/ABC-123", out)


class TestPages(unittest.TestCase):
    def test_landing_lists_tasks_and_form(self):
        html_out = landing([{"task_id": "t1", "title": "某任务", "status": "REVIEWING",
                             "step_summary": "3/3 成功"}], "real")
        self.assertIn("/ui/run", html_out)
        self.assertIn("某任务", html_out)
        self.assertIn("3/3 成功", html_out)

    def test_report_page_links_evidence(self):
        html_out = report_page(TASK, "# 复盘\n\n结论见 `E-CHG-001`。")
        self.assertIn(f"/tasks/{TASK['task_id']}/evidence/E-CHG-001", html_out)

    def test_evidence_page_shows_hash_and_redaction(self):
        ev = {"evidence_id": "E-TLM-001", "source": "metric", "uri": "prometheus://x",
              "captured_at": "2026-11-21T22:00:00Z", "digest": "abc123",
              "redaction_level": "high", "excerpt": "P99 240ms -> 890ms"}
        html_out = evidence_page(TASK, ev, ["结论 A 引用它"])
        self.assertIn("abc123", html_out)
        self.assertIn("强脱敏", html_out)
        self.assertIn("P99 240ms", html_out)
        self.assertIn("结论 A 引用它", html_out)

    def test_evidence_page_marks_mock_data_yellow(self):
        ev = {"evidence_id": "E-LOG-001", "source": "log", "uri": "loki://x",
              "captured_at": "2026-11-22T13:00:00Z", "digest": "d",
              "redaction_level": "high", "data_mode": "mock",
              "excerpt": "【Mock 数据】connection pool exhausted"}
        html_out = evidence_page(TASK, ev, [])
        self.assertIn("badge mock", html_out)
        self.assertIn("不能作为真实故障结论", html_out)

    def test_report_page_warns_when_mock_evidence_is_present(self):
        html_out = report_page(TASK, "# 复盘\n\n- `E-LOG-001` [MOCK] log loki://x\n")
        self.assertIn("Mock / 降级数据", html_out)


class TestCatalogPages(unittest.TestCase):
    def test_catalog_list_shows_services_and_create_form(self):
        html_out = catalog_list(
            [{"name": "ecs-copilot", "display_name": "ECS", "owner": "SRE",
              "tier": "P0", "enabled": True, "binding_count": 1, "mock_count": 0}],
            {"services": 1, "bindings": 1, "datasources": 1},
        )
        self.assertIn("ecs-copilot", html_out)
        self.assertIn("/admin/catalog", html_out)
        self.assertIn("新增", html_out)

    def test_catalog_detail_marks_mock_binding_and_escapes(self):
        service = {
            "name": "order-service", "display_name": "<script>x</script>",
            "owner": "交易", "tier": "P1", "description": "", "enabled": True,
            "bindings": [{"kind": "loki", "datasource_name": "devstack",
                          "mode": "mock", "selector": 'service="order-service"',
                          "metric": "", "secondary_metric": "", "enabled": True,
                          "datasource_enabled": True}],
        }
        html_out = catalog_detail(service, [
            {"name": "devstack", "kind": "multi", "mock": True, "enabled": True}
        ])
        self.assertIn("badge mock", html_out)
        self.assertIn("devstack", html_out)
        self.assertNotIn("<script>x</script>", html_out)
        self.assertIn("&lt;script&gt;", html_out)

    def test_datasource_page_shows_test_result(self):
        html_out = datasource_list(
            [{"name": "ecs-prometheus", "kind": "prometheus",
              "base_url": "http://127.0.0.1:9090", "mock": False,
              "description": "ECS", "enabled": True}],
            test={"name": "ecs-prometheus", "ok": True, "detail": "up 序列 2 条"},
        )
        self.assertIn("连通性测试", html_out)
        self.assertIn("up 序列 2 条", html_out)
        self.assertIn("/admin/datasources/ecs-prometheus/test", html_out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
