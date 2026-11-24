"""M2 tests: contracts, tool guards, conflict resolution, orchestration.

Run: python -m unittest discover -s services/core/tests -v
No third-party dependencies beyond pydantic/httpx (already installed).
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pydantic import ValidationError  # noqa: E402

from app.agents import AgentContext, ImpactAgent, WriterAgent  # noqa: E402
from app.config import Settings  # noqa: E402
from app.contracts import (  # noqa: E402
    AgentOutput,
    AgentRole,
    Claim,
    ContractViolation,
    Evidence,
    SourceKind,
    validate_agent_output,
)
from app.lease import LeaseManager  # noqa: E402
from app.llm import LLMConfig, ModelClient  # noqa: E402
from app.orchestrator import LoopDetected, Orchestrator, resolve_conflicts  # noqa: E402
from app.service import StepStatus, TaskService  # noqa: E402
from app.store import EventStore  # noqa: E402
from app.tools import (  # noqa: E402
    SSRFBlocked,
    ToolResult,
    assert_url_allowed,
    build_default_registry,
    redact,
)
from app.trace import build_trace  # noqa: E402


def make_evidence(eid: str, source: SourceKind = SourceKind.METRIC) -> Evidence:
    return Evidence(
        evidence_id=eid,
        source=source,
        uri=f"test://{eid}",
        excerpt="sample",
    )


def make_claim(cid: str, refs: list[str], conf: float = 0.8) -> Claim:
    return Claim(
        claim_id=cid,
        statement="订单服务连接池下调导致支付链路超时",
        confidence=conf,
        evidence_refs=refs,
    )


class TestContracts(unittest.TestCase):
    def test_claim_without_evidence_is_rejected(self):
        with self.assertRaises(ValidationError):
            Claim(
                claim_id="c1",
                statement="某个没有证据的结论",
                confidence=0.9,
                evidence_refs=[],
            )

    def test_blank_evidence_refs_rejected(self):
        with self.assertRaises(ValidationError):
            Claim(
                claim_id="c1",
                statement="结论结论结论",
                confidence=0.9,
                evidence_refs=["  ", ""],
            )

    def test_unknown_evidence_is_dropped(self):
        out = AgentOutput(role=AgentRole.LOG, claims=[make_claim("c1", ["E-NOT-EXIST"])])
        with self.assertRaises(ContractViolation):
            validate_agent_output(out, known_evidence={"E-1"})

    def test_mixed_claims_keep_only_supported(self):
        out = AgentOutput(
            role=AgentRole.LOG,
            claims=[make_claim("c1", ["E-1"]), make_claim("c2", ["E-404"])],
        )
        cleaned, rejections = validate_agent_output(out, known_evidence={"E-1"})
        self.assertEqual([c.claim_id for c in cleaned.claims], ["c1"])
        self.assertEqual(len(rejections), 1)

    def test_evidence_digest_is_stable(self):
        a = make_evidence("E-1")
        b = make_evidence("E-1")
        self.assertEqual(a.digest, b.digest)


class TestToolGuards(unittest.TestCase):
    def test_ssrf_blocks_unknown_host(self):
        with self.assertRaises(SSRFBlocked):
            assert_url_allowed("http://169.254.169.254/latest/meta-data", ("localhost",))

    def test_ssrf_allows_allowlisted_subdomain(self):
        assert_url_allowed("http://api.prometheus:9090/x", ("prometheus",))

    def test_tool_call_with_bad_url_returns_error_not_exception(self):
        registry = build_default_registry()
        result = registry.call("kb.search_postmortems", query="http://evil.example.com/steal")
        self.assertFalse(result.ok)
        self.assertIn("SSRFBlocked", result.error)

    def test_redaction_masks_sensitive_tokens(self):
        text = "user=13800138000 mail=a.b@corp.com host=pay-gw.corp ip=10.1.2.3"
        masked = redact(text)
        for leaked in ("13800138000", "a.b@corp.com", "pay-gw.corp", "10.1.2.3"):
            self.assertNotIn(leaked, masked)

    def test_rate_limit_trips(self):
        import dataclasses

        registry = build_default_registry()
        # shrink the configured limit to 1/min for this tool
        registry._specs["loki.query"] = dataclasses.replace(  # noqa: SLF001
            registry._specs["loki.query"], rate_limit_per_min=1  # noqa: SLF001
        )
        first = registry.call("loki.query", service="order")
        second = registry.call("loki.query", service="order")
        self.assertTrue(first.ok)
        self.assertFalse(second.ok)
        self.assertIn("RateLimited", second.error)

    def test_audit_records_every_call(self):
        registry = build_default_registry()
        registry.call("cmdb.topology", service="order")
        self.assertEqual(len(registry.audit), 1)
        self.assertTrue(registry.audit[0].ok)


class TestConflictResolution(unittest.TestCase):
    def test_prefers_stronger_evidence(self):
        ev = {
            "E-1": make_evidence("E-1", SourceKind.METRIC),
            "E-2": make_evidence("E-2", SourceKind.CHANGE),
            "E-3": make_evidence("E-3", SourceKind.LOG),
        }
        # distinct statements so they are not merged as near-duplicates
        strong = Claim(
            claim_id="strong",
            statement="订单服务连接池下调导致支付链路排队超时",
            confidence=0.70,
            evidence_refs=["E-1", "E-2", "E-3"],
        )
        weak = Claim(
            claim_id="weak",
            statement="数据库主从延迟导致读请求堆积",
            confidence=0.90,
            evidence_refs=["E-1"],
        )
        result = resolve_conflicts([weak, strong], ev, min_confidence=0.3)
        self.assertEqual(result.ranked[0].claim_id, "strong")
        self.assertFalse(result.needs_human)

    def test_duplicate_conclusions_union_their_evidence(self):
        """Two agents independently reaching the same conclusion is
        corroboration — the merged claim must carry both evidence sources."""
        ev = {
            "E-TLM-001": make_evidence("E-TLM-001", SourceKind.METRIC),
            "E-CHG-001": make_evidence("E-CHG-001", SourceKind.CHANGE),
        }
        from_metric = Claim(
            claim_id="m", statement="订单服务连接池下调导致支付超时",
            confidence=0.70, evidence_refs=["E-TLM-001"],
        )
        from_change = Claim(
            claim_id="c", statement="订单服务连接池下调导致支付超时",
            confidence=0.85, evidence_refs=["E-CHG-001"],
        )
        result = resolve_conflicts([from_metric, from_change], ev, min_confidence=0.3)
        self.assertEqual(len(result.ranked), 1, "近似结论应合并为一条")
        merged = result.ranked[0]
        self.assertEqual(set(merged.evidence_refs), {"E-TLM-001", "E-CHG-001"})
        self.assertEqual(merged.confidence, 0.85, "合并后取较高置信度")

    def test_confidence_outranks_raw_source_count(self):
        """A decoy that merely cites more sources must not beat a confident
        conclusion. Regression for the demo where 0.41 (3 sources) outranked
        0.82 (2 sources)."""
        ev = {
            "E-1": make_evidence("E-1", SourceKind.METRIC),
            "E-2": make_evidence("E-2", SourceKind.CHANGE),
            "E-3": make_evidence("E-3", SourceKind.LOG),
        }
        decoy = Claim(
            claim_id="decoy",
            statement="支付网关实例数不足扩容滞后",
            confidence=0.41,
            evidence_refs=["E-1", "E-2", "E-3"],
        )
        real = Claim(
            claim_id="real",
            statement="订单服务连接池下调导致排队超时",
            confidence=0.82,
            evidence_refs=["E-1", "E-2"],
        )
        result = resolve_conflicts([decoy, real], ev, min_confidence=0.3)
        self.assertEqual(result.ranked[0].claim_id, "real")
        self.assertEqual(result.ranked[0].root_cause_rank, 1)

    def test_flags_human_on_tie(self):
        ev = {"E-1": make_evidence("E-1"), "E-2": make_evidence("E-2")}
        a = Claim(claim_id="a", statement="支付网关扩容滞后导致超时",
                  confidence=0.80, evidence_refs=["E-1"])
        b = Claim(claim_id="b", statement="数据库慢查询堆积导致超时",
                  confidence=0.81, evidence_refs=["E-2"])
        result = resolve_conflicts([a, b], ev, min_confidence=0.3)
        self.assertTrue(result.needs_human)
        self.assertIn("接近", result.reason)

    def test_drops_below_threshold(self):
        ev = {"E-1": make_evidence("E-1")}
        weak = make_claim("w", ["E-1"], 0.1)
        result = resolve_conflicts([weak], ev, min_confidence=0.35)
        self.assertEqual(result.ranked, [])
        self.assertTrue(result.needs_human)

    def test_negative_claim_does_not_win_rank1(self):
        """The reviewer used to see "暂无证据支持应用异常" win rank1 just
        because its self-reported confidence was high. It is an audit note,
        not a root cause; the CPU candidate must rank first."""
        ev = {"E-1": make_evidence("E-1"), "E-2": make_evidence("E-2")}
        negative = Claim(
            claim_id="neg",
            statement="日志侧错误关键字匹配结果为 0 条且无样本，无法从日志直接定位根因",
            confidence=0.9, evidence_refs=["E-1"],
        )
        cpu = Claim(
            claim_id="cpu",
            statement="node_load1 从基线骤升，节点出现 CPU 资源饱和",
            confidence=0.72, evidence_refs=["E-2"],
        )
        result = resolve_conflicts([negative, cpu], ev, min_confidence=0.35)
        self.assertEqual([c.claim_id for c in result.ranked], ["cpu"])
        self.assertTrue(result.dropped)
        self.assertIn("非因果", result.dropped[0])

        exculpatory = Claim(
            claim_id="neg2",
            statement="磁盘 IO 利用率极低，不足以解释本次负载峰值",
            confidence=0.88, evidence_refs=["E-1"],
        )
        negative_change = Claim(
            claim_id="neg3",
            statement="变更窗口内未记录任何部署或配置变更，故障无法归因于变更事件",
            confidence=0.9, evidence_refs=["E-1"],
        )
        result3 = resolve_conflicts([negative_change, cpu], ev, min_confidence=0.35)
        self.assertEqual([c.claim_id for c in result3.ranked], ["cpu"])
        result2 = resolve_conflicts([exculpatory, cpu], ev, min_confidence=0.35)
        self.assertEqual([c.claim_id for c in result2.ranked], ["cpu"])


class TestOrchestratorEndToEnd(unittest.TestCase):
    def build(self, settings: Settings | None = None) -> tuple[Orchestrator, TaskService, str]:
        store = EventStore(":memory:")
        leases = LeaseManager(store._conn, store._lock)
        service = TaskService(store, leases)
        s = settings or Settings()
        llm = ModelClient(LLMConfig(base_url="", api_key="", model=""))  # forced mock
        orchestrator = Orchestrator(service, llm, build_default_registry(s), s)
        task_id = service.create_task("订单服务超时复盘")
        return orchestrator, service, task_id

    def test_trace_is_projected_from_events(self):
        orch, service, task_id = self.build()
        orch.run(task_id, "order-service")
        events = service._store.load(task_id)  # noqa: SLF001
        trace = build_trace(events, task_id)
        self.assertEqual(len(trace["trace_id"]), 32)
        self.assertGreaterEqual(len(trace["spans"]), 5)
        names = [span["name"] for span in trace["spans"]]
        self.assertIn("analysis task", names)
        self.assertTrue(any(name.startswith("step telemetry") for name in names))
        for span in trace["spans"]:
            self.assertEqual(len(span["span_id"]), 16)
            self.assertIn(span["status"], {"OK", "ERROR", "UNSET"})

    def test_full_run_produces_evidence_backed_report(self):
        orch, service, task_id = self.build()
        report = orch.run(task_id, "order-service", "30m")

        self.assertGreaterEqual(len(report.evidence), 3, "应至少采集到 3 条证据")
        self.assertTrue(report.claims, "应产出至少一条结论")
        for claim in report.claims:
            for ref in claim.evidence_refs:
                self.assertIn(ref, report.evidence, f"{ref} 必须是真实证据")
        self.assertGreater(report.usage.get("calls", 0), 0)
        self.assertIn("telemetry", report.step_usage)
        self.assertIn("## 五、成本", report.report_md)
        self.assertFalse(report.cost.get("configured"))
        self.assertIn("故障复盘初稿", report.report_md)
        self.assertIn("时间线", report.report_md)

        view = service.view(task_id)
        for step_id in ("telemetry", "log", "change", "review"):
            self.assertEqual(
                view.step_status(step_id), StepStatus.SUCCEEDED, f"{step_id} 应成功"
            )

    def test_events_are_append_only_and_monotonic(self):
        orch, service, task_id = self.build()
        orch.run(task_id, "order-service")
        seqs = [e.seq for e in service._store.load(task_id)]  # noqa: SLF001
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))

    def test_no_claim_survives_without_evidence(self):
        orch, service, task_id = self.build()
        report = orch.run(task_id, "order-service")
        for claim in report.claims:
            self.assertTrue(claim.evidence_refs)
            self.assertTrue(all(r in report.evidence for r in claim.evidence_refs))

    def test_old_plan_without_impact_step_still_runs(self):
        """Tasks planned before M9 have four steps; the orchestrator must skip
        the missing impact step instead of trying to lease a non-existent one."""
        orch, service, task_id = self.build()
        legacy = [
            {"step_id": "telemetry", "role": "telemetry"},
            {"step_id": "log", "role": "log"},
            {"step_id": "change", "role": "change"},
            {"step_id": "review", "role": "reviewer"},
        ]
        service.plan(task_id, legacy)
        report = orch.run(task_id, "order-service")
        self.assertIsInstance(report.impact, dict)
        self.assertEqual(report.impact, {})

    def test_loop_detection_replans_local_branch(self):
        settings = Settings(max_replans=1)
        orch, service, task_id = self.build(settings)

        from app import orchestrator as mod

        class Boom(Exception):
            pass

        def exploding_run(self, ctx):  # noqa: ANN001
            raise Boom("tool explosion")

        original = mod.TelemetryAgent.run
        mod.TelemetryAgent.run = exploding_run
        try:
            report = orch.run(task_id, "order-service")
        finally:
            mod.TelemetryAgent.run = original

        view = service.view(task_id)
        self.assertEqual(view.step_status("telemetry"), StepStatus.FAILED)
        self.assertIn("telemetry", report.failed_steps)
        self.assertIn("impact", report.skipped_steps)
        self.assertIn("review", report.skipped_steps)
        self.assertEqual(report.status, "WAITING_HUMAN")


class TestNegativeControlSignal(unittest.TestCase):
    def test_disk_relative_jump_needs_absolute_utilization(self):
        from app.agents import _extra_signal_significant

        self.assertFalse(_extra_signal_significant(
            "node_disk_io_time_seconds_total", 363.6, 0.004
        ))
        self.assertTrue(_extra_signal_significant(
            "node_disk_io_time_seconds_total", 10880.0, 0.538
        ))


class TestClaimIdentityAndRanking(unittest.TestCase):
    """Regressions found while running against the real model."""

    def test_claim_ids_are_namespaced_by_role(self):
        """Every agent emits "c1"; ids must not collide across roles or any
        id-keyed map (scores, citations) silently corrupts."""
        from app.agents import AgentContext, _parse_claims

        ctx = AgentContext(
            task_id="t", service="s", window="30m",
            tools=build_default_registry(), llm=ModelClient(LLMConfig()),
            settings=Settings(),
        )
        raw = {"claims": [{"id": "c1", "statement": "连接池耗尽导致超时", "confidence": 0.8,
                           "evidence_refs": ["E-1"]}]}
        log_claims, _ = _parse_claims(raw, ctx, "log")
        change_claims, _ = _parse_claims(raw, ctx, "change")
        self.assertEqual(log_claims[0].claim_id, "log-c1")
        self.assertEqual(change_claims[0].claim_id, "change-c1")
        self.assertNotEqual(log_claims[0].claim_id, change_claims[0].claim_id)

    def test_report_scores_are_monotonic(self):
        store = EventStore(":memory:")
        service = TaskService(store, LeaseManager(store._conn, store._lock))
        orch = Orchestrator(service, ModelClient(LLMConfig()),
                            build_default_registry(Settings()), Settings())
        task_id = service.create_task("t")
        report = orch.run(task_id, "order-service")

        ordered = [report.claim_scores.get(c.claim_id, 0.0) for c in report.claims]
        self.assertTrue(ordered, "至少应有一条结论")
        self.assertEqual(ordered, sorted(ordered, reverse=True),
                         f"分数必须随 rank 递减，实际 {ordered}")

    def test_claim_ids_unique_in_report(self):
        store = EventStore(":memory:")
        service = TaskService(store, LeaseManager(store._conn, store._lock))
        orch = Orchestrator(service, ModelClient(LLMConfig()),
                            build_default_registry(Settings()), Settings())
        task_id = service.create_task("t")
        report = orch.run(task_id, "order-service")
        ids = [c.claim_id for c in report.claims]
        self.assertEqual(len(ids), len(set(ids)), f"claim_id 重复: {ids}")

    def test_timeline_uses_real_evidence_times(self):
        """A hardcoded timeline anchor used to produce a one-day gap between
        the deploy and the latency spike."""
        from datetime import datetime, timedelta, timezone

        store = EventStore(":memory:")
        service = TaskService(store, LeaseManager(store._conn, store._lock))
        orch = Orchestrator(service, ModelClient(LLMConfig()),
                            build_default_registry(Settings()), Settings())
        task_id = service.create_task("t")
        report = orch.run(task_id, "order-service")

        self.assertTrue(report.timeline, "时间线不应为空")
        horizon = datetime.now(timezone.utc) - timedelta(hours=2)
        for event in report.timeline:
            self.assertGreater(event.at, horizon,
                               f"时间线锚点 {event.at} 过旧，像是硬编码")
        # deploy must precede the metric spike
        self.assertLess(report.timeline[0].at, report.timeline[-1].at)


class _ImpactTools:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.audit: list = []

    def call(self, name, **kwargs):
        return ToolResult(True, name, data=self.data)


class TestImpactAgent(unittest.TestCase):
    def context(self, data: dict) -> AgentContext:
        ctx = AgentContext(
            task_id="t", service="ecs-copilot", window="30m",
            tools=_ImpactTools(data),  # type: ignore[arg-type]
            llm=ModelClient(LLMConfig()), settings=Settings(),
        )
        return ctx

    def test_real_impact_becomes_evidence_and_report_section(self):
        data = {
            "source": "prometheus", "data_mode": "real", "window": "30m",
            "summary": {"requests_incident": 12.5, "requests_baseline": 10.0,
                        "error_rate_incident": 0.2, "error_rate_baseline": 0.0,
                        "p95_incident": 850.0, "p95_baseline": 240.0,
                        "targets_up_incident": 2, "targets_up_baseline": 2},
            "impact": {"qps_change_pct": 25.0, "error_rate_delta": 0.2,
                       "p95_ms_change_pct": 254.2},
            "queries": {"requests": {"expr": "sum(rate(requests[1m]))"}},
        }
        ctx = self.context(data)
        ImpactAgent().run(ctx)
        self.assertEqual(ctx.impact["summary"]["p95_incident"], 850.0)
        evidence = ctx.evidence["E-IMP-001"]
        self.assertEqual(evidence.source.value, "impact")
        self.assertEqual(evidence.data_mode, "real")
        self.assertIn("P95", evidence.excerpt)

        report = WriterAgent().run(ctx, [], []).notes
        self.assertIn("## 三、影响面", report)
        self.assertIn("P95 峰值", report)
        self.assertIn("sum(rate(requests[1m]))", report)

    def test_mock_impact_is_skipped_and_not_rendered_as_real(self):
        data = {
            "source": "prometheus", "data_mode": "mock",
            "summary": {"requests_incident": 99},
            "queries": {},
        }
        ctx = self.context(data)
        out = ImpactAgent().run(ctx)
        self.assertIn("仅对真实数据源启用", out.notes)
        self.assertEqual(ctx.impact, {})
        self.assertEqual(ctx.evidence, {})


class TestAgentContext(unittest.TestCase):
    def test_evidence_ids_are_unique(self):
        ctx = AgentContext(
            task_id="t", service="s", window="30m",
            tools=build_default_registry(), llm=ModelClient(LLMConfig()),
            settings=Settings(),
        )
        a = ctx.add_evidence(make_evidence(ctx.next_id("TLM")))
        b = ctx.add_evidence(make_evidence(ctx.next_id("TLM")))
        self.assertEqual(a.evidence_id, "E-TLM-001")
        self.assertEqual(b.evidence_id, "E-TLM-002")


if __name__ == "__main__":
    unittest.main(verbosity=2)
