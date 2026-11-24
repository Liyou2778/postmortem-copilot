"""Shared MCP gateway lifecycle and wiring tests.

Real MCP processes are exercised by the production smoke test; these tests pin
the lifecycle rules that are easy to get wrong: one shared instance per
process, no per-run close, and a clean fallback when nothing is shared yet.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import mcp_gateway  # noqa: E402
from app.config import Settings  # noqa: E402
from app.lease import LeaseManager  # noqa: E402
from app.orchestrator import Orchestrator  # noqa: E402
from app.service import TaskService  # noqa: E402
from app.store import EventStore  # noqa: E402
from app.tools import AuditedTools  # noqa: E402


class FakeGateway:
    instances = 0

    def __init__(self, **kwargs) -> None:
        FakeGateway.instances += 1
        self.kwargs = kwargs
        self.starts = 0
        self.closes = 0
        self.audit: list = []

    def start(self) -> None:
        self.starts += 1

    def close(self) -> None:
        self.closes += 1

    def call(self, name, **kwargs):  # pragma: no cover - not used here
        raise AssertionError("not expected")


class TestSharedGateway(unittest.TestCase):
    def setUp(self):
        mcp_gateway.close_shared_gateway()
        FakeGateway.instances = 0

    def tearDown(self):
        mcp_gateway.close_shared_gateway()

    def test_empty_gateway_starts_and_closes(self):
        gateway = mcp_gateway.MCPGateway(servers=(), health_interval_s=0)
        gateway.start()
        self.assertEqual(gateway.status(), {})
        self.assertEqual(gateway.available_servers(), {})
        self.assertFalse(gateway.call("prometheus.query_range").ok)
        gateway.close()

    def test_unknown_server_fails_closed(self):
        gateway = mcp_gateway.MCPGateway(servers=(), health_interval_s=0)
        gateway.start()
        result = gateway.call("nope.tool", service="x")
        self.assertFalse(result.ok)
        self.assertIn("unknown server", result.error)
        gateway.close()

    def test_shared_gateway_is_created_once_and_reused(self):
        settings = Settings(tool_backend="mcp", mcp_gateway_mode="shared")
        with patch.object(mcp_gateway, "MCPGateway", FakeGateway):
            first = mcp_gateway.shared_gateway(settings)
            second = mcp_gateway.shared_gateway(settings)
        self.assertIs(first, second)
        self.assertEqual(FakeGateway.instances, 1)
        self.assertEqual(first.starts, 2, "each caller ensures started; start is idempotent")
        mcp_gateway.close_shared_gateway()
        self.assertEqual(first.closes, 1)

    def test_status_without_shared_gateway_is_safe(self):
        self.assertEqual(
            mcp_gateway.shared_gateway_status(),
            {"started": False, "servers": {}},
        )

    def test_orchestrator_does_not_close_shared_gateway(self):
        store = EventStore(":memory:")
        service = TaskService(store, LeaseManager(store._conn, store._lock))
        settings = Settings(
            tool_backend="mcp",
            mcp_gateway_mode="shared",
            mcp_health_interval_s=0,
        )
        fake = FakeGateway()
        with patch.object(mcp_gateway, "shared_gateway", return_value=fake):
            orch = Orchestrator(service, tools=None, settings=settings)
        self.assertIsInstance(orch.tools, AuditedTools)
        self.assertIs(orch.tools.inner, fake)
        self.assertFalse(orch._owns_tools)  # noqa: SLF001
        orch._shutdown_tools()  # noqa: SLF001
        self.assertEqual(fake.closes, 0, "shared gateway must outlive one analysis")


if __name__ == "__main__":
    unittest.main(verbosity=2)
