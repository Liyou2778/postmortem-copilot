"""Evaluation scenarios: labelled faults the analyser must localise.

Ground truth is defined as **the evidence signature**, not as a string the model
must reproduce. A prediction counts as correct when the cited evidence actually
carries the injected symptom. That rule is language-independent and cannot be
gamed by a model that parrots keywords without citing support.

60 scenarios = 9 fault types x the services below.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SERVICES = (
    "order-service",
    "pay-service",
    "cart-service",
    "inventory-service",
    "user-service",
    "search-service",
    "coupon-service",
)


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    fault_type: str
    service: str
    # Substrings that identify the injected fault inside evidence excerpts.
    markers: tuple[str, ...]
    # Concept terms the claim statement must name.
    terms: tuple[str, ...]
    description: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


# Per fault type: (evidence-side markers, statement-side concept terms).
#
# Two-sided matching is deliberate. Evidence-only matching is trivially gamed by
# a model that cites every available reference; statement-only matching is gamed
# by keyword regurgitation. A prediction must both *cite evidence carrying the
# signature* and *name the corresponding concept*, which is what a human
# reviewer would check.
FAULT_SIGNATURES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "deploy_regression": (
        ("连接池上限", "connection pool exhausted", "v2.31"),
        ("连接池", "pool", "200", "发布", "变更"),
    ),
    "resource_saturation": (
        ("cpu throttling", "memory pressure", "request queue full"),
        ("cpu", "资源", "节流", "内存", "饱和", "容量不足"),
    ),    "dependency_timeout": (
        ("context deadline exceeded", "half-open", "inventory"),
        ("依赖", "下游", "超时", "inventory"),
    ),
    "config_drift": (
        ("runtime config", "pool.size", "config version changed"),
        ("配置", "runtime", "漂移", "变更"),
    ),
    "pod_kill": (
        ("liveness probe failed", "container started", "readiness gate closed"),
        ("重启", "pod", "实例", "探针", "容器"),
    ),
    "network_partition": (
        ("i/o timeout", "connection reset by peer", "redis-session"),
        ("网络", "分区", "连接", "超时", "不通"),
    ),
    "dns_error": (
        ("no such host", "dns resolution", "upstream unreachable"),
        ("dns", "解析", "域名", "host"),
    ),
    "io_stall": (
        ("fsync took", "iowait", "database write timeout"),
        ("磁盘", "io", "fsync", "写入", "iowait"),
    ),
    "traffic_spike": (
        # "qps" alone appears in every scenario's QPS evidence, so the marker
        # must be the abnormal *value*, otherwise this fault is ungroundable.
        ("qps=12000", "rate limit engaged", "qps 12000"),
        ("流量", "qps", "容量", "限流", "并发"),
    ),
}

FAULT_ORDER = tuple(FAULT_SIGNATURES)


def build_scenarios(per_fault: int = 7) -> list[Scenario]:
    """9 fault types x N services, capped so the suite stays at 60."""
    scenarios: list[Scenario] = []
    idx = 0
    for fault_type in FAULT_ORDER:
        markers, terms = FAULT_SIGNATURES[fault_type]
        for service in SERVICES[:per_fault]:
            idx += 1
            scenarios.append(
                Scenario(
                    scenario_id=f"S{idx:03d}",
                    fault_type=fault_type,
                    service=service,
                    markers=markers,
                    terms=terms,
                    description=f"{fault_type} on {service}",
                    tags=(fault_type,),
                )
            )
    return scenarios


def scenario_count(per_fault: int = 7) -> int:
    return len(FAULT_ORDER) * min(per_fault, len(SERVICES))
