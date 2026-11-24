"""Work queue abstraction with two durable implementations.

Why two: the SQLite queue keeps tests and local development dependency-free,
while the Redis Stream queue is what you actually run in production. Both
implement the same contract, so the worker does not know which one it is
talking to.

Delivery semantics: **at-least-once**. A reserved item that is not acked within
the visibility timeout is redelivered. That is why every step handler must be
idempotent — and it already is, through the two-phase invocation journal.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Protocol


@dataclass
class WorkItem:
    task_id: str
    step_id: str
    capability_tag: str = ""
    attempts: int = 0
    item_id: str = ""

    def to_payload(self) -> dict[str, str]:
        return {
            "task_id": self.task_id,
            "step_id": self.step_id,
            "capability_tag": self.capability_tag,
            "attempts": str(self.attempts),
        }

    @classmethod
    def from_payload(cls, item_id: str, payload: dict[str, Any]) -> "WorkItem":
        return cls(
            task_id=str(payload.get("task_id", "")),
            step_id=str(payload.get("step_id", "")),
            capability_tag=str(payload.get("capability_tag", "")),
            attempts=int(payload.get("attempts", 0) or 0),
            item_id=item_id,
        )


class WorkQueue(Protocol):
    def enqueue(self, item: WorkItem) -> str: ...
    def reserve(self, timeout_ms: int = 1000) -> WorkItem | None: ...
    def ack(self, item: WorkItem) -> None: ...
    def nack(self, item: WorkItem) -> None: ...
    def stats(self) -> dict[str, int]: ...


# --------------------------------------------------------------------- sqlite
_SQL_SCHEMA = """
CREATE TABLE IF NOT EXISTS work_queue (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    step_id     TEXT NOT NULL,
    tag         TEXT NOT NULL DEFAULT '',
    attempts    INTEGER NOT NULL DEFAULT 0,
    state       TEXT NOT NULL DEFAULT 'pending',   -- pending | inflight | done
    reserved_at REAL,
    created_at  REAL NOT NULL,
    UNIQUE (task_id, step_id)
);
CREATE INDEX IF NOT EXISTS idx_wq_state ON work_queue(state, created_at);
"""


class SqliteQueue:
    """Durable queue on the same SQLite file as the journal."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock,
                 visibility_timeout_s: float = 60.0) -> None:
        self._conn = conn
        self._lock = lock
        self._vt = visibility_timeout_s
        self._conn.executescript(_SQL_SCHEMA)
        self._conn.commit()

    def enqueue(self, item: WorkItem) -> str:
        item_id = item.item_id or str(uuid.uuid4())
        with self._lock:
            # Idempotent enqueue: a step already queued is not queued twice.
            # Dialect difference handled by the handle (INSERT OR IGNORE vs INSERT IGNORE).
            self._conn.execute(
                self._conn.insert_ignore_sql(
                    "work_queue",
                    ["id", "task_id", "step_id", "tag", "attempts", "state", "created_at"],
                ),
                (item_id, item.task_id, item.step_id, item.capability_tag,
                 item.attempts, "pending", time.time()),
            )
            self._conn.commit()
        return item_id

    def _reclaim_expired(self) -> None:
        cutoff = time.time() - self._vt
        self._conn.execute(
            "UPDATE work_queue SET state='pending', reserved_at=NULL "
            "WHERE state='inflight' AND reserved_at < ?",
            (cutoff,),
        )
        self._conn.commit()

    def reserve(self, timeout_ms: int = 1000) -> WorkItem | None:
        deadline = time.time() + max(0, timeout_ms) / 1000.0
        while True:
            with self._lock:
                self._reclaim_expired()
                row = self._conn.execute(
                    "SELECT id, task_id, step_id, tag, attempts FROM work_queue "
                    "WHERE state='pending' ORDER BY created_at LIMIT 1"
                ).fetchone()
                if row:
                    self._conn.execute(
                        "UPDATE work_queue SET state='inflight', reserved_at=? WHERE id=?",
                        (time.time(), row[0]),
                    )
                    self._conn.commit()
                    return WorkItem(task_id=row[1], step_id=row[2],
                                    capability_tag=row[3], attempts=row[4], item_id=row[0])
            if time.time() >= deadline:
                return None
            time.sleep(0.02)

    def ack(self, item: WorkItem) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE work_queue SET state='done' WHERE task_id=? AND step_id=?",
                (item.task_id, item.step_id),
            )
            self._conn.commit()

    def nack(self, item: WorkItem) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE work_queue SET state='pending', reserved_at=NULL, attempts=attempts+1 "
                "WHERE task_id=? AND step_id=?",
                (item.task_id, item.step_id),
            )
            self._conn.commit()

    def stats(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, COUNT(*) FROM work_queue GROUP BY state"
            ).fetchall()
        out = {"pending": 0, "inflight": 0, "done": 0}
        for state, count in rows:
            out[str(state)] = int(count)
        return out


