"""Evaluation metrics and the scenario runner.

The runner drives the real pipeline (agents → MCP tool servers → devstack HTTP)
against a labelled scenario and returns a ranked prediction. Metrics are then
computed over all scenarios.

Cost control: one MCP gateway is reused across all scenarios instead of
spawning six server processes per scenario.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings, load_settings
from app.contracts import Claim, Evidence
from app.lease import LeaseManager
from app.llm import ModelClient
from app.orchestrator import Orchestrator
from app.service import TaskService
from app.store import EventStore
from .scenarios import Scenario


@dataclass
class Prediction:
    scenario_id: str
    fault_type: str
    service: str
    claims: list[Claim]
    evidence: dict[str, Evidence]
    status: str
    latency_s: float = 0.0
    notes: list[str] = field(default_factory=list)


@dataclass
class Score:
    scenario_id: str
    fault_type: str
    hit_at_1: bool
    hit_at_3: bool
    reciprocal_rank: float
    predicted_rank1: str = ""


def _is_correct(claim: Claim, scenario: Scenario, evidence: dict[str, Evidence]) -> bool:
    """Two-sided match: grounded in the fault evidence AND names the concept.

    *Evidence-only* is gamed by citing every reference available (an earlier
    version scored 100% this way). *Statement-only* is gamed by keyword
    regurgitation. Requiring both is what a human reviewer actually checks:
    "does it point at the right evidence, and does it say the right thing?"
    """
    cited = [evidence[ref] for ref in claim.evidence_refs if ref in evidence]
    if not cited:
        return False

    grounded = any(
        marker.lower() in f"{item.excerpt} {item.uri}".lower()
        for item in cited
        for marker in scenario.markers
    )
    if not grounded:
        return False

    statement = claim.statement.lower()
    return any(term.lower() in statement for term in scenario.terms)


def score_prediction(pred: Prediction, scenario: Scenario) -> Score:
    ranked = sorted(pred.claims, key=lambda c: (c.root_cause_rank or 99))
    hits = [_is_correct(c, scenario, pred.evidence) for c in ranked]
    hit1 = bool(hits[:1] and hits[0])
    hit3 = any(hits[:3])
    rr = 0.0
    for rank, hit in enumerate(hits, start=1):
        if hit:
            rr = 1.0 / rank
            break
    top = ranked[0].statement[:60] if ranked else ""
    return Score(
        scenario_id=scenario.scenario_id,
        fault_type=scenario.fault_type,
        hit_at_1=hit1,
        hit_at_3=hit3,
        reciprocal_rank=rr,
        predicted_rank1=top,
    )


def aggregate(scores: list[Score]) -> dict[str, Any]:
    if not scores:
        return {"count": 0}
    n = len(scores)
    return {
        "count": n,
        "recall_at_1": round(sum(s.hit_at_1 for s in scores) / n, 4),
        "recall_at_3": round(sum(s.hit_at_3 for s in scores) / n, 4),
        "mrr": round(statistics.fmean(s.reciprocal_rank for s in scores), 4),
        "by_fault": {
            fault: {
                "count": len(group),
                "recall_at_3": round(sum(s.hit_at_3 for s in group) / len(group), 4),
            }
            for fault in sorted({s.fault_type for s in scores})
            for group in [[s for s in scores if s.fault_type == fault]]
        },
    }


class ScenarioRunner:
    """Runs scenarios against the live pipeline with a shared MCP gateway."""

    def __init__(self, llm: ModelClient | None = None, settings: Settings | None = None,
                 use_mcp: bool = True) -> None:
        self.settings = settings or load_settings()
        self.llm = llm or ModelClient()
        self.use_mcp = use_mcp
        self._gateway = None

    def __enter__(self) -> "ScenarioRunner":
        if self.use_mcp:
            from app.mcp_gateway import MCPGateway

            self._gateway = MCPGateway(timeout=30)
            self._gateway.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._gateway is not None:
            self._gateway.close()

    def run(self, scenario: Scenario) -> Prediction:
        import time

        store = EventStore(":memory:")
        service = TaskService(store, LeaseManager(store._conn, store._lock))
        orch = Orchestrator(
            service,
            llm=self.llm,
            tools=self._gateway,       # shared, not owned -> not closed per run
            settings=self.settings,
        )
        task_id = service.create_task(f"{scenario.fault_type} on {scenario.service}")
        started = time.perf_counter()
        notes: list[str] = []
        try:
            report = orch.run(task_id, scenario.service, "30m")
            claims = report.claims
            evidence = report.evidence
            status = report.status
            notes.extend(report.rejections)
        except Exception as exc:  # noqa: BLE001 - a failed scenario is data, not a crash
            claims, evidence, status = [], {}, "ERROR"
            notes.append(f"{type(exc).__name__}: {exc}")
        elapsed = time.perf_counter() - started
        store.close()
        return Prediction(
            scenario_id=scenario.scenario_id,
            fault_type=scenario.fault_type,
            service=scenario.service,
            claims=claims,
            evidence=evidence,
            status=status,
            latency_s=round(elapsed, 2),
            notes=notes,
        )
