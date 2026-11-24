"""The six agent roles.

Each role owns exactly one capability, calls only the tools it needs, and must
return a contract-valid `AgentOutput`. An agent that cannot gather evidence
returns an empty result with a note instead of inventing one — the orchestrator
treats empty as "evidence unavailable" rather than "no problem".
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from .config import Settings
from .contracts import (
    AgentOutput,
    AgentRole,
    Anomaly,
    Claim,
    Evidence,
    SourceKind,
    TimelineEvent,
)
from .llm import ModelClient
from .tools import ToolRegistry, data_mode_note, evidence_mode, redact

SYSTEM_SCHEMA = """你是互联网平台的 SRE 根因分析助手。只输出 JSON，不要输出解释文字。

输出必须严格遵守下面这个 schema，字段名一个字都不能改：

{
  "claims": [
    {
      "id": "c1",
      "statement": "一句话结论，至少 8 个字，不能为空",
      "confidence": 0.82,
      "root_cause_rank": 1,
      "evidence_refs": ["E-XXX-001"]
    }
  ]
}

硬性要求：
1. 字段名只能是 id / statement / confidence / root_cause_rank / evidence_refs。
   禁止使用 claim、description、text、evidence、citations 等任何别名。
2. confidence 必须是 0 到 1 之间的数字（如 0.82），禁止使用 high/medium/low 等词。
3. evidence_refs 只能填提示中"可用证据"里真实出现过的编号，禁止编造。
4. statement 不能为空字符串。
5. 如果证据不足以推出结论，返回 {"claims": []}，不要硬编。
"""

# Backwards-compatible alias used elsewhere.
SYSTEM_BASE = SYSTEM_SCHEMA

# Documented, bounded coercion for the word-scale confidence some models emit.
_CONFIDENCE_WORDS = {"high": 0.75, "medium": 0.55, "low": 0.35, "中": 0.55, "高": 0.75, "低": 0.35}


def _coerce_confidence(value: Any) -> tuple[float, bool]:
    """Return (confidence, coerced). Word scales map to fixed, capped values."""
    if isinstance(value, (int, float)):
        return float(value), False
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _CONFIDENCE_WORDS:
            return _CONFIDENCE_WORDS[text], True
        try:
            return float(text), True
        except ValueError:
            raise ValueError(f"confidence {value!r} is neither a number nor a known scale")
    raise ValueError(f"unsupported confidence type {type(value).__name__}")


@dataclass
class AgentContext:
    task_id: str
    service: str
    window: str
    tools: ToolRegistry
    llm: ModelClient
    settings: Settings
    evidence: dict[str, Evidence] = field(default_factory=dict)
    rejections: list[str] = field(default_factory=list)
    # Structured impact data (M9); rendered by the writer, never invented by
    # the model. Empty when the datasource is mock or has no app metrics.
    impact: dict[str, Any] = field(default_factory=dict)
    # Per-run token usage / estimated cost (M10.3); filled before the writer.
    usage: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)

    def add_evidence(self, evidence: Evidence) -> Evidence:
        self.evidence[evidence.evidence_id] = evidence
        return evidence

    def next_id(self, prefix: str) -> str:
        n = sum(1 for k in self.evidence if k.startswith(f"E-{prefix}-")) + 1
        return f"E-{prefix}-{n:03d}"

    def evidence_block(self) -> str:
        lines = []
        for e in self.evidence.values():
            lines.append(f"- {e.evidence_id} | source={e.source.value} | {e.excerpt[:160]}")
        return "\n".join(lines) or "（无可用证据）"


_STATEMENT_ALIASES = ("statement", "claim", "description", "text", "conclusion")
_EVIDENCE_ALIASES = ("evidence_refs", "evidence", "citations", "refs", "sources")


def _first_present(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return None


def _parse_claims(raw: dict[str, Any], ctx: AgentContext, role: str) -> tuple[list[Claim], list[str]]:
    """Turn model output into validated claims.

    Field-name aliases are accepted because that is a purely syntactic
    difference. A word-scale confidence is coerced through a documented,
    capped mapping and flagged as coerced. Anything else is dropped and
    reported — the validator never guesses a conclusion.
    """
    claims: list[Claim] = []
    problems: list[str] = []

    for item in raw.get("claims", []) or []:
        if not isinstance(item, dict):
            problems.append(f"{role}: claim item is not an object")
            continue
        try:
            confidence, coerced = _coerce_confidence(item.get("confidence", 0.5))
            refs = _first_present(item, _EVIDENCE_ALIASES) or []
            if isinstance(refs, str):
                refs = [refs]
            # Namespace the id by role: every agent independently emits "c1",
            # and duplicate ids corrupt any id-keyed map (scores, citations).
            raw_id = str(item.get("id") or item.get("claim_id") or f"c{len(claims)}")
            claim = Claim(
                claim_id=f"{role}-{raw_id}",
                statement=_first_present(item, _STATEMENT_ALIASES) or "",
                confidence=confidence,
                root_cause_rank=item.get("root_cause_rank"),
                evidence_refs=refs,
            )
            claims.append(claim)
            if coerced:
                ctx.rejections.append(
                    f"{role}: confidence coerced to {confidence:.2f} "
                    f"(model returned {item.get('confidence')!r})"
                )
        except (ValidationError, TypeError, ValueError) as exc:
            problems.append(f"{role}: malformed claim dropped ({exc})"[:220])

    ctx.rejections.extend(problems)
    return claims, problems


def _ask_claims(ctx: AgentContext, prompt: str, role: str, max_repair: int = 1) -> list[Claim]:
    """Ask for claims, and on a total parse failure ask once more with the errors.

    One repair round is enough in practice; looping would burn tokens without
    converging, so the budget is explicit and small.
    """
    raw = ctx.llm.chat_json(SYSTEM_SCHEMA, prompt)
    claims, problems = _parse_claims(raw, ctx, role)
    if claims or not (raw.get("claims") or []):
        return claims

    for attempt in range(max_repair):
        repair = (
            prompt
            + "\n\n你上一次的输出不符合 schema，问题如下：\n"
            + "\n".join(f"- {p}" for p in problems[:5])
            + '\n请严格按 {"claims":[{"id","statement","confidence","root_cause_rank","evidence_refs"}]} '
            "重新输出，confidence 必须是 0-1 的数字。"
        )
        retry_raw = ctx.llm.chat_json(SYSTEM_SCHEMA, repair)
        retry_claims, retry_problems = _parse_claims(retry_raw, ctx, f"{role}-repair{attempt + 1}")
        if retry_claims:
            return retry_claims
        problems = retry_problems
    return []



def _parse_timeline(raw: dict[str, Any]) -> list[TimelineEvent]:
    events: list[TimelineEvent] = []
    for item in raw.get("events", []) or []:
        try:
            events.append(
                TimelineEvent(
                    at=datetime.fromisoformat(str(item["at"]).replace("Z", "+00:00")),
                    kind=item.get("kind", "other"),
                    title=item.get("title", ""),
                    evidence_ref=item.get("evidence_ref", ""),
                )
            )
        except (ValidationError, KeyError, ValueError):
            continue
    return events


# ---------------------------------------------------------------- telemetry
def _extra_signal_significant(metric: str, change_pct: float, peak: float) -> bool:
    """A relative jump is not enough for a physical saturation claim.

    Negative control showed disk IO 0.001 -> 0.004 (0.4% busy) being reported
    as 363% "saturation". Require a real absolute floor for IO-time metrics.
    """
    if change_pct < 100:
        return False
    if metric.startswith("node_disk_io_time_seconds_total") and peak < 0.2:
        return False
    if metric.startswith("node_disk_write_time_seconds_total") and peak < 0.2:
        return False
    return True


class TelemetryAgent:
    role = AgentRole.TELEMETRY

    def run(self, ctx: AgentContext) -> AgentOutput:
        # The metric name is NOT hardcoded: the service catalog decides what the
        # primary signal is for this service (node_load1 for a host, p99 latency
        # for an application). The router fills ``metric`` from the binding.
        result = ctx.tools.call(
            "prometheus.query_range", service=ctx.service, window=ctx.window, slot="primary"
        )
        if not result.ok:
            return AgentOutput(role=self.role, notes=f"指标不可得: {result.error}")

        data = result.data or {}
        points = data.get("points") or []
        metric = str(data.get("metric") or "unknown")
        if not points:
            return AgentOutput(
                role=self.role,
                notes=f"指标 {metric} 无数据（服务未接入真实 Prometheus，或时间窗内无样本）",
            )

        anomaly = data.get("anomaly") or {}
        mode = evidence_mode(data)
        if anomaly:
            baseline = anomaly.get("baseline")
            change = anomaly.get("change_pct")
            direction = {"up": "升高", "down": "下降"}.get(
                str(anomaly.get("direction")), "变化"
            )
            detail = (
                f"{metric} 在 {anomaly.get('t')} {direction}（方向 {anomaly.get('direction')}），"
                f"基线 {baseline} → 峰值 {anomaly.get('v')}"
                + (f"，变化 {change}%" if change is not None else "")
            )
        else:
            values = [float(p.get("v", 0)) for p in points]
            detail = (
                f"{metric} 在窗口内无显著异常"
                f"（{len(values)} 个样本，{min(values):.4g}–{max(values):.4g}）"
            )

        evidence = ctx.add_evidence(
            Evidence(
                evidence_id=ctx.next_id("TLM"),
                source=SourceKind.METRIC,
                uri=f"prometheus://query/{metric}?service={ctx.service}&mode={mode}",
                excerpt=redact(
                    data_mode_note(data)
                    + detail
                    + f"；查询语句 {data.get('query')}"
                ),
                redaction_level="low",
                data_mode=mode,
            )
        )

        prompt = (
            f"服务: {ctx.service}\n时间窗: {ctx.window}\n"
            f"主指标（服务目录指定）: {json.dumps(data, ensure_ascii=False)}\n"
        )

        # A secondary signal separates "demand caused it" from "capacity caused
        # it". It is optional: a service whose catalog binding has no secondary
        # metric simply skips this step instead of querying a made-up metric.
        secondary_anomaly: dict[str, Any] = {}
        secondary_metric = ""
        secondary_last: float | None = None
        secondary_min: float | None = None
        secondary_evidence_id = ""
        secondary_result = ctx.tools.call(
            "prometheus.query_range", service=ctx.service, window=ctx.window, slot="secondary"
        )
        if secondary_result.ok and secondary_result.data:
            sdata = secondary_result.data or {}
            spoints = sdata.get("points") or []
            if spoints:
                smetric = str(sdata.get("metric") or "secondary")
                secondary_metric = smetric
                secondary_anomaly = sdata.get("anomaly") or {}
                last = spoints[-1].get("v")
                try:
                    secondary_last = float(last)
                except (TypeError, ValueError):
                    secondary_last = None
                try:
                    secondary_min = min(float(p.get("v", 0)) for p in spoints)
                except (TypeError, ValueError):
                    secondary_min = None
                secondary_evidence = ctx.add_evidence(
                    Evidence(
                        evidence_id=ctx.next_id("QPS"),
                        source=SourceKind.METRIC,
                        uri=f"prometheus://query/{smetric}?service={ctx.service}"
                            f"&mode={evidence_mode(sdata)}",
                        excerpt=redact(
                            data_mode_note(sdata)
                            + f"辅助指标 {smetric}={last}（窗口 {ctx.window}；"
                            f"查询语句 {sdata.get('query')}）"
                        ),
                        redaction_level="low",
                        data_mode=evidence_mode(sdata),
                    )
                )
                secondary_evidence_id = secondary_evidence.evidence_id
                prompt += f"\n辅助指标: {json.dumps(sdata, ensure_ascii=False)}\n"

        # Metric pack: optional extra signals (disk IO, error rate, CPU...).
        # The catalog decides how many exist; an unconfigured slot fails closed
        # and stops the loop rather than querying invented metric names.
        extra_anomalies: list[Anomaly] = []
        deterministic_claims: list[Claim] = []
        extra_dominant = False
        primary_pct = abs(float(anomaly.get("change_pct") or 0)) if anomaly else 0.0
        extra_count = int(data.get("extra_metric_count") or 0)
        for index in range(min(extra_count, 4)):
            extra_result = ctx.tools.call(
                "prometheus.query_range", service=ctx.service,
                window=ctx.window, slot=f"extra:{index}",
            )
            if not extra_result.ok:
                break
            edata = extra_result.data or {}
            epoints = edata.get("points") or []
            if not epoints:
                continue
            emetric = str(edata.get("metric") or f"extra:{index}")
            emode = evidence_mode(edata)
            eanomaly = edata.get("anomaly") or {}
            if eanomaly:
                epct = eanomaly.get("change_pct")
                edetail = (
                    f"{emetric} 在 {eanomaly.get('t')} "
                    f"{'升高' if eanomaly.get('direction') == 'up' else '下降'}"
                    f"（基线 {eanomaly.get('baseline')} → {eanomaly.get('v')}"
                    + (f"，变化 {epct}%" if epct is not None else "")
                    + "）"
                )
            else:
                values = [float(p.get("v", 0)) for p in epoints]
                edetail = (
                    f"{emetric} 无显著异常"
                    f"（{len(values)} 个样本，{min(values):.4g}–{max(values):.4g}）"
                )
            extra_evidence = ctx.add_evidence(
                Evidence(
                    evidence_id=ctx.next_id("EXT"),
                    source=SourceKind.METRIC,
                    uri=f"prometheus://query/{emetric}?service={ctx.service}"
                        f"&mode={emode}",
                    excerpt=redact(
                        data_mode_note(edata)
                        + f"扩展指标（{edata.get('metric_type', 'gauge')}）"
                        + edetail
                        + f"；查询语句 {edata.get('query')}"
                    ),
                    redaction_level="low",
                    data_mode=emode,
                )
            )
            prompt += (
                f"\n扩展指标 {index}: {emetric}\n"
                f"{json.dumps(edata, ensure_ascii=False)}\n"
            )
            if eanomaly:
                try:
                    eonset = datetime.fromisoformat(
                        str(eanomaly.get("t")).replace("Z", "+00:00")
                    )
                except (TypeError, ValueError):
                    eonset = None
                if eonset is not None:
                    echange = abs(float(eanomaly.get("change_pct") or 0))
                    if (
                        emetric == "node_cpu_seconds_total"
                        and float(eanomaly.get("v") or 0) >= 70
                    ):
                        deterministic_claims.append(
                            Claim(
                                claim_id="telemetry-cpu",
                                statement=(
                                    f"CPU 利用率峰值 "
                                    f"{float(eanomaly.get('v')):.1f}%"
                                    "（100% 为满载），指向节点 CPU 饱和"
                                ),
                                confidence=0.91,
                                evidence_refs=[extra_evidence.evidence_id],
                            )
                        )
                    extra_anomalies.append(
                        Anomaly(
                            metric=emetric,
                            onset=eonset,
                            severity="high" if echange >= 50 else "mid",
                            evidence_ref=extra_evidence.evidence_id,
                        )
                    )
                    # A dominant extra signal is a first-class candidate, not
                    # just prompt context. Require 5x the primary movement so
                    # CPU stress (which also perturbs disk IO) does not create
                    # a false disk root cause. This is deterministic evidence,
                    # not an LLM guess.
                    if (
                        _extra_signal_significant(
                            emetric, echange, float(eanomaly.get("v") or 0)
                        )
                        and echange >= primary_pct * 5
                    ):
                        label = (
                            "磁盘 IO" if emetric.startswith("node_disk")
                            else "CPU" if emetric.startswith("node_cpu")
                            else "扩展指标"
                        )
                        deterministic_claims.append(
                            Claim(
                                claim_id=f"telemetry-extra-{index}",
                                statement=(
                                    f"{label}指标 {emetric} 相对基线变化 {echange:.1f}%，"
                                    f"是窗口内最显著且超出主指标的异常，指向{label}饱和"
                                ),
                                confidence=0.86,
                                evidence_refs=[extra_evidence.evidence_id],
                            )
                        )
                        extra_dominant = True

        # Load1 is a direct CPU/run-queue signal. A huge disk co-signal must not
        # erase it. Use the peak and require it to be materially above the
        # baseline, so a disk fault's post-fault load recovery does not create
        # a false CPU claim.
        try:
            max_point = max(float(p.get("v", 0)) for p in points)
        except (TypeError, ValueError):
            max_point = 0.0
        try:
            baseline_value = float(anomaly.get("baseline") or 0)
        except (TypeError, ValueError):
            baseline_value = 0.0
        if (
            metric == "node_load1"
            and max_point >= 4
            and (baseline_value == 0 or max_point >= baseline_value * 1.5)
        ):
            deterministic_claims.append(
                Claim(
                    claim_id="telemetry-load",
                    statement=(
                        f"node_load1 峰值 {max_point:.2f}（基线 "
                        f"{baseline_value:.2f}），指向节点 CPU/运行队列饱和"
                    ),
                    confidence=0.89,
                    evidence_refs=[evidence.evidence_id],
                )
            )

        # When the primary signal is real and no extra signal dominates it,
        # make the primary cause a deterministic candidate too. This prevents
        # the LLM from picking a co-occurring disk signal for a CPU fault.
        primary_peak = float(anomaly.get("v") or 0) if anomaly else 0.0
        primary_is_high_load = metric == "node_load1" and primary_peak >= 4
        primary_direction_up = str(anomaly.get("direction") or "") == "up"
        if (
            anomaly
            and primary_pct >= 20
            and primary_direction_up
            and (not extra_dominant or primary_is_high_load)
        ):
            deterministic_claims.append(
                Claim(
                    claim_id="telemetry-primary",
                    statement=(
                        f"主指标 {metric} 在窗口内从基线 {anomaly.get('baseline')} "
                        f"变化到 {anomaly.get('v')}（{primary_pct:.1f}%），"
                        "指向节点 CPU/运行队列或主资源压力"
                    ),
                    confidence=0.88,
                    evidence_refs=[evidence.evidence_id],
                )
            )

        # Memory pressure is direct: use the absolute available-memory level,
        # not just the percentage. Disk write faults also move page cache and
        # can show a memory drop, so require the low absolute floor.
        if secondary_anomaly and secondary_metric.startswith(
            "node_memory_MemAvailable_bytes"
        ):
            try:
                spct = abs(float(secondary_anomaly.get("change_pct") or 0))
            except (TypeError, ValueError):
                spct = 0.0
            primary_direction = str(anomaly.get("direction") or "") if anomaly else ""
            if (
                primary_direction == "up"
                and spct >= 20
                and secondary_min is not None
                and secondary_min < 1_800_000_000
            ):
                deterministic_claims.append(
                    Claim(
                        claim_id="telemetry-memory",
                        statement=(
                            f"可用内存 {secondary_metric} 最低降至 "
                            f"{secondary_min:.0f} 字节（相对基线变化 {spct:.1f}%），"
                            "绝对余量已低于 800MB，指向真实内存压力"
                        ),
                        confidence=0.90,
                        evidence_refs=[secondary_evidence_id],
                    )
                )

        prompt += (
            f"\n可用证据:\n{ctx.evidence_block()}\n\n"
            '请输出可能的根因候选（JSON: {"claims": [...]}）。'
            "每条 claim 必须带 evidence_refs。"
        )
        claims = _ask_claims(ctx, prompt, "telemetry") + deterministic_claims

        anomalies: list[Anomaly] = []
        if anomaly:
            try:
                onset = datetime.fromisoformat(
                    str(anomaly.get("t")).replace("Z", "+00:00")
                )
            except (TypeError, ValueError):
                onset = None
            if onset is not None:
                change = abs(float(anomaly.get("change_pct") or 0))
                anomalies.append(
                    Anomaly(
                        metric=metric,
                        onset=onset,
                        severity="high" if change >= 50 else "mid",
                        evidence_ref=evidence.evidence_id,
                    )
                )
        anomalies.extend(extra_anomalies)
        return AgentOutput(role=self.role, claims=claims, anomalies=anomalies)


# ---------------------------------------------------------------------- log
class LogAgent:
    role = AgentRole.LOG

    def run(self, ctx: AgentContext) -> AgentOutput:
        result = ctx.tools.call(
            "loki.query",
            service=ctx.service,
            # Real journald logs mostly contain INFO access lines; catch the
            # error families without case sensitivity. `re:` is understood by
            # the Loki MCP server; devstack ignores the filter.
            pattern="re:(?i)(error|exception|traceback|timeout|refused|fail)",
            window=ctx.window,
        )
        if not result.ok:
            return AgentOutput(role=self.role, notes=f"日志不可得: {result.error}")

        data = result.data
        # Keep a multi-line sample, not just the newest line. A single line is
        # often an unrelated log that merely happens to be most recent, which
        # both hides the real signature from reviewers and breaks traceability
        # (a claim cannot cite a fact the evidence does not contain).
        raw_lines = data.get("lines") or []
        sample = " | ".join(
            str(item.get("line", ""))[:80] for item in raw_lines[:5] if item.get("line")
        ) or "（无样本）"
        evidence = ctx.add_evidence(
            Evidence(
                evidence_id=ctx.next_id("LOG"),
                source=SourceKind.LOG,
                uri=f"loki://query?service={ctx.service}&pattern=error",
                excerpt=redact(
                    data_mode_note(data) + f"{data.get('count')} 条错误；样本: {sample}"
                ),
                redaction_level="high",
                data_mode=evidence_mode(data),
            )
        )

        prompt = (
            f"服务: {ctx.service}\n日志样本: {json.dumps(data, ensure_ascii=False)}\n\n"
            f"可用证据:\n{ctx.evidence_block()}\n\n"
            "请输出日志揭示的根因候选（JSON: {\"claims\": [...]}）。"
        )
        claims = _ask_claims(ctx, prompt, "log")
        return AgentOutput(role=self.role, claims=claims)


# ------------------------------------------------------------------- change
class ChangeAgent:
    role = AgentRole.CHANGE

    def run(self, ctx: AgentContext) -> AgentOutput:
        changes = ctx.tools.call("deploy.changes", service=ctx.service, window="2h")
        topology = ctx.tools.call("cmdb.topology", service=ctx.service)
        if not changes.ok:
            return AgentOutput(role=self.role, notes=f"变更记录不可得: {changes.error}")

        payload = changes.data or {}
        items = payload.get("changes") or []
        excerpt = "; ".join(
            f"{c.get('at')} {c.get('change_id')} {c.get('summary')}" for c in items
        ) or "无变更"
        evidence = ctx.add_evidence(
            Evidence(
                evidence_id=ctx.next_id("CHG"),
                source=SourceKind.CHANGE,
                uri=f"deploy://changes?service={ctx.service}",
                excerpt=redact(data_mode_note(payload) + excerpt),
                redaction_level="low",
                data_mode=evidence_mode(payload),
            )
        )
        if topology.ok:
            ctx.add_evidence(
                Evidence(
                    evidence_id=ctx.next_id("TOP"),
                    source=SourceKind.TOPOLOGY,
                    uri=f"cmdb://topology/{ctx.service}",
                    excerpt=redact(
                        data_mode_note(topology.data)
                        + f"下游: {topology.data.get('downstream')}"
                    ),
                    redaction_level="low",
                    data_mode=evidence_mode(topology.data),
                )
            )

        prompt = (
            f"服务: {ctx.service}\n变更: {json.dumps(payload, ensure_ascii=False)}\n"
            f"拓扑: {json.dumps(topology.data if topology.ok else {}, ensure_ascii=False)}\n\n"
            f"可用证据:\n{ctx.evidence_block()}\n\n"
            "请判断哪一条变更最可能引发故障，输出根因候选（JSON: {\"claims\": [...]}）。"
        )
        claims = _ask_claims(ctx, prompt, "change")

        # Emit timeline anchors from the actual change records. Hardcoding this
        # in the orchestrator previously produced a one-day gap between the
        # deploy and the latency spike.
        timeline: list[TimelineEvent] = []
        for change in items:
            raw_at = str(change.get("at", ""))
            try:
                at = datetime.fromisoformat(raw_at.replace("Z", "+00:00"))
            except ValueError:
                continue
            timeline.append(
                TimelineEvent(
                    at=at,
                    kind="deploy" if change.get("kind") == "deploy" else "config_change",
                    title=str(change.get("summary") or change.get("change_id") or "变更"),
                    evidence_ref=evidence.evidence_id,
                )
            )
        return AgentOutput(role=self.role, claims=claims, timeline=timeline)


# ------------------------------------------------------------------- impact
class ImpactAgent:
    """Deterministic impact aggregation; no LLM call.

    Impact numbers must be traceable to a PromQL query, so this agent only
    collects and formats what the tool returned. It refuses mock/fixture data
    instead of producing a plausible-looking number from a demo fixture.
    """

    role = AgentRole.IMPACT

    def run(self, ctx: AgentContext) -> AgentOutput:
        result = ctx.tools.call(
            "prometheus.impact", service=ctx.service, window=ctx.window
        )
        if not result.ok:
            return AgentOutput(role=self.role, notes=f"影响面不可得: {result.error}")
        data = result.data or {}
        mode = evidence_mode(data)
        if mode != "real":
            return AgentOutput(
                role=self.role,
                notes=f"影响面统计仅对真实数据源启用（当前 {mode}，已跳过）",
            )
        if data.get("no_data") or not data.get("summary"):
            return AgentOutput(
                role=self.role,
                notes="影响面不可得: Prometheus 中没有该服务的 HTTP 指标样本",
            )

        ctx.impact = data
        summary = data.get("summary") or {}
        impact = data.get("impact") or {}
        excerpt = (
            f"请求峰值 {summary.get('requests_incident', 'n/a')} req/s"
            f"（基线 {summary.get('requests_baseline', 'n/a')}，"
            f"变化 {impact.get('qps_change_pct', 'n/a')}%）；"
            f"5xx 峰值 {summary.get('error_rate_incident', 'n/a')}"
            f"（基线 {summary.get('error_rate_baseline', 'n/a')}）；"
            f"P95 峰值 {summary.get('p95_incident', 'n/a')} ms"
            f"（基线 {summary.get('p95_baseline', 'n/a')}）；"
            f"可用监控目标 {summary.get('targets_up_incident', 'n/a')}"
        )
        business = data.get("business_impact") or {}
        if business.get("configured"):
            excerpt += (
                f"；受影响用户估算 {business.get('estimated_affected_users')}，"
                f"预计损失 {business.get('estimated_cost')} "
                f"{business.get('currency', 'CNY')}（配置系数估算）"
            )
        queries = "; ".join(
            f"{name}: {item.get('expr')}"
            for name, item in (data.get("queries") or {}).items()
        )
        ctx.add_evidence(
            Evidence(
                evidence_id=ctx.next_id("IMP"),
                source=SourceKind.IMPACT,
                uri=f"prometheus://impact?service={ctx.service}&window={ctx.window}",
                excerpt=redact(f"{excerpt}；查询语句 {queries}"),
                redaction_level="low",
                data_mode="real",
            )
        )
        return AgentOutput(role=self.role, notes="")


# ------------------------------------------------------------------- writer
class WriterAgent:
    role = AgentRole.WRITER

    def run(self, ctx: AgentContext, timeline: list[TimelineEvent], top_claims: list[Claim]) -> AgentOutput:
        rows = "\n".join(
            f"| {e.at.strftime('%H:%M:%S')} | {e.kind} | {e.title} | {e.evidence_ref} |"
            for e in sorted(timeline, key=lambda x: x.at)
        ) or "| - | - | 无事件 | - |"

        # Evidence trust is a first-class column now: a reviewer must be able to
        # see that a claim rests on mock data without reading every excerpt.
        mock_ids = {e.evidence_id for e in ctx.evidence.values() if e.data_mode != "real"}
        mock_warning = ""
        if mock_ids:
            mock_warning = (
                "> **数据可信度警告**：本报告包含 Mock / 降级数据（"
                + "、".join(sorted(mock_ids))
                + "），仅用于流程演示，**不得作为真实故障结论对外引用**。\n\n"
            )

        claims_md = "\n".join(
            f"{i+1}. {c.statement}（置信度 {c.confidence:.2f}，证据 {', '.join(c.evidence_refs)}）"
            + (
                "　⚠️ 含 Mock/降级证据，仅供演示"
                if any(ref in mock_ids for ref in c.evidence_refs)
                else ""
            )
            for i, c in enumerate(top_claims)
        ) or "（无有效结论）"

        def evidence_line(e: Evidence) -> str:
            tag = {"mock": "[MOCK] ", "fixture": "[FIXTURE] "}.get(e.data_mode, "")
            return f"- `{e.evidence_id}` {tag}{e.source.value} {e.uri}"

        impact_md = "（影响面不可得：没有真实 HTTP 指标，或该服务未接 Prometheus）"
        if ctx.impact:
            summary = ctx.impact.get("summary") or {}
            impact = ctx.impact.get("impact") or {}
            business = ctx.impact.get("business_impact") or {}
            qps_change = impact.get("qps_change_pct")
            p95_change = impact.get("p95_ms_change_pct")
            business_md = ""
            if business.get("configured"):
                business_md = (
                    f"\n| 受影响用户（估算） | {business.get('estimated_affected_users', 0)} | - | - |\n"
                    f"| 预计损失（{business.get('currency', 'CNY')}，估算） | "
                    f"{business.get('estimated_cost', 0)} | - | - |\n"
                    f"\n> 业务口径来自服务目录配置：`{business.get('assumptions')}`；"
                    "这是估算，不是财务确认数字。\n"
                )
            else:
                business_md = (
                    "\n> 业务影响未配置：在服务目录绑定中填写 "
                    "`users_per_request` / `cost_per_minute` 后才会输出估算用户与金额。\n"
                )
            impact_md = (
                "| 指标 | 故障窗口 | 基线 | 变化 |\n"
                "|---|---|---|---|\n"
                f"| 请求峰值 (req/s) | {summary.get('requests_incident', 'n/a')} | "
                f"{summary.get('requests_baseline', 'n/a')} | "
                f"{qps_change if qps_change is not None else 'n/a'}% |\n"
                f"| 5xx 错误率峰值 | {summary.get('error_rate_incident', 'n/a')} | "
                f"{summary.get('error_rate_baseline', 'n/a')} | "
                f"{impact.get('error_rate_delta', 'n/a')} |\n"
                f"| P95 峰值 (ms) | {summary.get('p95_incident', 'n/a')} | "
                f"{summary.get('p95_baseline', 'n/a')} | "
                f"{p95_change if p95_change is not None else 'n/a'}% |\n"
                f"| 可用监控目标 | {summary.get('targets_up_incident', 'n/a')} | "
                f"{summary.get('targets_up_baseline', 'n/a')} | - |\n"
                + business_md
                + f"\n统计窗口：{ctx.impact.get('window', ctx.window)}；"
                f"基线为紧邻的等长窗口。查询语句：\n"
                + "\n".join(
                    f"- `{name}`: `{item.get('expr', '')}`"
                    for name, item in (ctx.impact.get("queries") or {}).items()
                )
            )

        report = (
            "# 故障复盘初稿\n\n"
            + mock_warning
            + f"**服务**：{ctx.service}　**时间窗**：{ctx.window}\n\n"
            "## 一、时间线\n\n"
            "| 时间 | 类型 | 事件 | 证据 |\n|---|---|---|---|\n"
            f"{rows}\n\n"
            "## 二、根因候选\n\n"
            f"{claims_md}\n\n"
            "## 三、影响面\n\n"
            f"{impact_md}\n\n"
            "## 四、证据清单\n\n"
            + "\n".join(evidence_line(e) for e in ctx.evidence.values())
            + "\n\n## 五、成本\n\n"
            + self._cost_md(ctx)
            + "\n\n## 六、待补充\n\n"
            "- 改进项与责任人（待人工填写）\n"
        )
        # The writer does not decide; it only renders. No claims are produced,
        # so nothing it emits can bypass evidence validation.
        return AgentOutput(role=self.role, notes=report)

    @staticmethod
    def _cost_md(ctx: AgentContext) -> str:
        usage = ctx.usage or {}
        cost = ctx.cost or {}
        if not usage.get("calls"):
            return "（无模型调用记录）"
        lines = [
            "| 项 | 值 |",
            "|---|---|",
            f"| 模型调用次数 | {usage.get('calls', 0)} |",
            f"| Prompt tokens | {usage.get('prompt_tokens', 0)} |",
            f"| Completion tokens | {usage.get('completion_tokens', 0)} |",
            f"| Total tokens | {usage.get('total_tokens', 0)} |",
        ]
        if cost.get("configured"):
            lines.append(
                f"| 估算成本 | {cost.get('estimated_cost', 0)} "
                f"{cost.get('currency', 'CNY')} |"
            )
        else:
            lines.append("| 估算成本 | 单价未配置（LLM_COST_PER_1K_*） |")
        steps = cost.get("step_usage") or {}
        if steps:
            lines += ["", "| 步骤 | Prompt | Completion |", "|---|---|---|"]
            for step, item in steps.items():
                lines.append(
                    f"| {step} | {item.get('prompt_tokens', 0)} | "
                    f"{item.get('completion_tokens', 0)} |"
                )
        lines.append("")
        lines.append("> 单价来自环境变量；token 数来自模型响应 usage，不接受估算猜测。")
        return "\n".join(lines)


# ----------------------------------------------------------------- reviewer
class ReviewerAgent:
    role = AgentRole.REVIEWER

    def run(self, ctx: AgentContext, claims: list[Claim]) -> tuple[AgentOutput, list[str]]:
        known = set(ctx.evidence)
        issues: list[str] = []
        kept: list[Claim] = []

        for c in claims:
            missing = [r for r in c.evidence_refs if r not in known]
            if missing:
                issues.append(f"{c.claim_id}: 引用了不存在的证据 {missing}")
                continue
            if c.confidence < ctx.settings.conflict_min_confidence:
                issues.append(f"{c.claim_id}: 置信度 {c.confidence:.2f} 低于阈值")
                continue
            kept.append(c)

        prompt = (
            f"待核查结论: {json.dumps([c.model_dump(mode='json') for c in kept], ensure_ascii=False)}\n"
            f"可用证据:\n{ctx.evidence_block()}\n\n"
            '请输出 {"verdict": "accept|reject", "issues": []} 的 JSON。'
        )
        raw = ctx.llm.chat_json(SYSTEM_BASE, prompt)
        verdict = raw.get("verdict", "accept")
        if verdict == "reject":
            issues.append("reviewer rejected the claim set")

        return AgentOutput(role=self.role, claims=kept, notes=json.dumps(issues, ensure_ascii=False)), issues


def _degraded_note(data: Any) -> str:
    """Backward-compatible alias for the shared mock/fixture marker."""
    return data_mode_note(data)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
