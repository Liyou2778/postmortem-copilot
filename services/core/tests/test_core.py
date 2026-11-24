"""Tests for the event-sourced core.

Run with:  python -m unittest discover -s services/core/tests -v
No third-party dependencies required.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.events import EventType  # noqa: E402
from app.lease import LeaseManager, StaleLease  # noqa: E402
from app.service import (  # noqa: E402
    IllegalTransition,
    NeedsHumanDecision,
    TaskService,
    StepStatus,
    TaskStatus,
)
from app.statemachine import project  # noqa: E402
from app.store import EventStore  # noqa: E402


def build_service() -> TaskService:
    store = EventStore(":memory:")
    leases = LeaseManager(store._conn, store._lock)  # shared connection keeps the test simple
    return TaskService(store, leases)


STEPS = [
    {"step_id": "s1", "role": "telemetry"},
    {"step_id": "s2", "role": "log"},
    {"step_id": "s3", "role": "change"},
]


class TestLifecycle(unittest.TestCase):
    def test_full_lifecycle_and_replay_equivalence(self):
        svc = build_service()
        task_id = svc.create_task("P1 故障复盘")

        svc.plan(task_id, STEPS)
        svc.start(task_id)

        for step in ("s1", "s2", "s3"):
            fence = svc.lease_step(task_id, step, owner="w1")
            self.assertIsNotNone(fence)
            svc.begin_step(task_id, step, "w1", fence)
            svc.succeed(task_id, step, fence, {"ok": True})

        svc.submit_for_review(task_id)
        svc.deliver(task_id, "s3://reports/r1.docx", approver="sre-lead")

        live = svc.view(task_id)
        replayed = project(svc._store.load(task_id))

        self.assertEqual(live.status, TaskStatus.DELIVERED)
        self.assertEqual(replayed.status, live.status)
        self.assertEqual(replayed.steps, live.steps)
        self.assertTrue(live.all_steps_terminal())

    def test_journal_is_monotonic(self):
        svc = build_service()
        task_id = svc.create_task("t")
        svc.plan(task_id, STEPS)
        seqs = [e.seq for e in svc._store.load(task_id)]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))


class TestIllegalTransitions(unittest.TestCase):
    def test_cannot_deliver_before_review(self):
        svc = build_service()
        task_id = svc.create_task("t")
        svc.plan(task_id, STEPS)
        svc.start(task_id)
        with self.assertRaises(IllegalTransition):
            svc.deliver(task_id, "s3://x", approver="a")

    def test_cannot_review_with_open_steps(self):
        svc = build_service()
        task_id = svc.create_task("t")
        svc.plan(task_id, STEPS)
        svc.start(task_id)
        with self.assertRaises(IllegalTransition):
            svc.submit_for_review(task_id)

    def test_cannot_start_same_step_twice(self):
        svc = build_service()
        task_id = svc.create_task("t")
        svc.plan(task_id, STEPS)
        svc.start(task_id)
        fence = svc.lease_step(task_id, "s1", "w1")
        svc.begin_step(task_id, "s1", "w1", fence)
        with self.assertRaises(IllegalTransition):
            svc.begin_step(task_id, "s1", "w1", fence)


class TestLeaseFencing(unittest.TestCase):
    def test_stale_worker_write_is_rejected(self):
        svc = build_service()
        task_id = svc.create_task("t")
        svc.plan(task_id, STEPS)
        svc.start(task_id)

        stale = svc.lease_step(task_id, "s1", owner="w-old", ttl_ms=1)
        self.assertEqual(stale, 1)

        # Lease lapses, a replacement worker takes over.
        import time

        time.sleep(0.01)
        fresh = svc.lease_step(task_id, "s1", owner="w-new", ttl_ms=30_000)
        self.assertEqual(fresh, 2)

        with self.assertRaises(StaleLease):
            svc.begin_step(task_id, "s1", "w-old", stale)
        with self.assertRaises(StaleLease):
            svc.succeed(task_id, "s1", stale, {"late": True})

        # The current holder can proceed.
        svc.begin_step(task_id, "s1", "w-new", fresh)
        svc.succeed(task_id, "s1", fresh, {"ok": True})
        self.assertEqual(svc.view(task_id).step_status("s1"), StepStatus.SUCCEEDED)

    def test_second_worker_cannot_steal_live_lease(self):
        svc = build_service()
        task_id = svc.create_task("t")
        svc.plan(task_id, STEPS)
        svc.start(task_id)
        self.assertIsNotNone(svc.lease_step(task_id, "s1", "w1", ttl_ms=30_000))
        self.assertIsNone(svc.lease_step(task_id, "s1", "w2", ttl_ms=30_000))


class TestIdempotencyAndUnknown(unittest.TestCase):
    def test_replay_returns_cached_result(self):
        svc = build_service()
        task_id = svc.create_task("t")
        svc.plan(task_id, STEPS)
        svc.start(task_id)
        fence = svc.lease_step(task_id, "s1", "w1")
        svc.begin_step(task_id, "s1", "w1", fence)

        calls = {"n": 0}

        def executor():
            calls["n"] += 1
            return {"ticket": "T-1"}

        r1 = svc.invoke_tool(task_id, "s1", fence, "ticket.create", {"title": "x"}, executor)
        r2 = svc.invoke_tool(task_id, "s1", fence, "ticket.create", {"title": "x"}, executor)

        self.assertEqual(r1, r2)
        self.assertEqual(calls["n"], 1, "side effect must run exactly once")

    def test_crash_between_intent_and_result_becomes_unknown(self):
        svc = build_service()
        task_id = svc.create_task("t")
        svc.plan(task_id, STEPS)
        svc.start(task_id)
        fence = svc.lease_step(task_id, "s1", "w1")
        svc.begin_step(task_id, "s1", "w1", fence)

        class Crash(RuntimeError):
            pass

        def exploding_executor():
            raise Crash("worker died after calling the ticketing API")

        with self.assertRaises(Crash):
            svc.invoke_tool(task_id, "s1", fence, "ticket.create", {"title": "x"},
                            exploding_executor)

        dangling = svc.find_dangling_invocations(task_id)
        self.assertEqual(len(dangling), 1, "journal must show INTENT without RESULT")

        unknown_steps = svc.recover(task_id)
        self.assertEqual(unknown_steps, ["s1"])
        self.assertEqual(svc.view(task_id).step_status("s1"), StepStatus.UNKNOWN)

        # A blind retry is refused; the outcome is unknowable.
        with self.assertRaises(NeedsHumanDecision):
            svc.invoke_tool(task_id, "s1", fence, "ticket.create", {"title": "x"},
                            exploding_executor)

    def test_recovery_does_not_flag_completed_invocations(self):
        svc = build_service()
        task_id = svc.create_task("t")
        svc.plan(task_id, STEPS)
        svc.start(task_id)
        fence = svc.lease_step(task_id, "s1", "w1")
        svc.begin_step(task_id, "s1", "w1", fence)
        svc.invoke_tool(task_id, "s1", fence, "loki.query", {"q": "*"}, lambda: {"rows": 3})
        self.assertEqual(svc.find_dangling_invocations(task_id), [])
        self.assertEqual(svc.recover(task_id), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestTaskCreateIdempotency(unittest.TestCase):
    def test_same_key_returns_same_task(self):
        svc = build_service()
        first = svc.create_task_idempotent("req-1", "title", tenant_id="team-a")
        second = svc.create_task_idempotent("req-1", "title", tenant_id="team-a")
        other_tenant = svc.create_task_idempotent("req-1", "title", tenant_id="team-b")
        self.assertEqual(first, second)
        self.assertNotEqual(first, other_tenant)
        self.assertTrue(svc.view(first).tenant_id == "team-a")
