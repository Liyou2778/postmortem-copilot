"""M10.4: DAG validation, local replan and consistent-hash assignment."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import Settings  # noqa: E402
from app.dag import (  # noqa: E402
    ConsistentHashRing, PlanError, assign_steps, descendants, local_replan,
    topological_order, validate_plan,
)
from app.llm import LLMConfig, ModelClient  # noqa: E402
from app.orchestrator import Orchestrator  # noqa: E402
from app.service import TaskService  # noqa: E402
from app.store import EventStore  # noqa: E402
from app.lease import LeaseManager  # noqa: E402
from app.tools import build_default_registry  # noqa: E402

PLAN = [
    {"step_id": "a", "role": "telemetry", "depends_on": []},
    {"step_id": "b", "role": "log", "depends_on": []},
    {"step_id": "c", "role": "change", "depends_on": ["a"]},
    {"step_id": "d", "role": "reviewer", "depends_on": ["a", "b", "c"]},
]


class TestDag(unittest.TestCase):
    def test_validate_and_topological_order(self):
        plan = validate_plan(PLAN)
        self.assertEqual(len(plan), 4)
        order = topological_order(plan)
        self.assertLess(order.index("a"), order.index("c"))
        self.assertLess(order.index("c"), order.index("d"))
        self.assertLess(order.index("b"), order.index("d"))

    def test_cycle_is_rejected(self):
        with self.assertRaises(PlanError):
            validate_plan([
                {"step_id": "a", "role": "x", "depends_on": ["b"]},
                {"step_id": "b", "role": "x", "depends_on": ["a"]},
            ])

    def test_unknown_and_duplicate_are_rejected(self):
        with self.assertRaises(PlanError):
            validate_plan([{"step_id": "a", "role": "x", "depends_on": ["nope"]}])
        with self.assertRaises(PlanError):
            validate_plan([
                {"step_id": "a", "role": "x"},
                {"step_id": "a", "role": "y"},
            ])

    def test_descendants_and_local_replan(self):
        self.assertEqual(descendants(PLAN, "a"), ["c", "d"])
        result = local_replan(PLAN, "a")
        self.assertEqual(result["failed"], "a")
        self.assertEqual(set(result["skip"]), {"c", "d"})
        self.assertIn("b", result["runnable"])

    def test_consistent_hash_is_stable_and_minimally_remaps(self):
        keys = [f"step-{i}" for i in range(200)]
        ring3 = ConsistentHashRing(["w1", "w2", "w3"])
        ring4 = ConsistentHashRing(["w1", "w2", "w3", "w4"])
        self.assertEqual(ring3.assignment(keys), ring3.assignment(keys))
        self.assertLess(ring3.remap_fraction(ring4, keys), 0.45)
        self.assertEqual(len(set(ring4.assignment(keys).values())), 4)
        decision = assign_steps(PLAN, ["w1", "w2", "w3"])
        self.assertEqual(set(decision.assignments), {"a", "b"})


class TestDagFailureReplan(unittest.TestCase):
    def build(self):
        store = EventStore(":memory:")
        leases = LeaseManager(store._conn, store._lock)
        service = TaskService(store, leases)
        llm = ModelClient(LLMConfig(base_url="", api_key="", model=""))
        settings = Settings()
        orchestrator = Orchestrator(service, llm, build_default_registry(settings), settings)
        task_id = service.create_task("DAG failure test")
        return orchestrator, service, task_id

    def test_telemetry_failure_skips_only_descendants(self):
        orch, service, task_id = self.build()
        with patch("app.orchestrator.TelemetryAgent.run",
                   side_effect=RuntimeError("telemetry down")):
            report = orch.run(task_id, "order-service")
        self.assertIn("telemetry", report.failed_steps)
        self.assertIn("impact", report.skipped_steps)
        self.assertIn("review", report.skipped_steps)
        view = service.view(task_id)
        self.assertEqual(view.step_status("log").value, "SUCCEEDED")
        self.assertEqual(view.step_status("change").value, "SUCCEEDED")
        self.assertEqual(view.step_status("impact").value, "SKIPPED")
        self.assertEqual(view.step_status("review").value, "SKIPPED")
        self.assertEqual(report.status, "WAITING_HUMAN")


if __name__ == "__main__":
    unittest.main(verbosity=2)
