"""M3 tests: saga compensation, lease reaper, UNKNOWN adjudication.

Run: python -m unittest discover -s services/core/tests -v
"""
from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.lease import LeaseManager  # noqa: E402
from app.reaper import LeaseReaper  # noqa: E402
from app.saga import CompensationStatus, SagaRegistry  # noqa: E402
from app.service import IllegalTransition, NeedsHumanDecision, StepStatus, TaskService  # noqa: E402
from app.statemachine import TaskStatus  # noqa: E402
from app.store import EventStore  # noqa: E402

STEPS = [
    {"step_id": "s1", "role": "telemetry"},
    {"step_id": "s2", "role": "change"},
]


def build() -> tuple[TaskService, LeaseManager, SagaRegistry, LeaseReaper, str]:
    store = EventStore(":memory:")
    leases = LeaseManager(store._conn, store._lock)
    saga = SagaRegistry(store._conn, store._lock)
    service = TaskService(store, leases, saga)
    reaper = LeaseReaper(service, leases)
    task_id = service.create_task("t")
    service.plan(task_id, STEPS)
    service.start(task_id)
    return service, leases, saga, reaper, task_id


class TestSaga(unittest.TestCase):
    def test_compensation_is_registered_and_executed(self):
        service, _, saga, _, task_id = build()
        comp_id = service.register_compensation(
            task_id, "s1", "ticket.close", {"service": "order", "title": "x"}
        )
        self.assertTrue(comp_id)
        self.assertEqual(len(saga.pending(task_id)), 1)

        called: list[dict] = []
        results = service.compensate(task_id, {"ticket.close": lambda p: called.append(p)})

        self.assertEqual(len(called), 1, "补偿动作必须真的被执行")
        self.assertEqual(results[0]["status"], "done")
        self.assertEqual(saga.pending(task_id), [], "补偿完成后不应再 pending")

    def test_failed_compensation_stays_pending_for_retry(self):
        service, _, saga, _, task_id = build()
        service.register_compensation(task_id, "s1", "ticket.close", {"id": "T-1"})

        def boom(_payload):
            raise RuntimeError("ticketing API down")

        results = service.compensate(task_id, {"ticket.close": boom})
        self.assertEqual(results[0]["status"], "failed")
        pending = saga.pending(task_id)
        self.assertEqual(len(pending), 1, "失败的补偿必须保留，不能静默丢弃")
        self.assertEqual(pending[0].status, CompensationStatus.FAILED)
        self.assertIn("ticketing API down", pending[0].last_error)

    def test_unknown_action_is_skipped_not_faked(self):
        service, _, _, _, task_id = build()
        service.register_compensation(task_id, "s1", "ghost.action", {})
        results = service.compensate(task_id, {})
        self.assertEqual(results[0]["status"], "skipped")

    def test_write_requires_saga_registry(self):
        store = EventStore(":memory:")
        leases = LeaseManager(store._conn, store._lock)
        service = TaskService(store, leases)  # no saga
        task_id = service.create_task("t")
        with self.assertRaises(RuntimeError):
            service.register_compensation(task_id, "s1", "ticket.close", {})