# ---------------------------------------------------------------------- redis
class RedisStreamQueue:
    """Redis Streams with a consumer group.

    ``XACK`` + ``XDEL`` on success; ``XAUTOCLAIM`` reclaims entries stuck in a
    dead consumer's PEL (pending entries list), which is the distributed
    equivalent of the SQLite visibility timeout.
    """

    def __init__(self, url: str, stream: str = "copilot:steps",
                 group: str = "workers", consumer: str | None = None,
                 visibility_timeout_s: int = 60) -> None:
        import redis  # imported lazily so the SQLite path needs no dependency

        self._r = redis.Redis.from_url(url, decode_responses=True)
        self._stream = stream
        self._group = group
        self._consumer = consumer or f"worker-{uuid.uuid4().hex[:8]}"
        self._vt_ms = visibility_timeout_s * 1000
        self._ensure_group()

    def _ensure_group(self) -> None:
        try:
            self._r.xgroup_create(self._stream, self._group, id="0", mkstream=True)
        except Exception as exc:  # noqa: BLE001 - BUSYGROUP means it already exists
            if "BUSYGROUP" not in str(exc):
                raise

    def enqueue(self, item: WorkItem) -> str:
        return str(self._r.xadd(self._stream, item.to_payload()))

    def reserve(self, timeout_ms: int = 1000) -> WorkItem | None:
        # First try to reclaim entries abandoned by a dead consumer.
        try:
            claimed = self._r.xautoclaim(
                self._stream, self._group, self._consumer,
                min_idle_time=self._vt_ms, start_id="0-0", count=1,
            )
            if claimed and claimed[1]:
                entry_id, fields = claimed[1][0]
                return WorkItem.from_payload(entry_id, fields)
        except Exception:  # noqa: BLE001 - older redis without XAUTOCLAIM
            pass

        resp = self._r.xreadgroup(
            self._group, self._consumer, {self._stream: ">"},
            count=1, block=timeout_ms,
        )
        if not resp:
            return None
        _stream, entries = resp[0]
        if not entries:
            return None
        entry_id, fields = entries[0]
        return WorkItem.from_payload(entry_id, fields)

    def ack(self, item: WorkItem) -> None:
        self._r.xack(self._stream, self._group, item.item_id)
        self._r.xdel(self._stream, item.item_id)

    def nack(self, item: WorkItem) -> None:
        self._r.xack(self._stream, self._group, item.item_id)
        self._r.xdel(self._stream, item.item_id)
        payload = item.to_payload()
        payload["attempts"] = str(item.attempts + 1)
        self._r.xadd(self._stream, payload)

    def stats(self) -> dict[str, int]:
        try:
            pending = self._r.xpending(self._stream, self._group)
            pending_count = int(pending.get("pending", 0)) if isinstance(pending, dict) else 0
        except Exception:  # noqa: BLE001
            pending_count = 0
        return {
            "pending": int(self._r.xlen(self._stream)) - pending_count,
            "inflight": pending_count,
            "done": 0,
        }


# --------------------------------------------------------------------- factory
def build_queue(backend: str, *, conn: sqlite3.Connection | None = None,
                lock: threading.RLock | None = None, redis_url: str = "") -> WorkQueue:
    backend = (backend or "sqlite").strip().lower()
    if backend == "redis":
        if not redis_url:
            raise ValueError("redis backend requires a redis_url")
        return RedisStreamQueue(redis_url)
    if conn is None or lock is None:
        raise ValueError("sqlite backend requires a connection and lock")
    return SqliteQueue(conn, lock)
