"""Durable per-step output, so steps executed in different processes can share
evidence and claims.

In a single-process run the agents share an ``AgentContext`` in memory. Once
steps are dispatched to separate workers that shared memory is gone, so each
step must persist what it learned and the assembler must read it back.

The table is keyed by (task_id, step_id) and written exactly once per attempt;
a retried step overwrites its own row, which is safe because the step is
idempotent.
"""
from __future__ import annotations

from .db import is_mysql

import json
import sqlite3
import threading
from typing import Any

from .contracts import AgentOutput, AgentRole, Claim, Evidence, SourceKind, TimelineEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS step_output (
    task_id     TEXT NOT NULL,
    step_id     TEXT NOT NULL,
    role        TEXT NOT NULL,
    payload     TEXT NOT NULL,          -- AgentOutput JSON
    updated_at  REAL NOT NULL,
    PRIMARY KEY (task_id, step_id)
);
CREATE TABLE IF NOT EXISTS task_evidence (
    task_id     TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    payload     TEXT NOT NULL,          -- Evidence JSON
    PRIMARY KEY (task_id, evidence_id)
);
"""


class StepOutputStore:
    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn = conn
        self._lock = lock
        if not is_mysql(self._conn):
            self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---------------------------------------------------------------- evidence
    def save_evidence(self, task_id: str, evidence: dict[str, Evidence]) -> None:
        import time

        with self._lock:
            for item in evidence.values():
                self._conn.execute(
                    self._conn.upsert_sql(
                        "task_evidence", ["task_id", "evidence_id", "payload"]
                    ),
                    (task_id, item.evidence_id, item.model_dump_json()),
                )
            self._conn.commit()

    def load_evidence(self, task_id: str) -> dict[str, Evidence]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM task_evidence WHERE task_id = ?", (task_id,)
            ).fetchall()
        out: dict[str, Evidence] = {}
        for (payload,) in rows:
            item = Evidence.model_validate_json(payload)
            out[item.evidence_id] = item
        return out

    # ----------------------------------------------------------------- outputs
    def save_output(self, task_id: str, step_id: str, output: AgentOutput) -> None:
        import time

        with self._lock:
            self._conn.execute(
                self._conn.upsert_sql(
                    "step_output", ["task_id", "step_id", "role", "payload", "updated_at"]
                ),
                (task_id, step_id, output.role.value, output.model_dump_json(), time.time()),
            )
            self._conn.commit()

    def load_outputs(self, task_id: str) -> dict[str, AgentOutput]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT step_id, payload FROM step_output WHERE task_id = ?", (task_id,)
            ).fetchall()
        return {step_id: AgentOutput.model_validate_json(payload) for step_id, payload in rows}

    def merged_claims(self, task_id: str) -> list[Claim]:
        claims: list[Claim] = []
        for output in self.load_outputs(task_id).values():
            claims.extend(output.claims)
        return claims

    def merged_timeline(self, task_id: str) -> list[TimelineEvent]:
        events: list[TimelineEvent] = []
        for output in self.load_outputs(task_id).values():
            events.extend(output.timeline)
        return sorted(events, key=lambda e: e.at)
