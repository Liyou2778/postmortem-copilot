"""Alert ingestion / dedup / notification tests."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.incidents import (  # noqa: E402
    Alert,
    Deduper,
    IncidentResult,
    Notifier,
    parse,
    parse_alertmanager,
    parse_generic,
    report_message,
)

ALERTMANAGER = {
    "version": "4",
    "status": "firing",
    "alerts": [
        {
            "status": "firing",
            "labels": {"alertname": "HighLatency", "service": "order-service",
                       "severity": "critical"},
            "annotations": {"summary": "P99 延迟超阈"},
            "startsAt": "2026-11-22T12:00:00Z",
        },
        {
            "status": "firing",
            "labels": {"alertname": "HighErrorRate", "app": "pay-service",
                       "severity": "warning"},
            "annotations": {"description": "5xx 比例升高"},
        },
    ],
}


class TestParsing(unittest.TestCase):
    def test_alertmanager_payload(self):
        alerts = parse_alertmanager(ALERTMANAGER)
        self.assertEqual(len(alerts), 2)
        self.assertEqual(alerts[0].service, "order-service")
        self.assertEqual(alerts[0].name, "HighLatency")
        self.assertEqual(alerts[0].severity, "critical")
        self.assertEqual(alerts[0].summary, "P99 延迟超阈")
        # `app` is a common alternative label for the service identity
        self.assertEqual(alerts[1].service, "pay-service")

    def test_generic_payload(self):
        alerts = parse_generic({"service": "search-service", "alert": "Down",
                                "severity": "critical"})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].service, "search-service")

    def test_parse_prefers_alertmanager(self):
        self.assertEqual(len(parse(ALERTMANAGER)), 2)
        self.assertEqual(len(parse({"service": "x"})), 1)

    def test_alert_without_service_is_dropped(self):
        payload = {"alerts": [{"labels": {"alertname": "NoService"}}]}
        self.assertEqual(parse_alertmanager(payload), [])

    def test_empty_payload(self):
        self.assertEqual(parse({}), [])


class TestDedup(unittest.TestCase):
    def test_same_service_within_window_is_deduplicated(self):
        d = Deduper(window_s=60)
        self.assertTrue(d.accept("order-service"))
        self.assertFalse(d.accept("order-service"), "窗口内重复告警应被丢弃")
        self.assertTrue(d.accept("pay-service"), "不同服务互不影响")

    def test_expired_window_accepts_again(self):
        d = Deduper(window_s=0)  # disabled window
        self.assertTrue(d.accept("a"))
        self.assertTrue(d.accept("a"))

    def test_window_zero_never_dedups(self):
        d = Deduper(window_s=0)
        for _ in range(3):
            self.assertTrue(d.accept("svc"))


class TestNotifier(unittest.TestCase):
    def test_disabled_without_urls(self):
        n = Notifier([])
        self.assertFalse(n.enabled)
        self.assertEqual(n.send("t", "b"), 0)

    def test_payload_shape_per_channel(self):
        n = Notifier(["https://oapi.dingtalk.com/robot/send?access_token=x",
                      "https://open.feishu.cn/open-apis/bot/v2/hook/y",
                      "https://example.com/hook"])
        import json as _json

        ding = _json.loads(n._body(n._urls[0], "标题", "内容"))  # noqa: SLF001
        feishu = _json.loads(n._body(n._urls[1], "标题", "内容"))  # noqa: SLF001
        generic = _json.loads(n._body(n._urls[2], "标题", "内容"))  # noqa: SLF001
        self.assertEqual(ding["msgtype"], "markdown")
        self.assertEqual(feishu["msg_type"], "text")
        self.assertIn("text", generic)


class TestReportMessage(unittest.TestCase):
    def test_success_message_contains_link_and_count(self):
        r = IncidentResult(task_id="abcdef1234", service="order-service",
                           status="ok", report_url="http://h/tasks/1/report", claims=3)
        title, body = report_message(r)
        self.assertIn("order-service", title)
        self.assertIn("http://h/tasks/1/report", body)
        self.assertIn("3", body)

    def test_failure_message_says_human_required(self):
        r = IncidentResult(task_id="abcdef1234", service="x", status="failed")
        _, body = report_message(r)
        self.assertIn("人工", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
