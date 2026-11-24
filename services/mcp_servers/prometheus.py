"""MCP server: Prometheus (read-only).

Two responsibilities, kept deliberately small:

1. Translate a *service* into a PromQL series. The caller (the core catalog
   router) supplies the metric and label selector; this server never invents
   them and never hardcodes an application-specific metric name.
2. Turn Prometheus' matrix response into a compact ``points + anomaly`` shape
   that the agent can reason about, with an explicit ``data_mode`` label.

Failure policy: an unreachable or blocked upstream degrades to a deterministic
fixture **and says so** (``source: fixture``, ``data_mode: fixture``); it never
pretends synthetic data is live. A reachable upstream that simply has no
samples returns ``points: []`` with ``source: prometheus`` — "no data" is a
real answer and must not be replaced by a fabricated series.

Run standalone:
    python -m services.mcp_servers.prometheus
"""
from __future__ import annotations

import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from services.mcp_servers._shared import (  # noqa: E402
    ToolSecurityError,
    assert_host_allowed,
    fixture_note,
    http_get_json,
    parse_allowlist,
    redact_deep,
)

mcp = FastMCP("prometheus-readonly")

BASE_URL = os.getenv("PROMETHEUS_URL", "http://127.0.0.1:9090")
ALLOWLIST = parse_allowlist("PROMETHEUS_ALLOWLIST", "localhost,127.0.0.1,prometheus")

DEFAULT_METRIC = "latency_p99_ms"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat(timespec="seconds")


def parse_window(window: str) -> int:
    """``30m`` / ``2h`` / ``90s`` -> seconds. Unknown input falls back to 30m."""
    text = (window or "30m").strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if len(text) < 2 or text[-1] not in units:
        return 1800
    try:
        value = int(float(text[:-1]))
    except ValueError:
        return 1800
    return max(60, value * units[text[-1]])


def build_query(metric: str, selector: str, service: str,
                metric_type: str = "gauge", rate_window: str = "5m") -> str:
    """Build a PromQL expression without interpolating unquoted user text.

    ``selector`` comes from the trusted service catalog (an operator writes it);
    ``service`` is the untrusted request argument and is always quoted here.
    A counter is wrapped in ``rate(...)`` because a monotonic counter's raw
    values can never show a usable anomaly shape.
    """
    metric = (metric or DEFAULT_METRIC).strip() or DEFAULT_METRIC
    sel = (selector or "").strip()
    if sel.startswith("{") and sel.endswith("}"):
        sel = sel[1:-1].strip()
    if not sel:
        escaped = service.replace("\\", "\\\\").replace('"', '\\"')
        sel = f'service="{escaped}"'
    if metric == "node_cpu_seconds_total":
        # A raw per-CPU/per-mode counter is useless to a single-series anomaly
        # detector. Percent busy is the bounded, interpretable CPU signal.
        return (
            "100 * (1 - avg(rate(node_cpu_seconds_total"
            f"{{{sel},mode=\"idle\"}}[{rate_window}])))"
        )
    if (metric_type or "gauge").lower() == "counter":
        return f"rate({metric}{{{sel}}}[{rate_window}])"
    return f"{metric}{{{sel}}}"


