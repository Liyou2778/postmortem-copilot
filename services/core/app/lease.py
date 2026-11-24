"""Lease manager with fencing tokens.

A worker may only mutate a step while it holds a valid lease. Every acquisition
bumps a monotonically increasing ``fence``; writes carrying a stale fence are
rejected. This is what prevents a pause-then-resume worker from clobbering the
state that a replacement worker already advanced.

The lease is keyed by ``(task_id, step_id)``, not by ``step_id`` alone. Step ids
are only unique *within* a task 鈥?every task in this system has steps named
"telemetry", "log", "change". Keying on step_id alone made two concurrent
analyses share one lease row and invalidate each other's fence (observed in
production as ``StaleLease: presented 6, current 7`` 鈫?HTTP 500).
"""
from __future__ import annotations

from .db import is_mysql

import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS leases (
    task_id    TEXT NOT NULL,
    step_id    TEXT NOT NULL,
    owner      TEXT NOT NULL,
    fence      INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,   -- epoch milliseconds
    PRIMARY KEY (task_id, step_id)
);
"""

# Columns of the pre-fix schema: the primary key was step_id alone.
_LEGACY_PK = ("step_id",)


class StaleLease(RuntimeError):
    """Raised when a writer presents a fence that is no longer current."""


class LeaseManager:
    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn = conn
        self._lock = lock
        self._migrate()
        if not is_mysql(self._conn):
            self._conn.executescript(SCHEMA)
        self._conn.commit()

    def _migrate(self) -> None:
        """Drop a pre-fix leases table (SQLite only).

        Leases are transient coordination state, never business data, so
        recreating the table costs at most a re-acquisition on the next tick.
        MySQL has no sqlite_master/PRAGMA and starts from the current schema,
        so there is nothing to migrate there.
        """
        if is_mysql(self._conn):
            return
        row = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='leases'"
        ).fetchone()
        if row is None:
            return
        pk_cols = [
            info[1]
            for info in self._conn.execute("PRAGMA table_info(leases)").fetchall()
            if info[5] > 0
        ]
        if pk_cols == list(_LEGACY_PK):
            self._conn.execute("DROP TABLE leases")
            self._conn.commit()

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def acquire(self, task_id: str, step_id: str, owner: str, ttl_ms: int = 30_000) -> int | None:
        """Acquire or steal an expired lease. Returns the new fence, or None."""
        now = self._now_ms()
        with self._lock:
            row = self._conn.execute(
                "SELECT owner, fence, expires_at FROM leases "
                "WHERE task_id = ? AND step_id = ?",
                (task_id, step_id),
            ).fetchone()
            if row is None:
                fence = 1
                self._conn.execute(
                    "INSERT INTO leases (task_id, step_id, owner, fence, expires_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (task_id, step_id, owner, fence, now + ttl_ms),
                )
                self._conn.commit()
                return fence

            cur_owner, cur_fence, expires_at = row
            if expires_at > now and cur_owner != owner:
                return None  # someone else holds a live lease

            fence = int(cur_fence) + 1
            self._conn.execute(
                "UPDATE leases SET owner = ?, fence = ?, expires_at = ? "
                "WHERE task_id = ? AND step_id = ?",
                (owner, fence, now + ttl_ms, task_id, step_id),
            )
            self._conn.commit()
            return fence

    def renew(self, task_id: str, step_id: str, owner: str, fence: int,
              ttl_ms: int = 30_000) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE leases SET expires_at = ? "
                "WHERE task_id = ? AND step_id = ? AND owner = ? AND fence = ?",
                (self._now_ms() + ttl_ms, task_id, step_id, owner, fence),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def validate(self, task_id: str, step_id: str, fence: int) -> None:
        """Reject writes from a superseded worker."""
        with self._lock:
            row = self._conn.execute(
                "SELECT fence, expires_at FROM leases WHERE task_id = ? AND step_id = ?",
                (task_id, step_id),
            ).fetchone()
        if row is None:
            raise StaleLease(f"step {step_id} of task {task_id[:8]} has no lease")
        cur_fence, expires_at = row
        if int(cur_fence) != int(fence):
            raise StaleLease(
                f"stale fence for {task_id[:8]}/{step_id}: presented {fence}, current {cur_fence}"
            )
        if int(expires_at) <= self._now_ms():
            raise StaleLease(f"lease for {task_id[:8]}/{step_id} has expired")

    def expired_step_ids(self) -> list[tuple[str, str]]:
        """Return (task_id, step_id) pairs whose lease has lapsed."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT task_id, step_id FROM leases "
                "WHERE expires_at <= ? AND owner != ''",
                (self._now_ms(),),
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def expired_leases(self) -> list[tuple[str, str, str, int]]:
        """Return (task_id, step_id, owner, fence) for lapsed leases.

        The reaper needs owner/fence so it can release exactly the lease it
        observed, and so the takeover is attributable in the audit trail.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT task_id, step_id, owner, fence FROM leases "
                "WHERE expires_at <= ? AND owner != ''",
                (self._now_ms(),),
            ).fetchall()
        return [(r[0], r[1], r[2], int(r[3])) for r in rows]

    def force_release(self, task_id: str, step_id: str) -> None:
        """Free the slot but KEEP the row, so the fence keeps increasing.

        Deleting the row would reset the counter to 1 and let a stale worker
        present a fence that looks current. The row is parked with owner='' and
        expires_at=0 instead.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE leases SET owner = '', expires_at = 0 "
                "WHERE task_id = ? AND step_id = ?",
                (task_id, step_id),
            )
            self._conn.commit()

    def release(self, task_id: str, step_id: str, owner: str, fence: int) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM leases WHERE task_id = ? AND step_id = ? "
                "AND owner = ? AND fence = ?",
                (task_id, step_id, owner, fence),
            )
            self._conn.commit()

    def current_fence(self, task_id: str, step_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT fence FROM leases WHERE task_id = ? AND step_id = ?",
                (task_id, step_id),
            ).fetchone()
        return int(row[0]) if row else 0