class TestReaper(unittest.TestCase):
    def test_expired_lease_without_invocation_is_requeued(self):
        service, leases, _, reaper, task_id = build()
        fence = service.lease_step(task_id, "s1", owner="w1", ttl_ms=500)
        service.begin_step(task_id, "s1", "w1", fence)
        time.sleep(0.6)  # 500ms TTL; generous margin so a loaded CI box is not flaky  # let the lease lapse

        results = reaper.reap()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].action, "requeued")
        self.assertEqual(service.view(task_id).step_status("s1"), StepStatus.TIMEOUT)

        # the step must be leaseable again by a fresh worker
        new_fence = service.lease_step(task_id, "s1", owner="w2", ttl_ms=30_000)
        self.assertIsNotNone(new_fence)
        self.assertGreater(new_fence, fence, "围栏必须递增")

    def test_dangling_invocation_escalates_to_unknown(self):
        service, leases, _, reaper, task_id = build()
        fence = service.lease_step(task_id, "s1", owner="w1", ttl_ms=500)
        service.begin_step(task_id, "s1", "w1", fence)

        class Crash(RuntimeError):
            pass

        with self.assertRaises(Crash):
            service.invoke_tool(
                task_id, "s1", fence, "ticket.create", {"title": "x"},
                lambda: (_ for _ in ()).throw(Crash("dead")),
            )

        time.sleep(0.6)  # 500ms TTL; generous margin so a loaded CI box is not flaky
        results = reaper.reap()
        self.assertEqual(results[0].action, "unknown")
        self.assertEqual(service.view(task_id).step_status("s1"), StepStatus.UNKNOWN)

    def test_terminal_step_only_has_its_lease_released(self):
        service, leases, _, reaper, task_id = build()
        fence = service.lease_step(task_id, "s1", owner="w1", ttl_ms=500)
        service.begin_step(task_id, "s1", "w1", fence)
        service.succeed(task_id, "s1", fence)
        time.sleep(0.6)  # 500ms TTL; generous margin so a loaded CI box is not flaky

        results = reaper.reap()
        self.assertEqual(results[0].action, "released")
        self.assertEqual(service.view(task_id).step_status("s1"), StepStatus.SUCCEEDED)

    def test_reaper_is_idempotent(self):
        service, _, _, reaper, task_id = build()
        fence = service.lease_step(task_id, "s1", owner="w1", ttl_ms=500)
        service.begin_step(task_id, "s1", "w1", fence)
        time.sleep(0.6)  # 500ms TTL; generous margin so a loaded CI box is not flaky

        first = reaper.reap()
        second = reaper.reap()
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [], "第二次回收不应重复处理")


class TestAdjudication(unittest.TestCase):
    def _make_unknown(self, task_id, service, leases):
        fence = service.lease_step(task_id, "s1", owner="w1", ttl_ms=500)
        service.begin_step(task_id, "s1", "w1", fence)

        class Crash(RuntimeError):
            pass

        with self.assertRaises(Crash):
            service.invoke_tool(
                task_id, "s1", fence, "ticket.create", {"title": "x"},
                lambda: (_ for _ in ()).throw(Crash("dead")),
            )
        service.recover(task_id)
        return fence

    def test_adjudicate_confirm_failure_marks_failed(self):
        service, leases, _, _, task_id = build()
        self._make_unknown(task_id, service, leases)
        self.assertEqual(service.view(task_id).step_status("s1"), StepStatus.UNKNOWN)

        service.adjudicate(task_id, "s1", "confirm_failure", actor="sre-lead",
                           note="确认未创建工单")
        self.assertEqual(service.view(task_id).step_status("s1"), StepStatus.FAILED)

    def test_adjudicate_confirm_success_marks_succeeded(self):
        service, leases, _, _, task_id = build()
        self._make_unknown(task_id, service, leases)
        service.adjudicate(task_id, "s1", "confirm_success", actor="sre-lead")
        self.assertEqual(service.view(task_id).step_status("s1"), StepStatus.SUCCEEDED)

    def test_adjudicate_rejects_non_unknown_step(self):
        service, _, _, _, task_id = build()
        with self.assertRaises(IllegalTransition):
            service.adjudicate(task_id, "s1", "confirm_success", actor="x")

    def test_adjudicate_rejects_unknown_decision(self):
        service, leases, _, _, task_id = build()
        self._make_unknown(task_id, service, leases)
        with self.assertRaises(ValueError):
            service.adjudicate(task_id, "s1", "do_something_else", actor="x")

    def test_unknown_step_blocks_a_blind_retry(self):
        service, leases, _, _, task_id = build()
        fence = self._make_unknown(task_id, service, leases)
        with self.assertRaises(NeedsHumanDecision):
            service.invoke_tool(
                task_id, "s1", fence, "ticket.create", {"title": "x"}, lambda: "again"
            )


