"""Pure DAG planning, local replanning and consistent-hash assignment.

The orchestrator used to run a fixed list of five steps. That is not a DAG: it
cannot express "impact depends on telemetry and change" or "review depends on
all evidence agents". This module keeps the graph logic pure so it can be tested
without a database, model or network.

Failure policy (local replan) is deliberately conservative:

* a step that fails after its retry budget taints only its descendants;
* independent branches keep running;
* every descendant is marked SKIPPED explicitly, never silently omitted;
* the task goes to WAITING_HUMAN if any branch was tainted, even when the
  remaining evidence produced a report.

Distributed assignment is rendezvous hashing over (step_id, capability). Adding
or removing a worker only remaps the keys that worker owns; it is stable and
does not need a central lock.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable


class PlanError(ValueError):
    pass


@dataclass(frozen=True)
class PlanStep:
    step_id: str
    role: str
    depends_on: tuple[str, ...] = ()
    capability_tag: str = ""
    side_effect: bool = False
    max_attempts: int = 1


def normalize_steps(steps: Iterable[dict | PlanStep]) -> list[PlanStep]:
    out: list[PlanStep] = []
    for raw in steps:
        if isinstance(raw, PlanStep):
            out.append(raw)
            continue
        deps = raw.get("depends_on") or ()
        if isinstance(deps, str):
            deps = (deps,)
        out.append(PlanStep(
            step_id=str(raw.get("step_id") or "").strip(),
            role=str(raw.get("role") or "").strip(),
            depends_on=tuple(str(d) for d in deps),
            capability_tag=str(raw.get("capability_tag") or ""),
            side_effect=bool(raw.get("side_effect", False)),
            max_attempts=max(1, int(raw.get("max_attempts", 1) or 1)),
        ))
    return out


def validate_plan(steps: Iterable[dict | PlanStep]) -> list[PlanStep]:
    plan = normalize_steps(steps)
    if not plan:
        raise PlanError("plan must contain at least one step")
    ids: set[str] = set()
    for step in plan:
        if not step.step_id:
            raise PlanError("step_id is required")
        if step.step_id in ids:
            raise PlanError(f"duplicate step_id: {step.step_id}")
        if not step.role:
            raise PlanError(f"step {step.step_id} has no role")
        ids.add(step.step_id)
    graph = {step.step_id: list(step.depends_on) for step in plan}
    for step in plan:
        for dep in step.depends_on:
            if dep not in ids:
                raise PlanError(f"step {step.step_id} depends on unknown {dep}")
            if dep == step.step_id:
                raise PlanError(f"step {step.step_id} depends on itself")

    # Kahn topological sort; also detects cycles.
    indegree = {node: len(deps) for node, deps in graph.items()}
    ready = [step.step_id for step in plan if indegree[step.step_id] == 0]
    order: list[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for other, deps in graph.items():
            if node in deps:
                indegree[other] -= 1
                if indegree[other] == 0:
                    ready.append(other)
    if len(order) != len(plan):
        raise PlanError("plan contains a cycle")
    return plan


def topological_order(steps: Iterable[dict | PlanStep]) -> list[str]:
    plan = validate_plan(steps)
    by_id = {step.step_id: step for step in plan}
    indegree = {step.step_id: len(step.depends_on) for step in plan}
    ready = [step.step_id for step in plan if indegree[step.step_id] == 0]
    order: list[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for step in plan:
            if node in step.depends_on:
                indegree[step.step_id] -= 1
                if indegree[step.step_id] == 0:
                    ready.append(step.step_id)
    return [sid for sid in order if sid in by_id]


def descendants(steps: Iterable[dict | PlanStep], step_id: str) -> list[str]:
    plan = validate_plan(steps)
    result: set[str] = set()
    frontier = {step_id}
    while frontier:
        current = frontier.pop()
        for step in plan:
            if current in step.depends_on and step.step_id not in result:
                result.add(step.step_id)
                frontier.add(step.step_id)
    return [step.step_id for step in plan if step.step_id in result]


def local_replan(steps: Iterable[dict | PlanStep], failed_step: str) -> dict:
    """Return the steps to skip after a failed step; independent steps stay."""
    plan = validate_plan(steps)
    ids = {step.step_id for step in plan}
    if failed_step not in ids:
        raise PlanError(f"unknown failed step: {failed_step}")
    skip = set(descendants(plan, failed_step))
    return {
        "failed": failed_step,
        "skip": [step.step_id for step in plan if step.step_id in skip],
        "runnable": [
            step.step_id for step in plan
            if step.step_id != failed_step and step.step_id not in skip
        ],
    }


def _score(key: str, worker: str) -> int:
    digest = hashlib.sha256(f"{key}|{worker}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


class ConsistentHashRing:
    """Rendezvous hashing: no virtual nodes to maintain, stable on join/leave."""

    def __init__(self, workers: Iterable[str]) -> None:
        self.workers = sorted({str(worker) for worker in workers if worker})
        if not self.workers:
            raise ValueError("at least one worker is required")

    def owner(self, key: str) -> str:
        return max(self.workers, key=lambda worker: _score(key, worker))

    def assignment(self, keys: Iterable[str]) -> dict[str, str]:
        return {key: self.owner(key) for key in keys}

    def remap_fraction(self, other: "ConsistentHashRing", keys: Iterable[str]) -> float:
        keys = list(keys)
        if not keys:
            return 0.0
        changed = sum(1 for key in keys if self.owner(key) != other.owner(key))
        return round(changed / len(keys), 4)


@dataclass
class SchedulerDecision:
    assignments: dict[str, str] = field(default_factory=dict)
    unassigned: list[str] = field(default_factory=list)


def assign_steps(steps: Iterable[dict | PlanStep], workers: Iterable[str],
                 ready_only: bool = True) -> SchedulerDecision:
    plan = validate_plan(steps)
    ring = ConsistentHashRing(workers)
    keys = [step.step_id for step in plan] if not ready_only else [
        step.step_id for step in plan if not step.depends_on
    ]
    return SchedulerDecision(assignments=ring.assignment(keys))
