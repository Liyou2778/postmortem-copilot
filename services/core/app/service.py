"""Application service: the only place allowed to append events.

Rules enforced here:
1. No state is written directly; every mutation is an event.
2. A step may only be mutated while the caller holds a current fence.
3. A tool call is journalled in two phases (INTENT then RESULT). A crash between
   the two leaves an UNKNOWN outcome that is escalated to a human instead of
   being blindly retried.
"""
from __future__ import annotations

import uuid
from typing import Any, Callable

from .events import Event, EventType, canonical_json, idempotency_key, sha256_hex
from .lease import LeaseManager, StaleLease
from .saga import CompensationStatus, SagaRegistry
from .statemachine import (
    IllegalTransition,
    StepStatus,
    TaskStatus,
    TaskView,
    check_step_transition,
    check_task_transition,
    project,
)
from .store import EventStore


class NeedsHumanDecision(RuntimeError):
    """The side effect may or may not have happened; a human must decide."""


class TaskService:
    def __init__(
        self,
        store: EventStore,
        leases: LeaseManager,
        saga: SagaRegistry | None = None,
    ) -> None:
        self._store = store
        self._leases = leases
        # A saga registry is optional so the domain core can be tested without
        # the extra table, but any write tool path requires one at runtime.
        self._saga = saga

    # ------------------------------------------------------------------ helpers
    def view(self, task_id: str) -> TaskView:
        return project(self._store.load(task_id))

    def _append(self, task_id: str, etype: EventType, payload: dict, actor: str) -> Event:
        return self._store.append(task_id, etype, payload, actor)

    # ---------------------------------------------------------------- task flow
    def create_task(self, title: str, actor: str = "system",
                    tenant_id: str = "default") -> str:
        task_id = str(uuid.uuid4())
        self._append(
            task_id, EventType.TASK_CREATED,
            {"title": title, "tenant_id": tenant_id or "default"}, actor,
        )
        return task_id

    def create_task_idempotent(self, key: str, title: str, actor: str = "system",
                               tenant_id: str = "default") -> str:
        """Create once per (tenant, Idempotency-Key); replay returns the first id."""
        scoped = f"{tenant_id or 'default'}:{key}" if key else ""
        if scoped:
            existing = self._store.find_task_create(scoped)
            if existing:
                return existing
        task_id = self.create_task(title, actor, tenant_id=tenant_id)
        if scoped:
            try:
                self._store.remember_task_create(scoped, task_id)
            except Exception:
                # Concurrent duplicate: the unique key won; return its task.
                existing = self._store.find_task_create(scoped)
                if existing:
                    return existing
                raise
        return task_id

    def plan(self, task_id: str, steps: list[dict], actor: str = "orchestrator") -> TaskView:
        from .dag import validate_plan

        view = self.view(task_id)
        check_task_transition(view.status, EventType.TASK_PLANNED)
        validated = validate_plan(steps)
        clean = [
            {
                "step_id": step.step_id,
                "role": step.role,
                "capability_tag": step.capability_tag,
                "depends_on": list(step.depends_on),
                "side_effect": step.side_effect,
                "max_attempts": step.max_attempts,
            }
            for step in validated
        ]
        plan_version = view.plan_version + 1
        self._append(
            task_id,
            EventType.TASK_PLANNED,
            {"plan_version": plan_version, "steps": clean},
            actor,
        )
        return self.view(task_id)

    def skip_step(self, task_id: str, step_id: str, reason: str,
                  actor: str = "orchestrator") -> None:
        """Mark a pending step skipped when an upstream branch failed."""
        view = self.view(task_id)
        check_step_transition(view.step_status(step_id), EventType.STEP_SKIPPED)
        self._append(
            task_id, EventType.STEP_SKIPPED,
            {"step_id": step_id, "reason": reason[:300]}, actor,
        )

    def start(self, task_id: str, actor: str = "orchestrator") -> None:
        view = self.view(task_id)
        check_task_transition(view.status, EventType.TASK_STARTED)
        self._append(task_id, EventType.TASK_STARTED, {}, actor)

    def request_human(self, task_id: str, reason: str, actor: str = "orchestrator") -> None:
        view = self.view(task_id)
        check_task_transition(view.status, EventType.TASK_WAITING_HUMAN)
        self._append(task_id, EventType.TASK_WAITING_HUMAN, {"reason": reason}, actor)

    def resume(self, task_id: str, decision: str, actor: str) -> None:
        view = self.view(task_id)
        check_task_transition(view.status, EventType.TASK_RESUMED)
        self._append(task_id, EventType.TASK_RESUMED, {"decision": decision}, actor)

    def submit_for_review(self, task_id: str, actor: str = "orchestrator") -> None:
        view = self.view(task_id)
        if not view.all_steps_terminal():
            raise IllegalTransition(
                f"cannot review while steps are open: {view.open_steps()}"
            )
        check_task_transition(view.status, EventType.TASK_REVIEWING)
        self._append(task_id, EventType.TASK_REVIEWING, {}, actor)

    def deliver(self, task_id: str, artifact_uri: str, approver: str) -> None:
        view = self.view(task_id)
        check_task_transition(view.status, EventType.TASK_DELIVERED)
        self._append(
            task_id,
            EventType.TASK_DELIVERED,
            {"artifact_uri": artifact_uri, "approver": approver},
            approver,
        )

    def approve_delivery(self, task_id: str, approver: str,
                         artifact_uri: str | None = None) -> None:
        """Human approval that ends in delivery, from whichever state the run stopped in.

        Three entry states exist, and the old endpoint only handled one:

        * ``RUNNING`` + all steps terminal — mock-model run; submit for review, then deliver.
        * ``REVIEWING`` — real-model run already passed the review step; deliver directly.
        * ``WAITING_HUMAN`` — must RESUME first; the state machine has no
          WAITING_HUMAN -> REVIEWING edge, so submitting for review here is an
          illegal transition.

        Before this, approve returned HTTP 409 for both the real-model and the
        human-escalated cases, so those tasks could never be delivered.
        """
        view = self.view(task_id)
        if view.status == TaskStatus.WAITING_HUMAN:
            self.resume(task_id, "approve", approver)
            view = self.view(task_id)
        if view.status == TaskStatus.RUNNING:
            self.submit_for_review(task_id, approver)
        uri = artifact_uri or f"s3://reports/{task_id}.docx"
        self.deliver(task_id, uri, approver)

    # ---------------------------------------------------------------- step flow
    def lease_step(
        self, task_id: str, step_id: str, owner: str, ttl_ms: int = 30_000
    ) -> int | None:
        view = self.view(task_id)
        current = view.step_status(step_id)
        fence = self._leases.acquire(task_id, step_id, owner, ttl_ms)
        if fence is None:
            return None
        # A re-acquired lease on an already-leased step is an expiry takeover:
        # the journal records it so the takeover itself is auditable.
        check_step_transition(current, EventType.STEP_LEASED)
        self._append(
            task_id,
            EventType.STEP_LEASED,
            {"step_id": step_id, "owner": owner, "fence": fence},
            owner,
        )
        return fence

    def begin_step(
        self, task_id: str, step_id: str, owner: str, fence: int, attempt: int = 1
    ) -> None:
        self._leases.validate(task_id, step_id, fence)
        view = self.view(task_id)
        check_step_transition(view.step_status(step_id), EventType.STEP_STARTED)
        self._append(
            task_id,
            EventType.STEP_STARTED,
            {"step_id": step_id, "owner": owner, "fence": fence, "attempt": attempt},
            owner,
        )

    # ------------------------------------------------------- two-phase invocation
    def invoke_tool(
        self,
        task_id: str,
        step_id: str,
        fence: int,
        tool: str,
        args: dict[str, Any],
        executor: Callable[[], Any],
        attempt_scope: str = "s1",
        actor: str = "worker",
    ) -> Any:
        """Execute a tool call with journalling and idempotent replay."""
        self._leases.validate(task_id, step_id, fence)

        key = idempotency_key(task_id, step_id, attempt_scope, tool, args)
        args_digest = sha256_hex(canonical_json(args))
        claimed, prev = self._store.claim_intent(key, task_id, step_id)

        if not claimed:
            intent = self._store.get_intent(key)
            status = intent[0] if intent else None
            if status == "DONE":
                return intent[1] if intent else None      # replay cached result
            if status == "UNKNOWN":
                raise NeedsHumanDecision(
                    f"tool {tool} outcome is unknown for key {key[:12]}"
                )
            # INTENT without RESULT -> a previous attempt died mid-flight
            self._store.finish_intent(key, "UNKNOWN", None)
            self._append(
                task_id,
                EventType.STEP_UNKNOWN,
                {"step_id": step_id, "tool": tool, "idempotency_key": key},
                actor,
            )
            raise NeedsHumanDecision(
                f"tool {tool} crashed mid-flight for key {key[:12]}"
            )

        self._append(
            task_id,
            EventType.INVOCATION_INTENT,
            {"step_id": step_id, "tool": tool, "idempotency_key": key, "args_digest": args_digest},
            actor,
        )
        result = executor()
        self._append(
            task_id,
            EventType.INVOCATION_RESULT,
            {
                "step_id": step_id,
                "tool": tool,
                "idempotency_key": key,
                "result_digest": sha256_hex(canonical_json(result)),
                "status": "OK",
            },
            actor,
        )
        self._store.finish_intent(key, "DONE", result)
        return result

    # ------------------------------------------------------------- step outcomes
    def succeed(self, task_id: str, step_id: str, fence: int, result: Any = None,
                actor: str = "worker") -> None:
        self._leases.validate(task_id, step_id, fence)
        view = self.view(task_id)
        check_step_transition(view.step_status(step_id), EventType.STEP_SUCCEEDED)
        self._append(
            task_id,
            EventType.STEP_SUCCEEDED,
            {"step_id": step_id, "fence": fence,
             "result_digest": sha256_hex(canonical_json(result))},
            actor,
        )

    def fail(self, task_id: str, step_id: str, fence: int, error: str,
             actor: str = "worker") -> None:
        self._leases.validate(task_id, step_id, fence)
        view = self.view(task_id)
        check_step_transition(view.step_status(step_id), EventType.STEP_FAILED)
        self._append(
            task_id, EventType.STEP_FAILED,
            {"step_id": step_id, "fence": fence, "error": error}, actor,
        )

    def mark_unknown(self, task_id: str, step_id: str, reason: str,
                     actor: str = "reaper") -> None:
        view = self.view(task_id)
        check_step_transition(view.step_status(step_id), EventType.STEP_UNKNOWN)
        self._append(
            task_id, EventType.STEP_UNKNOWN,
            {"step_id": step_id, "reason": reason}, actor,
        )

    # ------------------------------------------------------------------ recovery
    def find_dangling_invocations(self, task_id: str) -> list[str]:
        """Idempotency keys with an INTENT but no RESULT in the journal."""
        intents: set[str] = set()
        results: set[str] = set()
        for event in self._store.load(task_id):
            if event.type == EventType.INVOCATION_INTENT:
                intents.add(event.payload["idempotency_key"])
            elif event.type == EventType.INVOCATION_RESULT:
                results.add(event.payload["idempotency_key"])
        return sorted(intents - results)

    def dangling_steps(self, task_id: str) -> set[str]:
        """Steps holding an invocation INTENT without a matching RESULT."""
        steps: set[str] = set()
        keys = set(self.find_dangling_invocations(task_id))
        for event in self._store.load(task_id):
            if event.type == EventType.INVOCATION_INTENT and event.payload.get(
                "idempotency_key"
            ) in keys:
                steps.add(event.payload["step_id"])
        return steps

    def register_compensation(
        self, task_id: str, step_id: str, action: str, payload: dict
    ) -> str:
        """Record what must be undone before a mutating tool call runs."""
        if self._saga is None:
            raise RuntimeError("no saga registry configured; refusing a write tool")
        comp = self._saga.register(task_id, step_id, action, payload)
        return comp.id

    def compensate(self, task_id: str, executors: dict) -> list[dict]:
        if self._saga is None:
            return []
        return self._saga.compensate(executors, task_id)

    def adjudicate(
        self, task_id: str, step_id: str, decision: str, actor: str, note: str = ""
    ) -> None:
        """Resolve a step stuck in UNKNOWN.

        ``confirm_success`` means the side effect did happen; ``confirm_failure``
        means it did not (or was rolled back). Only a human may make this call.
        """
        view = self.view(task_id)
        status = view.step_status(step_id)
        if status is not StepStatus.UNKNOWN:
            raise IllegalTransition(
                f"step {step_id} is {status.value}, not UNKNOWN; nothing to adjudicate"
            )
        if decision == "confirm_success":
            etype = EventType.STEP_SUCCEEDED
        elif decision == "confirm_failure":
            etype = EventType.STEP_FAILED
        else:
            raise ValueError(f"unknown adjudication decision: {decision!r}")
        self._append(
            task_id, etype,
            {"step_id": step_id, "adjudicated_by": actor, "note": note[:500]},
            actor,
        )

    def timeout_step(
        self, task_id: str, step_id: str, reason: str, actor: str = "reaper"
    ) -> None:
        """Reaper-only transition back to a retryable state.

        No fence check: the reaper acts precisely because the lease has already
        expired, so the previous holder is no longer allowed to write anyway.
        """
        view = self.view(task_id)
        check_step_transition(view.step_status(step_id), EventType.STEP_TIMEOUT)
        self._append(
            task_id, EventType.STEP_TIMEOUT, {"step_id": step_id, "reason": reason}, actor
        )

    def recover(self, task_id: str, actor: str = "reaper") -> list[str]:
        """Recover a task after a crash.

        Any invocation that started but never finished becomes UNKNOWN on its
        step. The caller is expected to escalate those steps to a human rather
        than retry them.
        """
        view = self.view(task_id)
        unknown_steps: list[str] = []
        for key in self.find_dangling_invocations(task_id):
            intent = self._store.get_intent(key)
            step_id = None
            for event in self._store.load(task_id):
                if (
                    event.type == EventType.INVOCATION_INTENT
                    and event.payload.get("idempotency_key") == key
                ):
                    step_id = event.payload["step_id"]
                    break
            if step_id is None:
                continue
            self._store.finish_intent(key, "UNKNOWN", None)
            status = view.step_status(step_id)
            if status in (StepStatus.DISPATCHED, StepStatus.RUNNING):
                self.mark_unknown(task_id, step_id, f"dangling invocation {key[:12]}", actor)
                unknown_steps.append(step_id)
        return unknown_steps


__all__ = [
    "TaskService",
    "NeedsHumanDecision",
    "StaleLease",
    "IllegalTransition",
    "TaskStatus",
    "StepStatus",
]