class TestLeaseScoping(unittest.TestCase):
    """Regression: leases used to be keyed by step_id alone.

    Every task has steps named telemetry/log/change, so two concurrent analyses
    shared one lease row and invalidated each other's fence. In production this
    surfaced as `StaleLease: presented 6, current 7` → HTTP 500.
    """

    def test_two_tasks_with_same_step_names_hold_independent_leases(self):
        service, _, _, _, task_a = build()
        task_b = service.create_task("第二个任务")
        service.plan(task_b, STEPS)
        service.start(task_b)

        fence_a = service.lease_step(task_a, "s1", owner="worker-A", ttl_ms=30_000)
        fence_b = service.lease_step(task_b, "s1", owner="worker-B", ttl_ms=30_000)
        self.assertIsNotNone(fence_b, "第二个任务不应被第一个任务的租约挡住")
        self.assertEqual(fence_a, 1)
        self.assertEqual(fence_b, 1, "两个任务的围栏计数必须各自独立")

        service.begin_step(task_a, "s1", "worker-A", fence_a)
        service.begin_step(task_b, "s1", "worker-B", fence_b)

        # the core assertion: finishing A must not invalidate B's fence
        service.succeed(task_a, "s1", fence_a, {"ok": True})
        service.succeed(task_b, "s1", fence_b, {"ok": True})

        self.assertEqual(service.view(task_a).step_status("s1"), StepStatus.SUCCEEDED)
        self.assertEqual(service.view(task_b).step_status("s1"), StepStatus.SUCCEEDED)

    def test_same_step_in_two_tasks_has_separate_fence_counters(self):
        service, _, _, _, task_a = build()
        task_b = service.create_task("t2")
        service.plan(task_b, STEPS)
        service.start(task_b)

        fa = service.lease_step(task_a, "s1", owner="A", ttl_ms=30_000)
        fb = service.lease_step(task_b, "s1", owner="B", ttl_ms=30_000)
        self.assertEqual(service._leases.current_fence(task_a, "s1"), fa)  # noqa: SLF001
        self.assertEqual(service._leases.current_fence(task_b, "s1"), fb)  # noqa: SLF001


class TestApprovalDelivery(unittest.TestCase):
    """Regression: real-model runs stop at REVIEWING and could not be delivered.

    The approval endpoint used to call ``submit_for_review`` unconditionally, so a
    task already in REVIEWING hit an illegal transition (HTTP 409) and there was
    no way to deliver a real analysis. Found during acceptance testing.
    """

    @staticmethod
    def _finish_all_steps(service, task_id: str) -> None:
        view = service.view(task_id)
        for step_id in view.steps:
            fence = service.lease_step(task_id, step_id, owner="w", ttl_ms=30_000)
            service.begin_step(task_id, step_id, "w", fence)
            service.succeed(task_id, step_id, fence, {"ok": True})

    def test_approve_from_reviewing_delivers(self):
        service, _, _, _, task_id = build()
        self._finish_all_steps(service, task_id)
        service.submit_for_review(task_id)          # -> REVIEWING (real-model shape)
        self.assertEqual(service.view(task_id).status, TaskStatus.REVIEWING)

        service.approve_delivery(task_id, approver="liyou")
        self.assertEqual(service.view(task_id).status, TaskStatus.DELIVERED)

    def test_approve_from_waiting_human_still_works(self):
        service, _, _, _, task_id = build()
        self._finish_all_steps(service, task_id)
        # mock-model shape: orchestrator parks the task for a human decision
        from app.events import EventType
        service._append(task_id, EventType.TASK_WAITING_HUMAN, {}, "reviewer")  # noqa: SLF001
        self.assertEqual(service.view(task_id).status, TaskStatus.WAITING_HUMAN)

        service.approve_delivery(task_id, approver="liyou")
        self.assertEqual(service.view(task_id).status, TaskStatus.DELIVERED)

    def test_delivered_is_terminal(self):
        service, _, _, _, task_id = build()
        self._finish_all_steps(service, task_id)
        service.submit_for_review(task_id)
        service.approve_delivery(task_id, approver="a")
        with self.assertRaises(IllegalTransition):
            service.approve_delivery(task_id, approver="a")


if __name__ == "__main__":
    unittest.main(verbosity=2)
