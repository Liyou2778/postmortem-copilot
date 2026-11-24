"""OTLP projection tests: no collector required."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.trace import export_otlp, to_otlp  # noqa: E402


def sample_trace():
    return {
        "task_id": "t1",
        "trace_id": "a" * 32,
        "spans": [{
            "name": "analysis task",
            "span_id": "b" * 16,
            "parent_span_id": "",
            "trace_id": "a" * 32,
            "kind": "SERVER",
            "start_time_unix_nano": 10,
            "end_time_unix_nano": 20,
            "status": "OK",
            "attributes": {"task_id": "t1", "events": 3},
        }],
    }


class TestOtlp(unittest.TestCase):
    def test_projection_has_otlp_shape(self):
        payload = to_otlp(sample_trace(), "svc")
        spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
        self.assertEqual(spans[0]["traceId"], "a" * 32)
        self.assertEqual(spans[0]["kind"], 2)
        self.assertEqual(spans[0]["status"]["code"], 1)
        attrs = {item["key"]: item["value"] for item in spans[0]["attributes"]}
        self.assertEqual(attrs["task_id"], {"stringValue": "t1"})
        self.assertEqual(attrs["events"], {"intValue": "3"})

    def test_missing_endpoint_is_not_an_exception(self):
        result = export_otlp(sample_trace(), "")
        self.assertFalse(result["ok"])
        self.assertIn("not configured", result["error"])

    def test_successful_post(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b""

        with patch("app.trace.urllib.request.urlopen", return_value=Response()) as call:
            result = export_otlp(sample_trace(), "http://collector:4318", timeout=1)
        self.assertTrue(result["ok"])
        self.assertEqual(call.call_args[0][0].full_url,
                         "http://collector:4318/v1/traces")


if __name__ == "__main__":
    unittest.main(verbosity=2)
