"""Distributed pipeline: dispatch steps to the queue, then assemble from
persisted outputs.

This is the multi-process counterpart of ``Orchestrator.run``:

    dispatch()  ->  N workers execute steps independently  ->  assemble()

Because workers share nothing but the database, every step persists its
evidence and claims (``StepOutputStore``) and the assembler reads them back
before resolving conflicts and rendering the report.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .agents import AgentContext, ReviewerAgent, WriterAgent
from .config import Settings, load_settings
from .contracts import Claim, Evidence, TimelineEvent
from .llm import ModelClient
from .orchestrator import _dedupe_timeline, resolve_conflicts
from .queue import WorkItem, WorkQueue
from .service import IllegalTransition, TaskService
from .step_store import StepOutputStore

DISPATCHABLE = ("telemetry", "log", "change")


@dataclass
class AssembledReport:
    task_id: str
    status: str
    claims: list[Claim]
    timeline: list[TimelineEvent]
    evidence: dict[str, Evidence]
    report_md: str
    needs_human: bool
    claim_scores: dict[str, float]
    rejections: list[str]


def dispatch(service: TaskService, queue: WorkQueue, task_id: str) -> list[str]:
    """Enqueue every not-yet-terminal analysable step."""
    view = service.view(task_id)
    enqueued: list[str] = []
    for step_id, step in view.steps.items():
        if step["status"] in ("SUCCEEDED", "SKIPPED", "FAILED"):
            continue
        if step_id not in DISPATCHABLE:
            continue
        queue.enqueue(
            WorkItem(task_id=task_id, step_id=step_id, capability_tag=step.get("role", ""))
        )
        enqueued.append(step_id)
    return enqueued


def assemble(
    service: TaskService,
    step_store: StepOutputStore,
    task_id: str,
    *,
    llm: ModelClient | None = None,
    settings: Settings | None = None,
) -> AssembledReport:
    """Resolve conflicts from persisted step outputs and render the report."""
    s = settings or load_settings()
    client = llm or ModelClient()

    evidence = step_store.load_evidence(task_id)
    claims = step_store.merged_claims(task_id)
    timeline = step_store.merged_timeline(task_id)

    resolution = resolve_conflicts(claims, evidence, s.conflict_min_confidence)

    # The reviewer needs a context for its prompt; tools are irrelevant here.
    ctx = AgentContext(
        task_id=task_id,
        service=service.view(task_id).title or "unknown-service",
        window="30m",
        tools=None,  # type: ignore[arg-type] - reviewer does not call tools
        llm=client,
        settings=s,
        evidence=evidence,
    )
    review_out, issues = ReviewerAgent().run(ctx, resolution.ranked)
    final_claims = review_out.claims

    timeline = _dedupe_timeline(timeline)
    writer_out = WriterAgent().run(ctx, timeline, final_claims)

    needs_human = resolution.needs_human or not final_claims
    if needs_human:
        try:
            service.request_human(task_id, resolution.reason or "无有效结论", "assembler")
        except IllegalTransition:
            pass
        status = "WAITING_HUMAN"
    else:
        try:
            service.submit_for_review(task_id, "assembler")
            status = "REVIEWING"
        except IllegalTransition:
            status = service.view(task_id).status.value

    return AssembledReport(
        task_id=task_id,
        status=status,
        claims=final_claims,
        timeline=timeline,
        evidence=evidence,
        report_md=writer_out.notes,
        needs_human=needs_human,
        claim_scores={c.claim_id: resolution.scores.get(c.claim_id, 0.0) for c in final_claims},
        rejections=ctx.rejections + issues,
    )
