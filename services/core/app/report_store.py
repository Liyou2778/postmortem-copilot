"""Report persistence.

The orchestrator produces a Markdown report in memory; without persisting it the
only way to see a report is to re-run the whole analysis. Storing the rendered
Markdown makes the report addressable by URL, which is what turns the service
from "an API" into "something a person can open".
"""
from __future__ import annotations

from .db import is_mysql

import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS report (
    task_id    TEXT PRIMARY KEY,
    markdown   TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


class ReportStore:
    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn = conn
        self._lock = lock
        if not is_mysql(self._conn):
            self._conn.executescript(SCHEMA)
        self._conn.commit()

    def save(self, task_id: str, markdown: str) -> None:
        with self._lock:
            self._conn.execute(
                self._conn.upsert_sql("report", ["task_id", "markdown", "created_at"]),
                (task_id, markdown, time.time()),
            )
            self._conn.commit()

    def load(self, task_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT markdown FROM report WHERE task_id = ?", (task_id,)
            ).fetchone()
        return row[0] if row else None

    def exists(self, task_id: str) -> bool:
        return self.load(task_id) is not None
