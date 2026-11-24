"""State machine and projections.

The projection is a pure fold over the journal: ``project(events) -> TaskView``.
Nothing in this module mutates state, which is what makes replay trustworthy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .events import Event, EventType


class TaskStatus(str, Enum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    REVIEWING = "REVIEWING"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StepStatus(str, Enum):
    PENDING = "PENDING"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"
    SKIPPED = "SKIPPED"


TERMINAL_TASK = {TaskStatus.DELIVERED, TaskStatus.FAILED, TaskStatus.CANCELLED}
TERMINAL_STEP = {StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.TIMEOUT, StepStatus.SKIPPED}


@dataclass
class TaskView:
    task_id: str
    status: TaskStatus = TaskStatus.CREATED
    title: str = ""
    tenant_id: str = "default"
    plan_version: int = 0
    steps: dict[str, dict] = field(default_factory=dict)
    last_seq: int = 0

    def step_status(self, step_id: str) -> StepStatus:
        return StepStatus(self.steps[step_id]["status"])

    def dependencies(self, step_id: str) -> list[str]:
        return list(self.steps.get(step_id, {}).get("depends_on") or [])

    def ready_steps(self) -> list[str]:
        ready: list[str] = []
        for sid, step in self.steps.items():
            if StepStatus(step["status"]) is not StepStatus.PENDING:
                continue
            deps = step.get("depends_on") or []
            if all(
                StepStatus(self.steps[dep]["status"]) is StepStatus.SUCCEEDED
                for dep in deps if dep in self.steps
            ):
                ready.append(sid)
        return ready

    def open_steps(self) -> list[str]:
        return [
            sid
            for sid, s in self.steps.items()
            if StepStatus(s["status"]) not in TERMINAL_STEP
        ]

    def all_steps_terminal(self) -> bool:
        return not self.open_steps()


class IllegalTransition(RuntimeError):
    pass


# Task-level allowed transitions: current -> {event types that may apply}
TASK_TRANSITIONS: dict[TaskStatus, set[EventType]] = {
    TaskStatus.CREATED: {EventType.TASK_PLANNED, EventType.TASK_CANCELLED, EventType.TASK_FAILED},
    TaskStatus.PLANNING: {EventType.TASK_PLANNED, EventType.TASK_STARTED, EventType.TASK_FAILED},
    TaskStatus.RUNNING: {
        EventType.TASK_WAITING_HUMAN,
        EventType.TASK_REVIEWING,
        EventType.TASK_FAILED,
        EventType.TASK_CANCELLED,
    },
    TaskStatus.WAITING_HUMAN: {EventType.TASK_RESUMED, EventType.TASK_CANCELLED},
    TaskStatus.REVIEWING: {EventType.TASK_DELIVERED, EventType.TASK_STARTED, EventType.TASK_FAILED},
    TaskStatus.DELIVERED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.CANCELLED: set(),
}

# Step-level allowed transitions: current -> {event types that may apply}
STEP_TRANSITIONS: dict[StepStatus, set[EventType]] = {
    StepStatus.PENDING: {EventType.STEP_LEASED, EventType.STEP_SKIPPED},
    StepStatus.DISPATCHED: {
        EventType.STEP_STARTED,
        EventType.STEP_LEASED,      # lease re-acquired after expiry
        EventType.STEP_TIMEOUT,
        EventType.STEP_UNKNOWN,
    },
    StepStatus.RUNNING: {
        EventType.STEP_SUCCEEDED,
        EventType.STEP_FAILED,
        EventType.STEP_TIMEOUT,
        EventType.STEP_UNKNOWN,
        EventType.STEP_LEASED,      # stale worker re-queued
    },
    StepStatus.UNKNOWN: {EventType.STEP_SUCCEEDED, EventType.STEP_FAILED},
    StepStatus.SUCCEEDED: set(),
    StepStatus.FAILED: {EventType.STEP_LEASED},  # retry after re-plan
    StepStatus.TIMEOUT: {EventType.STEP_LEASED},
    StepStatus.SKIPPED: set(),
}


def check_task_transition(current: TaskStatus, event_type: EventType) -> None:
    if event_type not in TASK_TRANSITIONS[current]:
        raise IllegalTransition(
            f"task transition {current.value} --{event_type.value}--> not allowed"
        )


def check_step_transition(current: StepStatus, event_type: EventType) -> None:
    if event_type not in STEP_TRANSITIONS[current]:
        raise IllegalTransition(
            f"step transition {current.value} --{event_type.value}--> not allowed"
        )


def project(events: list[Event]) -> TaskView:
    """Fold the journal into the current view."""
    if not events:
        raise ValueError("cannot project an empty journal")

    view = TaskView(task_id=events[0].task_id)

    for event in events:
        if event.seq != view.last_seq + 1:
            raise ValueError(
                f"journal gap: expected seq {view.last_seq + 1}, got {event.seq}"
            )
        view.last_seq = event.seq
        etype = event.type
        payload = event.payload

        if etype == EventType.TASK_CREATED:
            view.title = payload.get("title", "")
            view.tenant_id = str(payload.get("tenant_id") or "default")
            view.status = TaskStatus.CREATED

        elif etype == EventType.TASK_PLANNED:
            view.plan_version = int(payload.get("plan_version", view.plan_version + 1))
            for step in payload.get("steps", []):
                view.steps[step["step_id"]] = {
                    "role": step["role"],
                    "status": StepStatus.PENDING.value,
                    "attempt": 0,
                    "fence": 0,
                    "depends_on": list(step.get("depends_on") or []),
                }
            view.status = TaskStatus.PLANNING

        elif etype == EventType.TASK_STARTED:
            view.status = TaskStatus.RUNNING

        elif etype == EventType.TASK_WAITING_HUMAN:
            view.status = TaskStatus.WAITING_HUMAN

        elif etype == EventType.TASK_RESUMED:
            view.status = TaskStatus.RUNNING

        elif etype == EventType.TASK_REVIEWING:
            view.status = TaskStatus.REVIEWING

        elif etype == EventType.TASK_DELIVERED:
            view.status = TaskStatus.DELIVERED

        elif etype == EventType.TASK_FAILED:
            view.status = TaskStatus.FAILED

        elif etype == EventType.TASK_CANCELLED:
            view.status = TaskStatus.CANCELLED

        # ---------------------------------------------------------- step events
        elif etype == EventType.STEP_LEASED:
            step = view.steps[payload["step_id"]]
            step["fence"] = int(payload.get("fence", step["fence"] + 1))
            step["status"] = StepStatus.DISPATCHED.value

        elif etype == EventType.STEP_STARTED:
            step = view.steps[payload["step_id"]]
            step["status"] = StepStatus.RUNNING.value
            step["attempt"] = int(payload.get("attempt", step["attempt"] + 1))

        elif etype == EventType.STEP_SUCCEEDED:
            view.steps[payload["step_id"]]["status"] = StepStatus.SUCCEEDED.value

        elif etype == EventType.STEP_FAILED:
            view.steps[payload["step_id"]]["status"] = StepStatus.FAILED.value

        elif etype == EventType.STEP_TIMEOUT:
            view.steps[payload["step_id"]]["status"] = StepStatus.TIMEOUT.value

        elif etype == EventType.STEP_UNKNOWN:
            view.steps[payload["step_id"]]["status"] = StepStatus.UNKNOWN.value

        elif etype == EventType.STEP_SKIPPED:
            view.steps[payload["step_id"]]["status"] = StepStatus.SKIPPED.value

        # invocation events do not change status; they are evidence for recovery
        elif etype in (EventType.INVOCATION_INTENT, EventType.INVOCATION_RESULT):
            pass

        else:  # pragma: no cover - defensive
            raise ValueError(f"unhandled event type {etype}")

    return view