def _detect_anomaly(points: list[dict[str, Any]], tiny_baseline_pct: bool = False) -> dict[str, Any]:
    """Find the largest deviation from the early-window baseline.

    Baseline = median of the first third of the window, because the very first
    sample is often a scrape-boundary artifact (e.g. a counter reset). Both
    directions matter: a memory-available drop is as interesting as a latency
    spike. A change is reported only above 50% so normal jitter stays quiet.
    """
    if len(points) < 3:
        return {}
    values = [float(p["v"]) for p in points]
    baseline_window = values[: max(1, len(values) // 3)]
    baseline = statistics.median(baseline_window)
    peak = max(points, key=lambda p: float(p["v"]))
    trough = min(points, key=lambda p: float(p["v"]))
    peak_v, trough_v = float(peak["v"]), float(trough["v"])

    # Thresholds are relative (>=50%), but the *winner* is the direction with
    # the larger absolute movement. Picking by ratio alone lets a 0.01 -> 0
    # load dip (infinite relative drop) outrank the 0.01 -> 6.7 CPU spike that
    # actually matters — a real false negative we hit during the first ECS
    # drill. Absolute delta keeps the comparison meaningful across scales.
    if baseline == 0:
        up_change = float("inf") if peak_v > 0 else 0.0
        down_change = 0.0
    else:
        up_change = (peak_v - baseline) / baseline
        down_change = (baseline - trough_v) / baseline
    candidates: list[tuple[str, dict[str, Any], float, float]] = []
    if up_change >= 0.5:
        candidates.append(("up", peak, peak_v - baseline, up_change))
    if down_change >= 0.5:
        candidates.append(("down", trough, baseline - trough_v, down_change))
    if not candidates:
        return {}
    direction, target, _delta, _ratio = max(candidates, key=lambda c: c[2])
    value = float(target["v"])
    # Percentages off a near-zero gauge baseline are noise (0.01 -> 7.25 prints
    # 72400%). A counter rate, however, legitimately lives at 0.001-0.01; the
    # caller opts in so a 2x disk-IO rate change is not reported as "None%".
    change_pct = (
        round((value - baseline) / baseline * 100.0, 1)
        if baseline > 0 and (baseline >= 0.5 or tiny_baseline_pct)
        else None
    )
    return {
        "t": target["t"],
        "v": value,
        "baseline": round(baseline, 4),
        "direction": direction,
        "change_pct": change_pct,
        # Absolute movement, so a reader can tell a 0.01 load wiggle from a
        # real 6.7 load spike without knowing the metric's unit.
        "delta": round(_delta, 4),
    }


def _series_from_prometheus(payload: dict[str, Any], requested_metric: str,
                            data_mode: str, metric_type: str = "gauge") -> dict[str, Any]:
    results = (payload.get("data") or {}).get("result") or []
    if not results:
        return {"metric": requested_metric, "points": [], "anomaly": {}}
    series = results[0]
    values = series.get("values") or []
    points = [
        {"t": _iso(float(ts)), "v": float(val)}
        for ts, val in values
        if isinstance(val, (int, float, str))
    ]
    metric = (series.get("metric") or {}).get("__name__") or requested_metric
    if data_mode == "mock":
        # Devstack answers every metric query with its one synthetic series;
        # report the metric the caller asked for so the evidence is not
        # confusingly labelled with the fixture's series name.
        metric = requested_metric
    return {
        "metric": metric,
        "points": points,
        "anomaly": _detect_anomaly(points, tiny_baseline_pct=(metric_type == "counter")),
    }


def _fixture(metric: str, query: str, datasource: str) -> dict[str, Any]:
    now = time.time()
    values = [210.0, 240.0, 890.0, 1520.0, 1180.0]
    points = [
        {"t": _iso(now - offset * 60), "v": value}
        for offset, value in zip((14, 10, 8, 6, 4), values)
    ]
    return {
        "metric": metric,
        "points": points,
        "anomaly": _detect_anomaly(points),
        "query": query,
        "datasource": datasource,
        "data_mode": "fixture",
    }


@mcp.tool()
def query_range(
    service: str,
    metric: str = "",
    window: str = "30m",
    selector: str = "",
    metric_type: str = "gauge",
    datasource_url: str = "",
    data_mode: str = "real",
) -> dict[str, Any]:
    """查询一个服务在时间窗内的指标序列与异常拐点。

    Args:
        service: 服务名，例如 ecs-copilot
        metric: 指标名；由服务目录注入，留空时使用 latency_p99_ms
        window: 时间窗，例如 30m / 1h
        selector: PromQL 标签选择器，例如 service="ecs-copilot"
        metric_type: gauge / counter；counter 会走 rate()
        datasource_url: 数据源地址；由服务目录注入，留空时用环境变量
        data_mode: real / mock；由服务目录注入，用于证据可信度标注
    """
    base = (datasource_url or BASE_URL).strip().rstrip("/")
    requested = (metric or DEFAULT_METRIC).strip() or DEFAULT_METRIC
    seconds = parse_window(window)
    rate_window = f"{max(60, min(300, seconds // 2))}s"
    query = build_query(requested, selector, service, metric_type, rate_window)
    end = time.time()
    start = end - seconds
    step = max(15, seconds // 120)
    url = f"{base}/api/v1/query_range"
    try:
        assert_host_allowed(url, ALLOWLIST)
        payload = http_get_json(
            url,
            ALLOWLIST,
            params={
                "query": query,
                "start": start,
                "end": end,
                "step": step,
                # Devstack ignores start/end/step and reads `window`; real
                # Prometheus ignores unknown params, so passing both is safe.
                "window": window,
            },
        )
        data = _series_from_prometheus(payload, requested, data_mode, metric_type)
        data.update(
            {
                "source": "prometheus",
                "data_mode": data_mode,
                "datasource": base,
                "query": query,
                "window": window,
                "step_s": step,
                "metric_type": metric_type,
            }
        )
        if not data["points"]:
            data["no_data"] = True
            data["note"] = "Prometheus 可访问，但该服务在窗口内没有匹配的样本"
        return redact_deep(data)
    except ToolSecurityError as exc:
        payload = _fixture(requested, query, base)
        payload["degraded_reason"] = f"url blocked by allowlist: {exc}"[:160]
        return fixture_note(payload, payload["degraded_reason"])
    except Exception as exc:  # noqa: BLE001 - degrade, but label it
        payload = _fixture(requested, query, base)
        return fixture_note(payload, f"{type(exc).__name__}: {exc}")


def _instant_value(base: str, query: str) -> float | None:
    """One scalar from an instant query; None on error/empty."""
    url = f"{base}/api/v1/query"
    try:
        assert_host_allowed(url, ALLOWLIST)
        payload = http_get_json(url, ALLOWLIST, params={"query": query})
    except Exception:  # noqa: BLE001 - business impact is optional
        return None
    results = (payload.get("data") or {}).get("result") or []
    if not results:
        return None
    value = results[0].get("value") or []
    if len(value) != 2:
        return None
    try:
        return float(value[1])
    except (TypeError, ValueError):
        return None


def _range_values(base: str, query: str, start: float, end: float,
                  step: int) -> list[float]:
    """Execute one PromQL range query and return the first series' values."""
    url = f"{base}/api/v1/query_range"
    assert_host_allowed(url, ALLOWLIST)
    payload = http_get_json(
        url, ALLOWLIST,
        params={"query": query, "start": start, "end": end, "step": step},
    )
    results = (payload.get("data") or {}).get("result") or []
    if not results:
        return []
    values = [float(value) for _, value in (results[0].get("values") or [])]
    # histogram_quantile returns NaN when a bucket window has no observations;
    # NaN is not valid JSON and would make the whole tool result unparsable.
    return [value for value in values if math.isfinite(value)]


def _stats(values: list[float], scale: float = 1.0) -> dict[str, Any]:
    values = [v for v in values if math.isfinite(v)]
    if not values:
        return {"points": 0}
    scaled = [v * scale for v in values]
    return {
        "points": len(scaled),
        "min": round(min(scaled), 4),
        "max": round(max(scaled), 4),
        "avg": round(sum(scaled) / len(scaled), 4),
        "last": round(scaled[-1], 4),
    }


def _strip_selector(selector: str, service: str) -> str:
    sel = (selector or "").strip()
    if sel.startswith("{") and sel.endswith("}"):
        sel = sel[1:-1].strip()
    if not sel:
        escaped = service.replace("\\", "\\\\").replace('"', '\\"')
        sel = f'service="{escaped}"'
    return sel


@mcp.tool()
def impact(
    service: str,
    window: str = "30m",
    selector: str = "",
    datasource_url: str = "",
    data_mode: str = "real",
    requests_metric: str = "copilot_http_requests_total",
    latency_metric: str = "copilot_http_request_duration_seconds",
    impact_policy: str = "{}",
) -> dict[str, Any]:
    """统计一次故障的影响面：请求量、5xx、P95 延迟、可用目标数。

    所有数字都来自可复现的 PromQL，返回体里带 query 原文，报告可直接展示。

    Args:
        service: 服务名
        window: 故障窗口，例如 30m；基线窗口取紧邻的等长窗口
        selector: 标签选择器，由服务目录注入
        datasource_url: Prometheus 地址，由服务目录注入
        data_mode: real / mock，由服务目录注入
        requests_metric: HTTP 请求 counter
        latency_metric: HTTP 延迟 histogram
        impact_policy: JSON 对象，业务口径假设（users_per_request/cost_per_minute）
    """
    base = (datasource_url or BASE_URL).strip().rstrip("/")
    sel = _strip_selector(selector, service)
    seconds = parse_window(window)
    end = time.time()
    incident_start = end - seconds
    baseline_start = incident_start - seconds
    step = max(15, seconds // 120)

    # The metric labels come from the copilot scrape job (service=ecs-copilot),
    # not from the node exporter job; `up` intentionally counts both so the
    # report can show whether monitoring targets were alive.
    queries = {
        "requests": f"sum(rate({requests_metric}{{{sel}}}[1m]))",
        "error_rate": (
            f'(sum(rate({requests_metric}{{{sel},status=~"5.."}}[1m])) or vector(0)) '
            f"/ clamp_min(sum(rate({requests_metric}{{{sel}}}[1m])), 0.000001)"
        ),
        "p95": (
            f"histogram_quantile(0.95, "
            f"sum(rate({latency_metric}_bucket{{{sel}}}[1m])) by (le))"
        ),
        "targets_up": f"sum(up{{{sel}}})",
    }
    scales = {"p95": 1000.0}  # seconds -> milliseconds in the summary
    result: dict[str, Any] = {
        "source": "prometheus",
        "data_mode": data_mode,
        "service": service,
        "window": window,
        "selector": sel,
        "datasource": base,
        "incident_window": {"start": _iso(incident_start), "end": _iso(end)},
        "baseline_window": {"start": _iso(baseline_start), "end": _iso(incident_start)},
        "queries": {},
        "summary": {},
        "errors": [],
    }
    for name, query in queries.items():
        try:
            incident = _range_values(base, query, incident_start, end, step)
            baseline = _range_values(base, query, baseline_start, incident_start, step)
        except Exception as exc:  # noqa: BLE001 - one query must not kill the section
            result["errors"].append(f"{name}: {type(exc).__name__}: {exc}"[:200])
            continue
        incident_stats = _stats(incident, scales.get(name, 1.0))
        baseline_stats = _stats(baseline, scales.get(name, 1.0))
        result["queries"][name] = {
            "expr": query,
            "incident": incident_stats,
            "baseline": baseline_stats,
        }
        if incident_stats.get("points"):
            result["summary"][f"{name}_incident"] = incident_stats["max"]
        if baseline_stats.get("points"):
            result["summary"][f"{name}_baseline"] = baseline_stats["max"]

    def pct(incident_key: str, baseline_key: str) -> float | None:
        inc = result["summary"].get(incident_key)
        base = result["summary"].get(baseline_key)
        if inc is None or base in (None, 0):
            return None
        return round((inc - base) / base * 100.0, 1)

    result["impact"] = {
        "qps_change_pct": pct("requests_incident", "requests_baseline"),
        "error_rate_delta": (
            round(result["summary"].get("error_rate_incident", 0.0)
                  - result["summary"].get("error_rate_baseline", 0.0), 4)
            if "error_rate_incident" in result["summary"] else None
        ),
        "p95_ms_change_pct": pct("p95_incident", "p95_baseline"),
        "duration_s": seconds,
    }

    # Business impact is an explicitly configured estimate, never a measured
    # fact. Show zeros/unconfigured rather than inventing users or money.
    try:
        policy = json.loads(impact_policy or "{}")
        if not isinstance(policy, dict):
            policy = {}
    except json.JSONDecodeError:
        policy = {}
    window_expr = f"{max(60, seconds)}s"
    requests_total = _instant_value(
        base, f"sum(increase({requests_metric}{{{sel}}}[{window_expr}]))"
    ) or 0.0
    errors_total = _instant_value(
        base,
        f'sum(increase({requests_metric}{{{sel},status=~"5.."}}[{window_expr}]))',
    ) or 0.0
    users_per_request = max(0.0, float(policy.get("users_per_request") or 0))
    cost_per_minute = max(0.0, float(policy.get("cost_per_minute") or 0))
    configured = bool(users_per_request or cost_per_minute)
    result["business_impact"] = {
        "configured": configured,
        "requests_total": round(requests_total, 1),
        "errors_total": round(errors_total, 1),
        "estimated_affected_users": round(requests_total * users_per_request, 1),
        "estimated_cost": round((seconds / 60.0) * cost_per_minute, 2),
        "currency": str(policy.get("currency") or "CNY"),
        "assumptions": policy if policy else {},
        "note": (
            "按服务目录配置的系数估算，不是财务确认口径"
            if configured else
            "未配置业务口径（users_per_request / cost_per_minute）"
        ),
    }
    result["no_data"] = not any(
        (item.get("incident") or {}).get("points", 0)
        for item in result["queries"].values()
    )
    if result["no_data"]:
        result["note"] = (
            "Prometheus 可访问，但没有 copilot HTTP 指标样本；"
            "确认 8000/metrics 已被 scrape，或换 requests_metric/latency_metric"
        )
    return redact_deep(result)


# Python-level alias kept for direct unit tests/probes; the MCP tool name is
# deliberately the short `impact` used by the catalog router.
impact_overview = impact


if __name__ == "__main__":
    mcp.run()
