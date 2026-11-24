"""Saga compensation registry.

Any mutating tool call must register its compensation *before* it executes, so a
crash leaves a pending compensation rather than an untracked side effect. The
registry is persisted in the same SQLite database as the event journal, which
keeps the "what did we do and what must we undo" story in one place.
"""
from __future__ import annotations

from .db import is_mysql

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class CompensationStatus(str, Enum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


SCHEMA = """
CREATE TABLE IF NOT EXISTS compensations (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    step_id     TEXT NOT NULL,
    action      TEXT NOT NULL,       -- e.g. ticket.close
    payload     TEXT NOT NULL,       -- JSON args for the compensating call
    status      TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comp_task ON compensations(task_id, status);
CREATE INDEX IF NOT EXISTS idx_comp_step ON compensations(step_id, status);
"""


@dataclass
class Compensation:
    id: str
    task_id: str
    step_id: str
    action: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: CompensationStatus = CompensationStatus.PENDING
    attempts: int = 0
    last_error: str = ""


class SagaRegistry:
    """Persistent register of "what must be undone"."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn = conn
        self._lock = lock
        if not is_mysql(self._conn):
            self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------ write
    def register(
        self, task_id: str, step_id: str, action: str, payload: dict[str, Any]
    ) -> Compensation:
        comp = Compensation(
            id=str(uuid.uuid4()),
            task_id=task_id,
            step_id=step_id,
            action=action,
            payload=payload,
        )
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO compensations "
                "(id, task_id, step_id, action, payload, status, attempts, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (comp.id, task_id, step_id, action,
                 json.dumps(payload, ensure_ascii=False), comp.status.value, now, now),
            )
            self._conn.commit()
        return comp

    def mark(self, comp_id: str, status: CompensationStatus, error: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE compensations SET status = ?, attempts = attempts + 1, "
                "last_error = ?, updated_at = ? WHERE id = ?",
                (status.value, error[:500], time.time(), comp_id),
            )
            self._conn.commit()

    # ------------------------------------------------------------------- read
    def pending(self, task_id: str | None = None) -> list[Compensation]:
        """Compensations that still need action.

        FAILED is included on purpose: an unreverted side effect stays visible
        and gets retried on the next sweep instead of disappearing.
        """
        sql = ("SELECT id, task_id, step_id, action, payload, status, attempts, "
               "COALESCE(last_error, '') FROM compensations WHERE status IN (?, ?)")
        params: list[Any] = [
            CompensationStatus.PENDING.value,
            CompensationStatus.FAILED.value,
        ]
        if task_id:
            sql += " AND task_id = ?"
            params.append(task_id)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            Compensation(
                id=r[0], task_id=r[1], step_id=r[2], action=r[3],
                payload=json.loads(r[4]), status=CompensationStatus(r[5]),
                attempts=r[6], last_error=r[7],
            )
            for r in rows
        ]

    def for_step(self, step_id: str) -> list[Compensation]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, task_id, step_id, action, payload, status, attempts, "
                "COALESCE(last_error, '') FROM compensations WHERE step_id = ?",
                (step_id,),
            ).fetchall()
        return [
            Compensation(
                id=r[0], task_id=r[1], step_id=r[2], action=r[3],
                payload=json.loads(r[4]), status=CompensationStatus(r[5]),
                attempts=r[6], last_error=r[7],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------ undo
    def compensate(
        self,
        executors: dict[str, Callable[[dict[str, Any]], Any]],
        task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Run every pending compensation, newest first.

        A failing compensation is retried on the next sweep rather than being
        dropped 鈥?an unreverted side effect must stay visible.
        """
        results: list[dict[str, Any]] = []
        for comp in sorted(self.pending(task_id), key=lambda c: c.id, reverse=True):
            executor = executors.get(comp.action)
            if executor is None:
                self.mark(comp.id, CompensationStatus.SKIPPED,
                          f"no executor registered for {comp.action}")
                results.append({"id": comp.id, "action": comp.action,
                                "status": "skipped", "error": "no executor"})
                continue
            try:
                executor(comp.payload)
            except Exception as exc:  # noqa: BLE001 - keep pending for retry
                self.mark(comp.id, CompensationStatus.FAILED,
                          f"{type(exc).__name__}: {exc}")
                results.append({"id": comp.id, "action": comp.action,
                                "status": "failed", "error": str(exc)[:200]})
            else:
                self.mark(comp.id, CompensationStatus.DONE)
                results.append({"id": comp.id, "action": comp.action, "status": "done"})
        return results
