"""Domain event definitions.

Every state change in the system is an immutable event. Current state is never
written directly; it is always derived by replaying the journal.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class EventType(str, Enum):
    # --- task lifecycle ---
    TASK_CREATED = "TASK_CREATED"
    TASK_PLANNED = "TASK_PLANNED"
    TASK_STARTED = "TASK_STARTED"
    TASK_WAITING_HUMAN = "TASK_WAITING_HUMAN"
    TASK_RESUMED = "TASK_RESUMED"
    TASK_REVIEWING = "TASK_REVIEWING"
    TASK_DELIVERED = "TASK_DELIVERED"
    TASK_FAILED = "TASK_FAILED"
    TASK_CANCELLED = "TASK_CANCELLED"

    # --- step lifecycle ---
    STEP_QUEUED = "STEP_QUEUED"
    STEP_LEASED = "STEP_LEASED"
    STEP_STARTED = "STEP_STARTED"
    STEP_SUCCEEDED = "STEP_SUCCEEDED"
    STEP_FAILED = "STEP_FAILED"
    STEP_TIMEOUT = "STEP_TIMEOUT"
    STEP_UNKNOWN = "STEP_UNKNOWN"
    STEP_SKIPPED = "STEP_SKIPPED"

    # --- tool invocation (two-phase journal) ---
    INVOCATION_INTENT = "INVOCATION_INTENT"
    INVOCATION_RESULT = "INVOCATION_RESULT"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class Event:
    """An immutable fact in the append-only journal."""

    task_id: str
    seq: int
    type: EventType
    payload: dict[str, Any]
    actor: str
    created_at: str

    def to_row(self) -> tuple:
        return (
            self.task_id,
            self.seq,
            self.type.value,
            json.dumps(self.payload, ensure_ascii=False, sort_keys=True),
            self.actor,
            self.created_at,
        )

    @classmethod
    def from_row(cls, row: tuple) -> "Event":
        task_id, seq, etype, payload, actor, created_at = row
        return cls(
            task_id=task_id,
            seq=int(seq),
            type=EventType(etype),
            payload=json.loads(payload),
            actor=actor,
            created_at=created_at,
        )


def canonical_json(value: Any) -> str:
    """Deterministic serialisation used for digests and idempotency keys."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def idempotency_key(
    task_id: str,
    step_id: str,
    attempt_scope: str,
    tool: str,
    args: dict[str, Any],
) -> str:
    """Stable key for one logical tool attempt.

    ``attempt_scope`` is *not* the retry counter: retries inside the same
    logical attempt share a key (so they dedupe), while a re-plan produces a
    new scope (so it is allowed to execute again).
    """
    return sha256_hex(
        "|".join([task_id, step_id, attempt_scope, tool, canonical_json(args)])
    )
