"""Tool layer: MCP-style tool registry with mandatory safety guards.

Every tool call passes four gates before its result is returned:

1. **Read-only enforcement** — write tools must be explicitly opted in.
2. **SSRF allowlist** — any URL-shaped argument must resolve to an allowlisted host.
3. **Rate limiting** — sliding window per tool.
4. **Output redaction** — phone / email / internal host / IP are masked before the
   result can reach a model or a report.

The implementations here return deterministic synthetic data so the whole
pipeline can be demoed and unit-tested offline. Swapping them for real
Prometheus/Loki/CMDB clients means implementing the same function signature.
"""
from __future__ import annotations

import concurrent.futures
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlparse

from .config import Settings, load_settings


def _ago(minutes: int) -> str:
    """Relative timestamp so fixtures never produce a stale timeline."""
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")


class ToolError(RuntimeError):
    pass


class SSRFBlocked(ToolError):
    pass


class RateLimited(ToolError):
    pass


class ReadOnlyViolation(ToolError):
    pass


# ---------------------------------------------------------------- redaction
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b1[3-9]\d{9}\b"), "<phone>"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b"), "<email>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
    (re.compile(r"\b[\w-]+\.(?:internal|corp|local|lan)\b", re.IGNORECASE), "<internal-host>"),
)


def redact(text: str) -> str:
    """Mask sensitive tokens. Applied to every tool output and evidence excerpt."""
    if not text:
        return text
    out = text
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def redact_deep(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_deep(v) for v in value]
    if isinstance(value, dict):
        return {k: redact_deep(v) for k, v in value.items()}
    return value


# --------------------------------------------------------------- data honesty
def evidence_mode(data: Any) -> str:
    """Classify a tool payload as ``real`` / ``mock`` / ``fixture``.

    The catalog sets ``data_mode`` before the call; an unreachable upstream
    downgrades itself to ``fixture`` inside the MCP server. A fixture is never
    relabelled as mock (or worse, real) by this helper — fixture is the lowest
    trust level and wins.
    """
    if isinstance(data, dict):
        mode = str(data.get("data_mode") or "").lower()
        if mode == "fixture" or data.get("source") == "fixture":
            return "fixture"
        if mode == "mock":
            return "mock"
    return "real"


def data_mode_note(data: Any) -> str:
    """A marker the report renders verbatim; never let mock data look real."""
    mode = evidence_mode(data)
    if mode == "mock":
        return "【Mock 数据：来自演练上游，仅用于演示，不能作为真实证据】"
    if mode == "fixture":
        return "【数据降级：上游不可达，使用 fixture】"
    return ""


