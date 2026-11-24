"""Service catalog + catalog-aware MCP routing + real Prometheus query tests.

The catalog is the trust boundary for evidence: a service either has a real
datasource binding or it does not, and a mock datasource can never be labelled
real. These tests pin that behaviour down before it reaches a report.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from app.catalog import CatalogError, CatalogStore  # noqa: E402
from app.catalog_api import build_router  # noqa: E402
from app.catalog_router import CatalogRouter  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.tools import ToolResult, evidence_mode  # noqa: E402


def catalog_settings(**overrides) -> Settings:
    base = dict(
        prometheus_url="http://127.0.0.1:19090",
        devstack_url="http://127.0.0.1:19100",
        tool_url_allowlist=("127.0.0.1", "localhost"),
        catalog_seed=True,
    )
    base.update(overrides)
    return Settings(**base)


def make_catalog(seed: bool = True) -> tuple[CatalogStore, Settings]:
    settings = catalog_settings()
    store = CatalogStore(Database.sqlite(":memory:"), settings, seed=seed)
    return store, settings


class TestCatalogStore(unittest.TestCase):
    def test_seed_is_idempotent_and_creates_real_and_mock(self):
        catalog, _ = make_catalog(seed=True)
        stats = catalog.stats()
        self.assertEqual(stats["services"], 2)
        self.assertEqual(stats["datasources"], 5,
                         "ecs-prometheus + devstack + ecs-loki + ecs-cmdb + ecs-deploy")
        self.assertEqual(stats["bindings"], 9, "ecs-copilot 4 + order-service 5")
        # A second seed call must not duplicate rows.
        catalog.seed_defaults()
        self.assertEqual(catalog.stats(), stats)
        real = catalog.resolve("ecs-copilot", "prometheus")
        real_logs = catalog.resolve("ecs-copilot", "loki")
        mock = catalog.resolve("order-service", "loki")
        self.assertEqual(real["mode"], "real")
        self.assertFalse(real["mock"])
        self.assertEqual(real["metric_type"], "gauge")
        self.assertEqual(real["extra_metrics"][0]["metric"],
                         "node_disk_io_time_seconds_total")
        self.assertEqual(real["extra_metrics"][0]["type"], "counter")
        self.assertEqual(real["impact_policy"]["currency"], "CNY")
        self.assertEqual(real_logs["mode"], "real")
        self.assertEqual(real_logs["datasource_name"], "ecs-loki")
        self.assertEqual(mock["mode"], "mock")
        self.assertTrue(mock["mock"])

    def test_unbound_service_resolves_to_none(self):
        catalog, _ = make_catalog()
        catalog.upsert_service("new-service")
        self.assertIsNone(catalog.resolve("new-service", "prometheus"))
        self.assertIsNone(catalog.resolve("does-not-exist", "prometheus"))

    def test_mock_datasource_cannot_be_bound_as_real(self):
        catalog, _ = make_catalog()
        with self.assertRaises(CatalogError):
            catalog.upsert_binding(
                "ecs-copilot", "loki", datasource_name="devstack", mode="real"
            )
        with self.assertRaises(CatalogError):
            catalog.upsert_binding(
                "ecs-copilot", "loki", datasource_name="ecs-prometheus", mode="mock"
            )

    def test_binding_kind_must_match_datasource_kind(self):
        catalog, _ = make_catalog()
        # ecs-prometheus is a prometheus datasource, not a Loki one.
        with self.assertRaises(CatalogError):
            catalog.upsert_binding(
                "ecs-copilot", "loki", datasource_name="ecs-prometheus", mode="real"
            )

    def test_disabled_binding_is_invisible_to_the_agent(self):
        catalog, _ = make_catalog()
        catalog.upsert_binding(
            "ecs-copilot", "loki", datasource_name="devstack", mode="mock", enabled=False
        )
        self.assertIsNone(catalog.resolve("ecs-copilot", "loki"))

    def test_selector_rejects_newlines(self):
        catalog, _ = make_catalog()
        with self.assertRaises(CatalogError):
            catalog.upsert_binding(
                "ecs-copilot", "prometheus", datasource_name="ecs-prometheus",
                mode="real", selector='service="x"\n} or up',
            )

    def test_datasource_in_use_cannot_be_deleted(self):
        catalog, _ = make_catalog()
        with self.assertRaises(CatalogError):
            catalog.delete_datasource("devstack")
        catalog.delete_binding("order-service", "prometheus")
        catalog.delete_binding("order-service", "loki")
        catalog.delete_binding("order-service", "cmdb")
        catalog.delete_binding("order-service", "deploy")
        catalog.delete_binding("order-service", "kb")
        catalog.delete_datasource("devstack")
        self.assertIsNone(catalog.get_datasource("devstack"))

    def test_url_must_be_allowlisted(self):
        catalog, _ = make_catalog()
        with self.assertRaises(CatalogError):
            catalog.upsert_datasource("evil", kind="prometheus",
                                      base_url="http://169.254.169.254/latest")
        with self.assertRaises(CatalogError):
            catalog.upsert_datasource("bad-scheme", kind="prometheus",
                                      base_url="file:///etc/passwd")


class FakeInner:
    def __init__(self, data: dict | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.audit: list = []
        self.data = data or {"points": [{"t": "2026-11-22T13:00:00Z", "v": 1.0}],
                             "metric": "node_load1"}

    def call(self, name: str, **kwargs):
        self.calls.append((name, kwargs))
        return ToolResult(True, name, data=dict(self.data))


class TestCatalogRouter(unittest.TestCase):
    def setUp(self):
        self.catalog, self.settings = make_catalog()

    def test_injects_datasource_selector_metric_and_mode(self):
        inner = FakeInner()
        router = CatalogRouter(inner, self.catalog, self.settings)
        result = router.call(
            "prometheus.query_range", service="order-service", window="30m"
        )
        self.assertTrue(result.ok)
        _, kwargs = inner.calls[0]
        self.assertEqual(kwargs["datasource_url"], "http://127.0.0.1:19100")
        self.assertEqual(kwargs["data_mode"], "mock")
        self.assertEqual(kwargs["selector"], 'service="order-service"')
        self.assertEqual(kwargs["metric"], "latency_p99_ms")
        self.assertEqual(result.data["data_mode"], "mock")
        self.assertEqual(result.data["datasource"], "devstack")

    def test_secondary_slot_uses_secondary_metric(self):
        inner = FakeInner()
        router = CatalogRouter(inner, self.catalog, self.settings)
        router.call("prometheus.query_range", service="order-service", slot="secondary")
        self.assertEqual(inner.calls[0][1]["metric"], "qps")
        self.assertEqual(inner.calls[0][1]["metric_type"], "gauge")

    def test_extra_slot_uses_metric_pack_and_counter_type(self):
        inner = FakeInner()
        router = CatalogRouter(inner, self.catalog, self.settings)
        result = router.call(
            "prometheus.query_range", service="ecs-copilot", slot="extra:0"
        )
        self.assertTrue(result.ok)
        kwargs = inner.calls[0][1]
        self.assertEqual(kwargs["metric"], "node_disk_io_time_seconds_total")
        self.assertEqual(kwargs["metric_type"], "counter")

    def test_missing_extra_slot_fails_closed(self):
        inner = FakeInner()
        router = CatalogRouter(inner, self.catalog, self.settings)
        result = router.call(
            "prometheus.query_range", service="ecs-copilot", slot="extra:9"
        )
        self.assertFalse(result.ok)
        self.assertIn("extra:9", result.error)
        self.assertEqual(inner.calls, [])

    def test_impact_gets_source_and_selector_without_a_metric(self):
        inner = FakeInner(data={"summary": {"requests_incident": 1}, "queries": {}})
        router = CatalogRouter(inner, self.catalog, self.settings)
        result = router.call("prometheus.impact", service="order-service", window="30m")
        self.assertTrue(result.ok)
        _, kwargs = inner.calls[0]
        self.assertEqual(kwargs["datasource_url"], "http://127.0.0.1:19100")
        self.assertEqual(kwargs["selector"], 'service="order-service"')
        self.assertEqual(kwargs["data_mode"], "mock")
        self.assertIn("impact_policy", kwargs)
        self.assertNotIn("metric", kwargs, "impact has its own metric parameters")

    def test_unbound_kind_fails_closed(self):
        inner = FakeInner()
        router = CatalogRouter(inner, self.catalog, self.settings)
        # ecs-copilot has metrics/logs/CMDB/deploy but no KB binding: must not
        # hit any upstream instead of falling back to another datasource.
        result = router.call("kb.search_postmortems", service="ecs-copilot")
        self.assertFalse(result.ok)
        self.assertIn("服务目录未配置", result.error)
        self.assertEqual(inner.calls, [])

    def test_unbound_secondary_metric_fails_closed(self):
        self.catalog.upsert_service("bare-service")
        self.catalog.upsert_binding(
            "bare-service", "prometheus", datasource_name="ecs-prometheus",
            mode="real", metric="node_load1",
        )
        inner = FakeInner()
        router = CatalogRouter(inner, self.catalog, self.settings)
        result = router.call(
            "prometheus.query_range", service="bare-service", slot="secondary"
        )
        self.assertFalse(result.ok)
        self.assertIn("次要指标", result.error)
        self.assertEqual(inner.calls, [])

    def test_disallowed_datasource_url_is_blocked(self):
        ds = self.catalog.upsert_datasource(
            "other", kind="prometheus", base_url="http://127.0.0.1:19999"
        )
        self.catalog.upsert_binding(
            "ecs-copilot", "loki", datasource_name="devstack", mode="mock"
        )
        # Narrow the allowlist after the row exists: the router, not the store,
        # is the last line of defence.
        self.settings = Settings(
            prometheus_url=self.settings.prometheus_url,
            devstack_url=self.settings.devstack_url,
            tool_url_allowlist=("localhost",),
        )
        inner = FakeInner()
        router = CatalogRouter(inner, self.catalog, self.settings)
        result = router.call("loki.query", service="ecs-copilot")
        self.assertFalse(result.ok)
        self.assertIn("SSRF", result.error)
        self.assertEqual(inner.calls, [])
        self.assertEqual(ds["name"], "other")

    def test_fixture_downgrade_is_never_relabelled(self):
        inner = FakeInner(data={"source": "fixture", "data_mode": "fixture",
                                "points": []})
        router = CatalogRouter(inner, self.catalog, self.settings)
        result = router.call("prometheus.query_range", service="order-service")
        self.assertEqual(result.data["data_mode"], "fixture")
        self.assertEqual(evidence_mode(result.data), "fixture")


class TestCatalogAuth(unittest.TestCase):
    def test_json_api_requires_basic_auth_when_password_is_set(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        settings = catalog_settings(admin_user="admin", admin_password="s3cret")
        catalog = CatalogStore(Database.sqlite(":memory:"), settings, seed=True)
        app = FastAPI()
        app.include_router(build_router(catalog, settings))
        client = TestClient(app)

        # Both the HTML page and the JSON API must reject anonymous access;
        # the JSON read endpoint was accidentally open in the first deploy.
        self.assertEqual(client.get("/admin/catalog").status_code, 401)
        self.assertEqual(client.get("/api/v1/catalog/services").status_code, 401)
        self.assertEqual(client.get("/api/v1/catalog/datasources").status_code, 401)
        ok = client.get("/api/v1/catalog/services", auth=("admin", "s3cret"))
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(
            client.get("/api/v1/catalog/services", auth=("admin", "wrong")).status_code,
            401,
        )
        self.assertEqual(
            client.put(
                "/api/v1/catalog/services/x",
                json={"owner": "o"},
                auth=("admin", "s3cret"),
            ).status_code,
            200,
        )


class TestCatalogForms(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        self.settings = catalog_settings()
        self.catalog = CatalogStore(
            Database.sqlite(":memory:"), self.settings, seed=True
        )
        app = FastAPI()
        app.include_router(build_router(self.catalog, self.settings))
        self.client = TestClient(app)

    def test_create_service_and_binding_via_html_form(self):
        resp = self.client.post(
            "/admin/catalog",
            data={"name": "pay-service", "display_name": "支付", "owner": "pay",
                  "tier": "P1", "description": "form test"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/admin/catalog/pay-service", resp.headers["location"])
        self.assertIsNotNone(self.catalog.get_service("pay-service"))

        resp = self.client.post(
            "/admin/catalog/pay-service/bindings",
            data={"kind": "loki", "datasource_name": "devstack", "mode": "mock",
                  "selector": 'service="pay-service"', "metric": "",
                  "secondary_metric": ""},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        binding = self.catalog.resolve("pay-service", "loki")
        self.assertIsNotNone(binding)
        self.assertEqual(binding["mode"], "mock")

    def test_form_rejects_mock_flag_on_real_datasource(self):
        resp = self.client.post(
            "/admin/catalog/ecs-copilot/bindings",
            data={"kind": "loki", "datasource_name": "ecs-loki",
                  "mode": "mock", "selector": "", "metric": ""},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("error=", resp.headers["location"])
        # The rejected write must not have overwritten the real binding.
        binding = self.catalog.resolve("ecs-copilot", "loki")
        self.assertIsNotNone(binding)
        self.assertEqual(binding["mode"], "real")
        self.assertEqual(binding["datasource_name"], "ecs-loki")


class _PromHandler(BaseHTTPRequestHandler):
    mode = "series"
    captured: dict = {}

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        _PromHandler.captured = {
            key: values[0] for key, values in parse_qs(parsed.query).items()
        }
        if parsed.path.endswith("/api/v1/query"):
            payload = {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{"metric": {}, "value": [1795353902, "100"]}],
                },
            }
        elif _PromHandler.mode == "empty":
            payload = {"status": "success", "data": {"resultType": "matrix", "result": []}}
        else:
            payload = {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [{
                        "metric": {"__name__": "node_load1",
                                   "service": "ecs-copilot"},
                        "values": [[1795353902, "0.1"], [1795353962, "0.15"],
                                   [1795354022, "0.9"], [1795354082, "0.8"]],
                    }],
                },
            }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # silence test output
        return


class TestPrometheusMCPQuery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _PromHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        from services.mcp_servers import prometheus as prom

        cls.prom = prom
        cls.old_base, cls.old_allow = prom.BASE_URL, prom.ALLOWLIST
        prom.BASE_URL = f"http://127.0.0.1:{cls.port}"
        prom.ALLOWLIST = ("127.0.0.1", "localhost")

    @classmethod
    def tearDownClass(cls):
        cls.prom.BASE_URL, cls.prom.ALLOWLIST = cls.old_base, cls.old_allow
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _PromHandler.mode = "series"

    def test_real_query_uses_prometheus_api_semantics(self):
        result = self.prom.query_range(
            "ecs-copilot", metric="node_load1", selector='service="ecs-copilot"',
            window="30m", datasource_url=f"http://127.0.0.1:{self.port}",
            data_mode="real",
        )
        self.assertEqual(result["source"], "prometheus")
        self.assertEqual(result["data_mode"], "real")
        self.assertEqual(len(result["points"]), 4)
        self.assertTrue(result["anomaly"], "0.1 -> 0.9 应该被识别为异常")
        self.assertEqual(result["anomaly"]["direction"], "up")
        self.assertEqual(_PromHandler.captured["query"],
                         'node_load1{service="ecs-copilot"}')
        self.assertIn("start", _PromHandler.captured)
        self.assertIn("end", _PromHandler.captured)
        self.assertEqual(_PromHandler.captured["step"], "15")

    def test_empty_result_is_not_replaced_by_fixture(self):
        _PromHandler.mode = "empty"
        result = self.prom.query_range(
            "order-service", metric="latency_p99_ms", window="30m",
            datasource_url=f"http://127.0.0.1:{self.port}",
        )
        self.assertEqual(result["source"], "prometheus")
        self.assertEqual(result["points"], [])
        self.assertTrue(result["no_data"])

    def test_unreachable_upstream_degrades_and_says_fixture(self):
        result = self.prom.query_range(
            "order-service", metric="latency_p99_ms", window="30m",
            datasource_url="http://127.0.0.1:1", data_mode="mock",
        )
        self.assertEqual(result["source"], "fixture")
        self.assertEqual(result["data_mode"], "fixture")
        self.assertTrue(result["points"])

    def test_query_builder_escapes_service(self):
        query = self.prom.build_query("latency_p99_ms", "", 'a"b')
        self.assertEqual(query, 'latency_p99_ms{service="a\\"b"}')

    def test_cpu_counter_builds_utilization_expression(self):
        query = self.prom.build_query(
            "node_cpu_seconds_total", 'service="ecs-copilot"',
            "ecs-copilot", metric_type="counter", rate_window="90s",
        )
        self.assertIn("100 * (1 - avg(rate(node_cpu_seconds_total", query)
        self.assertIn('mode="idle"', query)
        self.assertIn("[90s]", query)

    def test_counter_query_wraps_rate(self):
        query = self.prom.build_query(
            "node_disk_io_time_seconds_total", 'service="ecs-copilot"',
            "ecs-copilot", metric_type="counter", rate_window="90s",
        )
        self.assertEqual(
            query,
            'rate(node_disk_io_time_seconds_total{service="ecs-copilot"}[90s])',
        )

    def test_impact_overview_returns_queries_and_summary(self):
        result = self.prom.impact_overview(
            "ecs-copilot", window="30m", selector='service="ecs-copilot"',
            datasource_url=f"http://127.0.0.1:{self.port}", data_mode="real",
        )
        self.assertEqual(result["source"], "prometheus")
        self.assertEqual(result["data_mode"], "real")
        self.assertEqual(
            set(result["queries"]), {"requests", "error_rate", "p95", "targets_up"}
        )
        self.assertIn("requests_incident", result["summary"])
        self.assertIn("p95_incident", result["summary"])
        self.assertIn("impact", result)

    def test_impact_policy_produces_estimated_business_numbers(self):
        import json as _json

        result = self.prom.impact_overview(
            "ecs-copilot", window="30m", selector='service="ecs-copilot"',
            datasource_url=f"http://127.0.0.1:{self.port}", data_mode="real",
            impact_policy=_json.dumps({
                "users_per_request": 0.5,
                "cost_per_minute": 2.0,
                "currency": "CNY",
            }),
        )
        business = result["business_impact"]
        self.assertTrue(business["configured"])
        self.assertEqual(business["requests_total"], 100.0)
        self.assertEqual(business["estimated_affected_users"], 50.0)
        self.assertEqual(business["estimated_cost"], 60.0)
        self.assertEqual(business["currency"], "CNY")

    def test_anomaly_detects_a_drop(self):
        points = [{"t": "t1", "v": 4.0}, {"t": "t2", "v": 3.9},
                  {"t": "t3", "v": 0.4}, {"t": "t4", "v": 0.2}]
        anomaly = self.prom._detect_anomaly(points)  # noqa: SLF001
        self.assertEqual(anomaly["direction"], "down")
        self.assertLess(anomaly["change_pct"], 0)

    def test_tiny_baseline_spike_beats_infinite_relative_drop(self):
        """Regression from the first ECS drill: load 0.01 -> 6.7 with a 0.01
        dip was reported as a -100% drop because the dip had infinite relative
        strength. The real signal is the upward movement."""
        points = [{"t": "t1", "v": 0.01}, {"t": "t2", "v": 0.0},
                  {"t": "t3", "v": 0.02}, {"t": "t4", "v": 6.68},
                  {"t": "t5", "v": 5.21}]
        anomaly = self.prom._detect_anomaly(points)  # noqa: SLF001
        self.assertEqual(anomaly["direction"], "up")
        self.assertGreater(anomaly["v"], 6.0)
        self.assertGreater(anomaly["delta"], 6.0)


class TestLokiQueryBuilder(unittest.TestCase):
    def test_literal_pattern_stays_literal(self):
        from services.mcp_servers import loki

        query = loki.build_query('service="order-service"', "error", "order-service")
        self.assertEqual(query, '{service="order-service"} |= "error"')

    def test_regex_mode_adds_self_log_exclusion(self):
        from services.mcp_servers import loki

        query = loki.build_query(
            'job="journal",unit="copilot.service"',
            "re:(?i)(error|timeout)", "ecs-copilot",
        )
        self.assertIn("|~", query)
        self.assertIn("!~", query)
        self.assertIn("loki/api/v1", query, "MCP self-logs must not match their own query")

    def test_window_parser(self):
        from services.mcp_servers import loki

        self.assertEqual(loki.parse_window("30m"), 1800)
        self.assertEqual(loki.parse_window("10m"), 600)
        self.assertEqual(loki.parse_window("bogus"), 1800)


if __name__ == "__main__":
    unittest.main(verbosity=2)
