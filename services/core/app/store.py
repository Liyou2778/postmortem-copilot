"""Append-only event store.

The class keeps its original connection-shaped surface (``store._conn`` is the
database handle other modules share) but that handle is now a :class:`Database`,
which translates ``?`` placeholders and upserts for whichever backend is
configured. Domain code therefore contains no dialect branches.
"""
from __future__ import annotations

import threading
from typing import Any

from .db import MYSQL, SQLITE, Database, ensure_schema
from .events import Event, EventType, utcnow

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT    NOT NULL,
    seq        INTEGER NOT NULL,
    type       TEXT    NOT NULL,
    payload    TEXT    NOT NULL,
    actor      TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    UNIQUE (task_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, seq);

CREATE TABLE IF NOT EXISTS idempotency (
    key        TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL,
    step_id    TEXT NOT NULL,
    status     TEXT NOT NULL,          -- INTENT | DONE | UNKNOWN
    result     TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_create_idempotency (
    key        TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

MYSQL_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         BIGINT       NOT NULL AUTO_INCREMENT,
    task_id    VARCHAR(64)  NOT NULL,
    seq        INT          NOT NULL,
    type       VARCHAR(64)  NOT NULL,
    payload    MEDIUMTEXT   NOT NULL,
    actor      VARCHAR(128) NOT NULL,
    created_at VARCHAR(40)  NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_events_task_seq (task_id, seq),
    KEY idx_events_task (task_id, seq)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS idempotency (
    `key`      VARCHAR(191) NOT NULL,
    task_id    VARCHAR(64)  NOT NULL,
    step_id    VARCHAR(64)  NOT NULL,
    status     VARCHAR(16)  NOT NULL,
    result     MEDIUMTEXT   NULL,
    created_at VARCHAR(40)  NOT NULL,
    PRIMARY KEY (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""


class ConcurrencyError(RuntimeError):
    """Raised when two writers try to append the same sequence number."""


class EventStore:
    def __init__(self, path: str = ":memory:", db: Database | None = None) -> None:
        self._db = db if db is not None else Database.sqlite(path)
        # Shared handle: lease/saga/report/archive/step modules borrow these.
        self._conn = self._db
        self._lock = self._db.lock
        schema = MYSQL_SCHEMA if self._db.backend == MYSQL else SQLITE_SCHEMA
        self._db.executescript(schema)
        if self._db.backend == MYSQL:
            # Per-module DDL is SQLite-shaped; on MySQL one authoritative script
            # creates every table once, and the modules skip theirs.
            ensure_schema(self._db)

    def close(self) -> None:
        self._db.close()

    def find_task_create(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT task_id FROM task_create_idempotency WHERE `key` = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def remember_task_create(self, key: str, task_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO task_create_idempotency (`key`, task_id, created_at) "
                "VALUES (?, ?, ?)",
                (key, task_id, utcnow()),
            )

    # ------------------------------------------------------------------ append
    def append(
        self,
        task_id: str,
        event_type: EventType,
        payload: dict[str, Any],
        actor: str,
    ) -> Event:
        """Append the next event, allocating ``seq`` under a lock."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM events WHERE task_id = ?", (task_id,)
            )
            next_seq = int(cur.fetchone()[0]) + 1
            event = Event(
                task_id=task_id,
                seq=next_seq,
                type=event_type,
                payload=payload,
                actor=actor,
                created_at=utcnow(),
            )
            try:
                self._conn.execute(
                    "INSERT INTO events (task_id, seq, type, payload, actor, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    event.to_row(),
                )
                self._conn.commit()
            except self._db.integrity_errors() as exc:  # pragma: no cover - concurrency guard
                self._conn.rollback()
                raise ConcurrencyError(
                    f"seq {next_seq} already exists for task {task_id}"
                ) from exc
            return event

    # -------------------------------------------------------------------- load
    def load(self, task_id: str) -> list[Event]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT task_id, seq, type, payload, actor, created_at "
                "FROM events WHERE task_id = ? ORDER BY seq ASC",
                (task_id,),
            ).fetchall()
        return [Event.from_row(row) for row in rows]

    def load_after(self, task_id: str, seq: int) -> list[Event]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT task_id, seq, type, payload, actor, created_at "
                "FROM events WHERE task_id = ? AND seq > ? ORDER BY seq ASC",
                (task_id, seq),
            ).fetchall()
        return [Event.from_row(row) for row in rows]

    def list_tasks(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT task_id FROM events ORDER BY task_id"
            ).fetchall()
        return [row[0] for row in rows]

    def list_tasks_recent(self, limit: int = 50) -> list[str]:
        """Task ids newest first, by first event timestamp.

        ``list_tasks`` sorts by id, which is stable but meaningless to a user
        looking at "recent tasks" — a UUID has no chronological order.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT task_id, MIN(created_at) AS first_at FROM events "
                "GROUP BY task_id ORDER BY first_at DESC, task_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [row[0] for row in rows]

    # ------------------------------------------------------------- idempotency
    def claim_intent(
        self, key: str, task_id: str, step_id: str
    ) -> tuple[bool, str | None]:
        """Reserve a logical attempt.

        Returns ``(claimed, previous_status)``. When ``claimed`` is False the
        caller must inspect ``previous_status``:
        * ``DONE``    -> the side effect already happened, reuse the result
        * ``UNKNOWN`` -> outcome unknowable, escalate to a human
        * ``INTENT``  -> still in flight (or crashed before result was written)
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT status, result FROM idempotency WHERE key = ?", (key,)
            ).fetchone()
            if row:
                return False, row[0]
            self._conn.execute(
                "INSERT INTO idempotency (key, task_id, step_id, status, created_at) "
                "VALUES (?, ?, ?, 'INTENT', ?)",
                (key, task_id, step_id, utcnow()),
            )
            self._conn.commit()
            return True, None

    def finish_intent(self, key: str, status: str, result: Any = None) -> None:
        import json

        with self._lock:
            self._conn.execute(
                "UPDATE idempotency SET status = ?, result = ? WHERE key = ?",
                (status, json.dumps(result, ensure_ascii=False), key),
            )
            self._conn.commit()

    def get_intent(self, key: str) -> tuple[str, Any] | None:
        import json

        with self._lock:
            row = self._conn.execute(
                "SELECT status, result FROM idempotency WHERE key = ?", (key,)
            ).fetchone()
        if not row:
            return None
        return row[0], (json.loads(row[1]) if row[1] else None)