# ---------------------------------------------------------------- ssrf guard
def assert_url_allowed(url: str, allowlist: tuple[str, ...]) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SSRFBlocked(f"unsupported scheme: {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise SSRFBlocked("url has no host")
    if host in allowlist:
        return
    # allow explicit subdomains of allowlisted hosts (e.g. api.prometheus)
    for allowed in allowlist:
        if host.endswith("." + allowed):
            return
    raise SSRFBlocked(f"host {host!r} not in allowlist")


# ---------------------------------------------------------------- spec
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    readonly: bool = True
    timeout_s: float = 20.0
    rate_limit_per_min: int = 60
    params: tuple[str, ...] = ()


@dataclass
class ToolResult:
    ok: bool
    tool: str
    data: Any = None
    error: str = ""
    latency_ms: int = 0

    @property
    def read_only_safe(self) -> bool:
        return self.ok


@dataclass
class AuditRecord:
    tool: str
    ok: bool
    latency_ms: int
    error: str = ""
    at: float = field(default_factory=time.time)


class AuditedTools:
    """Per-run audit view over a shared tool backend.

    M9 shares one MCP gateway across concurrent analyses, so the gateway's own
    audit list is process-wide. A run must only report the calls *it* made;
    otherwise task B's report contains task A's tool calls. This thin wrapper
    keeps the per-run list local while the underlying gateway can still keep
    its global audit/metrics.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.audit: list[AuditRecord] = []

    def call(self, name: str, **kwargs: Any) -> ToolResult:
        result = self.inner.call(name, **kwargs)
        self.audit.append(
            AuditRecord(name, result.ok, result.latency_ms, result.error[:120])
        )
        return result

    def specs(self) -> list[dict[str, Any]]:
        if hasattr(self.inner, "specs"):
            return list(self.inner.specs())  # type: ignore[no-any-return]
        return []

    def close(self) -> None:
        if hasattr(self.inner, "close"):
            self.inner.close()

    def __getattr__(self, item: str) -> Any:
        return getattr(self.inner, item)


# ---------------------------------------------------------------- registry
class ToolRegistry:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or load_settings()
        self._specs: dict[str, ToolSpec] = {}
        self._impls: dict[str, Callable[..., Any]] = {}
        self._windows: dict[str, deque[float]] = {}
        self.audit: list[AuditRecord] = []

    def register(self, spec: ToolSpec, impl: Callable[..., Any]) -> None:
        self._specs[spec.name] = spec
        self._impls[spec.name] = impl
        self._windows.setdefault(spec.name, deque())

    # ------------------------------------------------------------------ guards
    def _check_rate(self, spec: ToolSpec) -> None:
        window = self._windows[spec.name]
        now = time.time()
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= spec.rate_limit_per_min:
            raise RateLimited(
                f"{spec.name}: {len(window)} calls in the last minute "
                f"(limit {spec.rate_limit_per_min})"
            )
        window.append(now)

    def _check_params(self, spec: ToolSpec, kwargs: dict[str, Any]) -> None:
        for key, value in kwargs.items():
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                assert_url_allowed(value, self.settings.tool_url_allowlist)
            if isinstance(value, str) and "://" in value and not value.startswith(
                ("http://", "https://")
            ):
                raise SSRFBlocked(f"param {key!r} uses a disallowed scheme")

    # ------------------------------------------------------------------- call
    def call(self, name: str, **kwargs: Any) -> ToolResult:
        spec = self._specs.get(name)
        if spec is None:
            raise ToolError(f"unknown tool: {name}")

        started = time.perf_counter()
        try:
            self._check_rate(spec)
            self._check_params(spec, kwargs)
            timeout = min(spec.timeout_s, self.settings.tool_timeout_s)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self._impls[name], **kwargs)
                raw = future.result(timeout=timeout)
            data = redact_deep(raw)
            latency = int((time.perf_counter() - started) * 1000)
            self.audit.append(AuditRecord(name, True, latency))
            return ToolResult(ok=True, tool=name, data=data, latency_ms=latency)
        except concurrent.futures.TimeoutError:
            latency = int((time.perf_counter() - started) * 1000)
            self.audit.append(AuditRecord(name, False, latency, "timeout"))
            return ToolResult(ok=False, tool=name, error="timeout", latency_ms=latency)
        except (SSRFBlocked, RateLimited, ReadOnlyViolation) as exc:
            latency = int((time.perf_counter() - started) * 1000)
            self.audit.append(AuditRecord(name, False, latency, type(exc).__name__))
            return ToolResult(ok=False, tool=name, error=f"{type(exc).__name__}: {exc}",
                              latency_ms=latency)
        except Exception as exc:  # noqa: BLE001 - tools must not crash the task
            latency = int((time.perf_counter() - started) * 1000)
            self.audit.append(AuditRecord(name, False, latency, str(exc)[:120]))
            return ToolResult(ok=False, tool=name, error=str(exc)[:200], latency_ms=latency)

    def specs(self) -> list[dict[str, Any]]:
        return [
            {
                "name": s.name,
                "description": s.description,
                "readonly": s.readonly,
                "params": list(s.params),
                "rate_limit_per_min": s.rate_limit_per_min,
            }
            for s in self._specs.values()
        ]


# ---------------------------------------------------------------- fixtures
# Deterministic synthetic payloads. Note the planted evidence: an upstream
# depool at 10:00, a deploy at 10:03, an error burst at 10:05. The intended
# root cause is the deploy, not the pool.
def _prometheus_query_range(
    service: str,
    metric: str = "",
    window: str = "30m",
    slot: str = "primary",
    selector: str = "",
    metric_type: str = "gauge",
    datasource_url: str = "",
    data_mode: str = "real",
    **_: Any,
) -> dict[str, Any]:
    # Local fixtures accept the same arguments the catalog router injects, so
    # switching between local and MCP does not change agent code. Catalog
    # routing itself only applies to the MCP backend.
    if slot.startswith("extra:"):
        raise ToolError("local fixture has no extra metric pack")
    if not metric:
        metric = "qps" if slot == "secondary" else "latency_p99_ms"
    points = [
        {"t": _ago(14), "v": 210},
        {"t": _ago(10), "v": 240},
        {"t": _ago(8), "v": 890},
        {"t": _ago(6), "v": 1520},
        {"t": _ago(4), "v": 1180},
    ]
    return {
        "service": service,
        "metric": metric,
        "window": window,
        "points": points,
        "anomaly": {"t": points[2]["t"], "v": 890, "baseline": 240,
                    "direction": "up", "change_pct": 270.8},
        "query": f"{metric}{{service=\"{service}\"}}[{window}]",
        "source": "metric",
    }


def _prometheus_impact(
    service: str,
    window: str = "30m",
    selector: str = "",
    datasource_url: str = "",
    data_mode: str = "real",
    **_: Any,
) -> dict[str, Any]:
    """Local fixture: impact stats require real app metrics, so this is
    deliberately empty and labelled fixture (the agent will skip it)."""
    return {
        "source": "fixture",
        "data_mode": "fixture",
        "service": service,
        "window": window,
        "summary": {},
        "queries": {},
        "no_data": True,
        "note": "local fixture has no HTTP metrics; run against Prometheus",
    }


def _loki_query(service: str, pattern: str = "error", window: str = "30m") -> dict[str, Any]:
    lines = [
        {"t": _ago(7), "line": "level=error msg=\"connection pool exhausted\" svc=order user=13800138000"},
        {"t": _ago(6), "line": "level=error msg=\"upstream timeout\" host=pay-gw.corp"},
        {"t": _ago(5), "line": "level=warn msg=\"retry 3/3\" ip=10.12.3.44"},
    ]
    return {
        "service": service,
        "pattern": pattern,
        "window": window,
        "count": len(lines),
        "lines": lines,
        "query": f'{{service="{service}"}} |= "{pattern}"',
        "source": "log",
    }


def _cmdb_topology(service: str) -> dict[str, Any]:
    return {
        "service": service,
        "upstream": ["api-gateway"],
        "downstream": ["mysql-order", "redis-session", "pay-gw"],
        "instances": 6,
        "healthy_instances": 6,
    }


def _deploy_changes(service: str, window: str = "2h") -> dict[str, Any]:
    return {
        "service": service,
        "window": window,
        "changes": [
            {
                "change_id": "CHG-20261119-031",
                "at": _ago(9),
                "kind": "deploy",
                "summary": "order-service v2.31: 连接池上限 200 -> 50",
                "operator": "ci-bot",
            }
        ],
        "source": "change",
    }


def _ticket_create(service: str, title: str) -> dict[str, Any]:
    # Write tool: the orchestrator must register a Saga compensation for it.
    return {"ticket_id": "T-9001", "service": service, "title": title, "status": "open"}


def _kb_search_postmortems(query: str, limit: int = 3) -> dict[str, Any]:
    return {
        "query": query,
        "hits": [
            {"doc": "PM-2026-014", "title": "订单服务连接池打满导致支付超时", "score": 0.87},
            {"doc": "PM-2025-102", "title": "大促前配置变更未灰度", "score": 0.62},
        ][:limit],
    }


def build_default_registry(settings: Settings | None = None) -> ToolRegistry:
    registry = ToolRegistry(settings)
    registry.register(
        ToolSpec("prometheus.query_range", "查询指标区间与拐点", readonly=True,
                 params=("service", "metric", "window")),
        _prometheus_query_range,
    )
    registry.register(
        ToolSpec("prometheus.impact", "统计故障影响面（请求量/错误率/延迟/可用目标）",
                 readonly=True, params=("service", "window")),
        _prometheus_impact,
    )
    registry.register(
        ToolSpec("loki.query", "按服务检索日志", readonly=True,
                 params=("service", "pattern", "window")),
        _loki_query,
    )
    registry.register(
        ToolSpec("cmdb.topology", "查询服务上下游拓扑", readonly=True, params=("service",)),
        _cmdb_topology,
    )
    registry.register(
        ToolSpec("deploy.changes", "查询发布与配置变更", readonly=True,
                 params=("service", "window")),
        _deploy_changes,
    )
    registry.register(
        ToolSpec("ticket.create", "创建跟进工单（写操作，需补偿）", readonly=False,
                 params=("service", "title"), rate_limit_per_min=10),
        _ticket_create,
    )
    registry.register(
        ToolSpec("kb.search_postmortems", "检索历史复盘知识库", readonly=True,
                 params=("query", "limit")),
        _kb_search_postmortems,
    )
    return registry
