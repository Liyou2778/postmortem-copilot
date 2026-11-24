"""Orchestrator: plan → dispatch → collect → resolve → review → report.

Design notes
------------
* The orchestrator never touches the database directly; every state change goes
  through `TaskService`, so the event journal stays the single source of truth.
* A step that throws is retried a bounded number of times and then recorded as
  failed. Exceeding `max_replans` for the same role trips the loop detector and
  the run is aborted instead of spinning forever.
* Conflict resolution is a pure function so it can be unit-tested without a
  model, a database or a network.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .agents import (
    AgentContext,
    ChangeAgent,
    ImpactAgent,
    LogAgent,
    ReviewerAgent,
    TelemetryAgent,
    WriterAgent,
)
from .config import Settings, load_settings
from .contracts import Claim, Evidence, TimelineEvent, evidence_strength
from .dag import descendants
from .llm import ModelClient
from .service import IllegalTransition, TaskService
from .tools import AuditedTools, ToolRegistry, build_default_registry

DEFAULT_PLAN: tuple[dict[str, object], ...] = (
    {"step_id": "telemetry", "role": "telemetry", "capability_tag": "telemetry",
     "depends_on": []},
    {"step_id": "log", "role": "log", "capability_tag": "log",
     "depends_on": []},
    {"step_id": "change", "role": "change", "capability_tag": "change",
     "depends_on": []},
    {"step_id": "impact", "role": "impact", "capability_tag": "impact",
     "depends_on": ["telemetry", "change"]},
    {"step_id": "review", "role": "reviewer", "capability_tag": "review",
     "depends_on": ["telemetry", "log", "change", "impact"]},
)


class LoopDetected(RuntimeError):
    """Raised when a role keeps failing past the replan budget."""


@dataclass
class ConflictResult:
    ranked: list[Claim] = field(default_factory=list)
    needs_human: bool = False
    reason: str = ""
    # Exposed so the ranking is explainable: score = confidence x evidence mult.
    scores: dict[str, float] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)


@dataclass
class RunReport:
    task_id: str
    service: str
    status: str
    timeline: list[TimelineEvent]
    claims: list[Claim]
    evidence: dict[str, Evidence]
    report_md: str
    needs_human: bool
    rejections: list[str]
    tool_calls: list[dict[str, Any]]
    claim_scores: dict[str, float] = field(default_factory=dict)
    impact: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    step_usage: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    failed_steps: dict[str, str] = field(default_factory=dict)
    skipped_steps: list[str] = field(default_factory=list)


# ------------------------------------------------------------ pure resolution
_STOPWORDS = {"的", "了", "和", "与", "在", "是", "把", "从", "到", "导致", "因为"}


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[\s，。；、,:;()（）]+", text) if t and t not in _STOPWORDS}


def _similar(a: Claim, b: Claim) -> bool:
    ta, tb = _tokens(a.statement), _tokens(b.statement)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= 0.75


# Claims that explicitly say "no evidence" are audit notes, not root causes.
# They used to win rank1 by having a high self-reported confidence, pushing the
# actual CPU/disk candidate to rank2. Keep them out of the causal leaderboard.
_NON_CAUSAL_MARKERS = (
    "暂无证据",
    "无证据支持",
    "没有证据",
    "未发现任何",
    "无异常信号",
    "不指向",
    "无法判断",
    "无样本",
    "0 条错误",
    "无法从日志",
    "无法直接定位",
    "未发现异常",
    "未发现应用",
    "不足以",
    "排除",
    "不构成",
    "无异常",
    "极低",
    "无法归因",
    "未记录任何",
    "无法说明",
    "并非",
    "已经退出",
    "未揭示",
)


def _is_non_causal(statement: str) -> bool:
    return any(marker in statement for marker in _NON_CAUSAL_MARKERS)


def resolve_conflicts(
    claims: list[Claim],
    evidence_by_id: dict[str, Evidence],
    min_confidence: float = 0.35,
) -> ConflictResult:
    """Rank claims, drop unsupported ones, flag when a human must decide."""
    dropped: list[str] = []
    valid: list[Claim] = []
    for claim in claims:
        if claim.confidence < min_confidence:
            continue
        missing = [ref for ref in claim.evidence_refs if ref not in evidence_by_id]
        if missing:
            continue
        if _is_non_causal(claim.statement):
            dropped.append(f"{claim.claim_id}: 非因果结论，不进入根因排名")
            continue
        valid.append(claim)
    if not valid:
        return ConflictResult(
            ranked=[], needs_human=True, reason="没有可用的带证据结论", dropped=dropped
        )

    # merge near-duplicates: independent agents reaching the same conclusion
    # from different sources is corroboration, so evidence ids are unioned
    # rather than one claim being discarded.
    merged: list[Claim] = []
    for claim in valid:
        for idx, existing in enumerate(merged):
            if _similar(existing, claim):
                combined = list(existing.evidence_refs)
                for ref in claim.evidence_refs:
                    if ref not in combined:
                        combined.append(ref)
                merged[idx] = existing.model_copy(
                    update={
                        "evidence_refs": combined,
                        "confidence": max(existing.confidence, claim.confidence),
                    }
                )
                break
        else:
            merged.append(claim)

    def score(c: Claim) -> float:
        """Confidence leads; corroboration multiplies, with a hard cap.

        A purely additive score lets a low-confidence claim win just by
        collecting more citations, which is how a decoy outranks the real root
        cause. The multiplier is capped at 1.7x so three weak sources can never
        overturn a confident conclusion on their own.
        """
        strength = evidence_strength(c, evidence_by_id)
        base = c.confidence * (1.0 + 0.35 * (strength - 1))
        # Deterministic metric claims are derived from the actual time series,
        # not from the model's self-reported confidence. Give them a small
        # specificity advantage so a generic LLM sentence ("load increased")
        # cannot outrank the explicitly computed CPU/memory/disk candidate.
        if c.claim_id.startswith("telemetry-"):
            base *= 1.25
        return base

    ranked = sorted(merged, key=score, reverse=True)[:3]
    for i, claim in enumerate(ranked, start=1):
        claim.root_cause_rank = i

    scores = {claim.claim_id: round(score(claim), 3) for claim in ranked}

    needs_human = False
    reason = ""
    if len(ranked) >= 2:
        top, second = ranked[0], ranked[1]
        gap = score(top) - score(second)
        same_strength = evidence_strength(top, evidence_by_id) == evidence_strength(
            second, evidence_by_id
        )
        if gap < 0.05 and same_strength:
            needs_human = True
            reason = (
                f"前两名根因分数接近（{score(top):.2f} vs {score(second):.2f}）"
                f"且证据强度相同，需人工裁决"
            )
    return ConflictResult(
        ranked=ranked, needs_human=needs_human, reason=reason, scores=scores,
        dropped=dropped,
    )


def _dedupe_timeline(events: list[TimelineEvent]) -> list[TimelineEvent]:
    """Collapse duplicate anchors (same second + same evidence) left by
    multiple agents reporting the same fact."""
    seen: set[tuple[str, str]] = set()
    out: list[TimelineEvent] = []
    for event in events:
        key = (event.at.isoformat(timespec="seconds"), event.evidence_ref)
        if key in seen:
            continue
        seen.add(key)
        out.append(event)
    return sorted(out, key=lambda e: e.at)


# ------------------------------------------------------------------ orchestrator
class Orchestrator:
    def __init__(
        self,
        service: TaskService,
        llm: ModelClient | None = None,
        tools: ToolRegistry | None = None,
        settings: Settings | None = None,
        catalog: Any | None = None,
    ) -> None:
        self.service = service
        self.settings = settings or load_settings()
        self.llm = llm or ModelClient()
        self.catalog = catalog
        self._owns_tools = tools is None
        self.tools = tools if tools is not None else self._build_tools()

    def _build_tools(self):
        """Pick the tool backend. MCP spawns real server processes over stdio.

        With ``catalog`` set, every MCP call is routed through the service
        catalog, which decides the datasource URL, selector, metric and the
        real/mock label. Without a catalog the gateway uses its env defaults
        (local tests and the evaluation harness do this on purpose).
        """
        if self.settings.tool_backend == "mcp":
            from .mcp_gateway import MCPGateway, shared_gateway

            if self.settings.mcp_gateway_mode == "shared":
                # Process-wide: the app pays the six interpreter startups once
                # at warm-up instead of on every incident.
                gateway = shared_gateway(self.settings)
                self._owns_tools = False
            else:
                gateway = MCPGateway(
                    timeout=max(30.0, self.settings.tool_timeout_s * 3),
                    settings=self.settings,
                    health_interval_s=0,
                )
                gateway.start()
            if self.catalog is not None:
                from .catalog_router import CatalogRouter

                return AuditedTools(CatalogRouter(gateway, self.catalog, self.settings))
            return AuditedTools(gateway)
        return build_default_registry(self.settings)

    def _shutdown_tools(self) -> None:
        if self._owns_tools and hasattr(self.tools, "close"):
            try:
                self.tools.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - shutdown is best effort
                pass

    # ------------------------------------------------------------------ plan
    def ensure_planned(self, task_id: str) -> None:
        view = self.service.view(task_id)
        if view.status.value in ("RUNNING", "WAITING_HUMAN", "REVIEWING", "DELIVERED"):
            return
        if not view.steps:
            self.service.plan(task_id, list(DEFAULT_PLAN), actor="orchestrator")
        self.service.start(task_id, actor="orchestrator")

    # ------------------------------------------------------------------- run
    def run(self, task_id: str, service_name: str, window: str = "30m") -> RunReport:
        try:
            return self._run_inner(task_id, service_name, window)
        finally:
            self._shutdown_tools()

    def _run_inner(self, task_id: str, service_name: str, window: str = "30m") -> RunReport:
        self.ensure_planned(task_id)
        ctx = AgentContext(
            task_id=task_id,
            service=service_name,
            window=window,
            tools=self.tools,
            llm=self.llm,
            settings=self.settings,
        )

        attempts: dict[str, int] = {}
        timeline: list[TimelineEvent] = []
        all_claims: list[Claim] = []
        step_usage: dict[str, dict[str, int]] = {}
        report_md = ""

        def usage_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
            return {
                key: max(0, int(after.get(key, 0)) - int(before.get(key, 0)))
                for key in ("prompt_tokens", "completion_tokens", "total_tokens", "calls")
            }

        def add_step_usage(step_id: str, delta: dict[str, int]) -> None:
            target = step_usage.setdefault(
                step_id,
                {"prompt_tokens": 0, "completion_tokens": 0,
                 "total_tokens": 0, "calls": 0},
            )
            for key, value in delta.items():
                target[key] += value

        def execute(step_id: str, fn) -> Any:
            """Lease → begin → run → succeed, retrying up to max_replans.

            Retrying re-acquires the lease every attempt, which bumps the fence,
            so a stale attempt can never commit after a newer one started.

            A step absent from the plan is skipped. This keeps tasks planned by
            an older version (pre-M9 plans have no ``impact`` step) resumable
            instead of leasing a step that does not exist.
            """
            if step_id not in self.service.view(task_id).steps:
                return None
            last: Exception | None = None
            for attempt in range(self.settings.max_replans + 1):
                attempts[step_id] = attempt + 1
                owner = f"orchestrator-{step_id}-{attempt}"
                fence = self.service.lease_step(
                    task_id, step_id, owner=owner, ttl_ms=120_000
                )
                if fence is None:
                    raise LoopDetected(f"could not acquire lease for {step_id}")
                self.service.begin_step(task_id, step_id, owner, fence, attempt=attempt + 1)
                before_usage = self.llm.usage_snapshot()
                try:
                    result = fn()
                except Exception as exc:  # noqa: BLE001 - recorded then retried
                    add_step_usage(step_id, usage_delta(before_usage, self.llm.usage_snapshot()))
                    self.service.fail(
                        task_id, step_id, fence, f"{type(exc).__name__}: {exc}", owner
                    )
                    last = exc
                    continue
                add_step_usage(step_id, usage_delta(before_usage, self.llm.usage_snapshot()))
                self.service.succeed(task_id, step_id, fence, {"ok": True}, owner)
                return result

            raise LoopDetected(
                f"step {step_id} exceeded {self.settings.max_replans} replans: {last}"
            )

        failed_steps: dict[str, str] = {}
        skipped_steps: list[str] = []

        def plan_view() -> list[dict]:
            view = self.service.view(task_id)
            return [
                {
                    "step_id": sid,
                    "role": step.get("role", ""),
                    "depends_on": list(step.get("depends_on") or []),
                }
                for sid, step in view.steps.items()
            ]

        def skip_descendants(step_id: str, reason: str) -> None:
            view = self.service.view(task_id)
            for desc in descendants(plan_view(), step_id):
                try:
                    if view.step_status(desc).value == "PENDING":
                        self.service.skip_step(task_id, desc, reason)
                        if desc not in skipped_steps:
                            skipped_steps.append(desc)
                except Exception:  # noqa: BLE001 - already terminal
                    pass

        def deps_ok(step_id: str) -> bool:
            view = self.service.view(task_id)
            if step_id not in view.steps:
                return False
            return all(
                view.steps[dep]["status"] == "SUCCEEDED"
                for dep in view.dependencies(step_id) if dep in view.steps
            )

        def run_step(step_id: str, fn) -> Any:
            """DAG-aware execute: skip descendants, keep independent branches."""
            if step_id not in self.service.view(task_id).steps:
                return None
            if not deps_ok(step_id):
                try:
                    if self.service.view(task_id).step_status(step_id).value == "PENDING":
                        self.service.skip_step(
                            task_id, step_id, "upstream branch failed"
                        )
                        if step_id not in skipped_steps:
                            skipped_steps.append(step_id)
                except Exception:  # noqa: BLE001
                    pass
                return None
            try:
                return execute(step_id, fn)
            except LoopDetected as exc:
                failed_steps[step_id] = str(exc)[:300]
                skip_descendants(step_id, f"upstream {step_id} failed")
                return None

        # --- telemetry / log / change (independent branches) ---------------
        tlm = run_step("telemetry", lambda: TelemetryAgent().run(ctx))
        if tlm is not None:
            all_claims.extend(tlm.claims)
            if tlm.anomalies:
                for a in tlm.anomalies:
                    timeline.append(
                        TimelineEvent(
                            at=a.onset, kind="metric_spike",
                            title=f"{a.metric} 异常", evidence_ref=a.evidence_ref,
                        )
                    )

        lg = run_step("log", lambda: LogAgent().run(ctx))
        if lg is not None:
            all_claims.extend(lg.claims)

        chg = run_step("change", lambda: ChangeAgent().run(ctx))
        if chg is not None:
            all_claims.extend(chg.claims)
            timeline.extend(chg.timeline)

        # --- impact (depends telemetry + change) ---------------------------
        # Deterministic aggregation, no model call. Failure is a degraded
        # section; downstream review is skipped by the DAG policy.
        run_step("impact", lambda: ImpactAgent().run(ctx))

        # --- resolve -------------------------------------------------------
        resolution = resolve_conflicts(
            all_claims, ctx.evidence, self.settings.conflict_min_confidence
        )
        ctx.rejections.extend(resolution.dropped)

        # --- review --------------------------------------------------------
        reviewed = run_step(
            "review", lambda: ReviewerAgent().run(ctx, resolution.ranked)
        )
        if reviewed is not None:
            review_out, issues = reviewed
            ctx.rejections.extend(issues)
            final_claims = review_out.claims
        else:
            # Keep conflict resolution output rather than dropping all claims;
            # the report is explicitly partial and the task asks for a human.
            final_claims = resolution.ranked

        # --- timeline: built by the agents from their own evidence ----------
        timeline = _dedupe_timeline(timeline)

        # --- usage / cost --------------------------------------------------
        usage = self.llm.usage_snapshot()
        cost = self._cost(usage, step_usage)
        ctx.usage = usage
        ctx.cost = cost

        # --- write ---------------------------------------------------------
        writer = WriterAgent()
        writer_out = writer.run(ctx, timeline, final_claims)
        report_md = writer_out.notes

        needs_human = (
            resolution.needs_human or not final_claims or bool(failed_steps)
        )
        status = "WAITING_HUMAN" if needs_human else "RUNNING"
        if needs_human:
            reason = resolution.reason or "无有效结论"
            if failed_steps:
                reason = (
                    f"DAG 分支失败: {', '.join(sorted(failed_steps))}; "
                    f"已跳过: {', '.join(skipped_steps) or '无'}"
                )
            try:
                self.service.request_human(task_id, reason, "orchestrator")
            except IllegalTransition:
                pass
        else:
            try:
                self.service.submit_for_review(task_id, "orchestrator")
                status = "REVIEWING"
            except IllegalTransition:
                status = self.service.view(task_id).status.value

        return RunReport(
            task_id=task_id,
            service=service_name,
            status=status,
            timeline=sorted(timeline, key=lambda e: e.at),
            claims=final_claims,
            evidence=ctx.evidence,
            report_md=report_md,
            needs_human=needs_human,
            rejections=ctx.rejections,
            claim_scores={c.claim_id: resolution.scores.get(c.claim_id, 0.0) for c in final_claims},
            impact=ctx.impact,
            usage=usage,
            step_usage=step_usage,
            cost=cost,
            failed_steps=failed_steps,
            skipped_steps=skipped_steps,
            tool_calls=[
                {"tool": a.tool, "ok": a.ok, "latency_ms": a.latency_ms, "error": a.error}
                for a in self.tools.audit
            ],
        )

    def _cost(self, usage: dict[str, Any], step_usage: dict[str, Any]) -> dict[str, Any]:
        input_rate = float(self.settings.llm_cost_per_1k_input or 0)
        output_rate = float(self.settings.llm_cost_per_1k_output or 0)
        configured = input_rate > 0 or output_rate > 0
        estimated = (
            float(usage.get("prompt_tokens", 0)) / 1000.0 * input_rate
            + float(usage.get("completion_tokens", 0)) / 1000.0 * output_rate
        )
        return {
            **usage,
            "configured": configured,
            "estimated_cost": round(estimated, 6),
            "currency": self.settings.llm_currency,
            "step_usage": step_usage,
        }

    # ------------------------------------------------------------- helpers
