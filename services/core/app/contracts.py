"""Agent I/O contracts.

The contracts are the enforcement point for two product invariants:

1. **No claim without evidence.** A `Claim` whose evidence ids are not present
   in the run's evidence set is rejected, not silently accepted.
2. **Evidence is immutable and hashed.** `Evidence.digest` is computed from the
   source uri + excerpt so a later tamper is detectable.

Models here are deliberately strict: agent output coming from an LLM is
untrusted input.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class SourceKind(str, Enum):
    METRIC = "metric"
    LOG = "log"
    CHANGE = "change"
    TOPOLOGY = "topology"
    KB = "kb"
    IMPACT = "impact"
    SYNTHETIC = "synthetic"


class AgentRole(str, Enum):
    TELEMETRY = "telemetry"
    LOG = "log"
    CHANGE = "change"
    IMPACT = "impact"
    WRITER = "writer"
    REVIEWER = "reviewer"
    ORCHESTRATOR = "orchestrator"


class Evidence(BaseModel):
    evidence_id: str
    source: SourceKind
    uri: str
    excerpt: str = ""
    captured_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    redaction_level: Literal["none", "low", "high"] = "low"
    # Provenance of the data behind this evidence. A mock or degraded record is
    # still evidence (it is what the tool returned), but it must be visible as
    # such wherever the record is rendered.
    data_mode: Literal["real", "mock", "fixture"] = "real"
    digest: str = ""

    @model_validator(mode="after")
    def _compute_digest(self) -> "Evidence":
        if not self.digest:
            raw = f"{self.source.value}|{self.data_mode}|{self.uri}|{self.excerpt}"
            object.__setattr__(self, "digest", hashlib.sha256(raw.encode("utf-8")).hexdigest())
        return self


class Claim(BaseModel):
    """A conclusion. Must cite at least one piece of evidence."""

    claim_id: str
    statement: str = Field(min_length=8, max_length=600)
    confidence: float = Field(ge=0.0, le=1.0)
    root_cause_rank: int | None = None
    evidence_refs: list[str] = Field(min_length=1)

    @field_validator("evidence_refs")
    @classmethod
    def _no_blank_refs(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for ref in value:
            ref = (ref or "").strip()
            if ref and ref not in cleaned:
                cleaned.append(ref)
        if not cleaned:
            raise ValueError("evidence_refs must contain at least one non-empty id")
        return cleaned

    @field_validator("statement")
    @classmethod
    def _no_placeholder(cls, value: str) -> str:
        if value.strip().lower() in {"n/a", "none", "unknown", "tbd"}:
            raise ValueError("statement must be substantive")
        return value.strip()


class TimelineEvent(BaseModel):
    at: datetime
    kind: Literal["alert", "metric_spike", "log_error", "deploy", "config_change", "recovery", "other"]
    title: str = Field(min_length=3, max_length=200)
    evidence_ref: str


class Anomaly(BaseModel):
    metric: str
    onset: datetime
    severity: Literal["high", "mid", "low"]
    evidence_ref: str


class AgentOutput(BaseModel):
    """Normalised agent result. Unknown extra keys are dropped on purpose."""

    role: AgentRole
    claims: list[Claim] = Field(default_factory=list)
    timeline: list[TimelineEvent] = Field(default_factory=list)
    anomalies: list[Anomaly] = Field(default_factory=list)
    notes: str = ""


class ContractViolation(RuntimeError):
    """Raised when agent output breaks the contract."""


def validate_agent_output(
    output: AgentOutput, known_evidence: set[str]
) -> tuple[AgentOutput, list[str]]:
    """Drop claims that cite unknown evidence; return the cleaned output.

    Returns the accepted output plus a list of rejection reasons, so the
    reviewer agent and the audit log can show what was thrown away.
    """
    rejections: list[str] = []
    accepted: list[Claim] = []

    for claim in output.claims:
        missing = [ref for ref in claim.evidence_refs if ref not in known_evidence]
        if missing:
            rejections.append(
                f"claim {claim.claim_id} dropped: unknown evidence {missing}"
            )
            continue
        accepted.append(claim)

    if not accepted and output.claims:
        raise ContractViolation(
            "all claims were rejected: " + "; ".join(rejections[:3])
        )

    return output.model_copy(update={"claims": accepted}), rejections


EVIDENCE_STRENGTH_BY_SOURCES = {1: 1, 2: 2, 3: 3}


def evidence_strength(claim: Claim, evidence_by_id: dict[str, Evidence]) -> int:
    """1 = weak (single source), 2 = medium, 3 = strong (three distinct sources)."""
    sources = {
        evidence_by_id[ref].source
        for ref in claim.evidence_refs
        if ref in evidence_by_id
    }
    return EVIDENCE_STRENGTH_BY_SOURCES.get(min(len(sources), 3), 1)


def to_plain(obj: Any) -> Any:
    """Helper for JSON-safe dumps in logs."""
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    return obj
