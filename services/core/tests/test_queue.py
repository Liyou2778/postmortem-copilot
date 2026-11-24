"""M3 queue tests (SQLite implementation; the Redis one shares the contract)."""
from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.queue import SqliteQueue, WorkItem  # noqa: E402
from app.store import EventStore  # noqa: E402


def build(vt: float = 60.0) -> tuple[SqliteQueue, EventStore]:
    store = EventStore(":memory:")
    return SqliteQueue(store._conn, store._lock, visibility_timeout_s=vt), store


class TestSqliteQueue(unittest.TestCase):
    def test_enqueue_is_idempotent_per_step(self):
        q, _ = build()
        item = WorkItem(task_id="t1", step_id="s1")
        q.enqueue(item)
        q.enqueue(item)  # dispatch is retried by operators; must not duplicate
        self.assertEqual(q.stats()["pending"], 1)

    def test_reserve_then_ack(self):
        q, _ = build()
        q.enqueue(WorkItem(task_id="t1", step_id="s1"))
        got = q.reserve(timeout_ms=200)
        self.assertIsNotNone(got)
        self.assertEqual((got.task_id, got.step_id), ("t1", "s1"))
        self.assertEqual(q.stats()["inflight"], 1)
        q.ack(got)
        self.assertEqual(q.stats()["done"], 1)
        self.assertIsNone(q.reserve(timeout_ms=100), "已完成的条目不应再被取出")

    def test_nack_requeues_and_counts_attempts(self):
        q, _ = build()
        q.enqueue(WorkItem(task_id="t1", step_id="s1"))
        got = q.reserve(timeout_ms=200)
        q.nack(got)
        self.assertEqual(q.stats()["pending"], 1)
        again = q.reserve(timeout_ms=200)
        self.assertEqual(again.step_id, "s1")
        self.assertEqual(again.attempts, 1, "nack 必须累加尝试次数")

    def test_visibility_timeout_reclaims_abandoned_item(self):
        q, _ = build(vt=0.2)
        q.enqueue(WorkItem(task_id="t1", step_id="s1"))
        first = q.reserve(timeout_ms=200)
        self.assertIsNotNone(first)
        # simulate a dead consumer: never ack
        time.sleep(0.25)
        reclaimed = q.reserve(timeout_ms=300)
        self.assertIsNotNone(reclaimed, "超时条目必须被重新投递（至少一次语义）")
        self.assertEqual(reclaimed.step_id, "s1")

    def test_reserve_returns_none_when_empty(self):
        q, _ = build()
        self.assertIsNone(q.reserve(timeout_ms=100))

    def test_different_tasks_are_independent(self):
        q, _ = build()
        q.enqueue(WorkItem(task_id="t1", step_id="s1"))
        q.enqueue(WorkItem(task_id="t2", step_id="s1"))
        self.assertEqual(q.stats()["pending"], 2, "不同任务的同名步骤不得互相覆盖")
        first = q.reserve(timeout_ms=200)
        second = q.reserve(timeout_ms=200)
        self.assertNotEqual(first.task_id, second.task_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
