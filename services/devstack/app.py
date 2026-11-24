"""Dev stack: fault-injectable HTTP upstreams for the MCP tool servers.

This app has two jobs:

1. Speak enough of the Prometheus / Loki / CMDB / deploy / KB protocols that the
   MCP servers exercise a real HTTP path.
2. Accept a *fault* that changes what all endpoints return, so the evaluation
   harness can generate labelled scenarios without needing a Kubernetes cluster.

Fault control:
    POST   /admin/fault   {"type": "...", "service": "..."}
    GET    /admin/fault
    DELETE /admin/fault

Run: uvicorn services.devstack.app:app --port 9100
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Postmortem Devstack", version="0.2.0")


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


NOW = time.time()
CHANGE_AT = NOW - 9 * 60
SPIKE_AT = NOW - 8 * 60
LOG_AT = NOW - 7 * 60


# ---------------------------------------------------------------- fault state
class Fault(BaseModel):
    """An injected fault. ``type`` selects the symptom shape.

    ``noise=True`` adds plausible but irrelevant signals (an unrelated deploy,
    unrelated error lines, an extra healthy dependency). Real incidents are
    never as clean as a single signature, and an evaluator that only ever shows
    clean signals measures nothing.
    """

    type: str
    service: str = "order-service"
    noise: bool = False


_NOISE_CHANGES = [
    {"change_id": "CHG-NOISE-101", "kind": "deploy",
     "summary": "user-service 扩容 3 -> 6 副本（与本次故障无关）", "operator": "ci-bot"},
    {"change_id": "CHG-NOISE-102", "kind": "config",
     "summary": "search-service 调整缓存 TTL 300s -> 600s", "operator": "ops-oncall"},
]

_NOISE_LOGS = [
    'level=warn msg="cache miss burst" svc=search-service',
    'level=info msg="scheduled job finished in 812ms" svc=reporting',
]


_FAULT: Fault | None = None


FAULT_TYPES = (
    "deploy_regression",     # bad deploy lowered a limit -> pool exhaustion
    "resource_saturation",   # cpu/memory pressure, no change involved
    "dependency_timeout",    # downstream call times out
    "config_drift",          # runtime config changed outside a release
    "pod_kill",              # instances died and restarted
    "network_partition",     # connectivity between two services broke
    "dns_error",             # name resolution failing
    "io_stall",              # disk latency spike
    "traffic_spike",         # load surge beyond capacity
)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/admin/fault")
def set_fault(fault: Fault) -> dict[str, Any]:
    global _FAULT
    if fault.type not in FAULT_TYPES:
        return {"ok": False, "error": f"unknown fault type {fault.type!r}"}
    _FAULT = fault
    return {"ok": True, "fault": _FAULT.model_dump()}


@app.get("/admin/fault")
def get_fault() -> dict[str, Any]:
    return {"fault": _FAULT.model_dump() if _FAULT else None}


@app.delete("/admin/fault")
def clear_fault() -> dict[str, Any]:
    global _FAULT
    _FAULT = None
    return {"ok": True}


def _fault_type() -> str:
    return _FAULT.type if _FAULT else "deploy_regression"


# ---------------------------------------------------------------- prometheus
_SERIES_BY_FAULT: dict[str, list[float]] = {
    "deploy_regression": [210, 240, 890, 1520, 1180],
    "resource_saturation": [300, 420, 980, 2100, 2400],
    "dependency_timeout": [220, 230, 260, 410, 620],
    "config_drift": [215, 250, 700, 1150, 900],
    "pod_kill": [205, 900, 210, 240, 230],
    "network_partition": [230, 240, 300, 1500, 1700],
    "dns_error": [225, 240, 280, 520, 480],
    "io_stall": [240, 300, 760, 1300, 1100],
    "traffic_spike": [400, 600, 1100, 1900, 1600],
}


def _noise_on() -> bool:
    return bool(_FAULT and _FAULT.noise)


_QPS_BY_FAULT: dict[str, float] = {
    "traffic_spike": 12000.0,     # load itself is the cause
    "resource_saturation": 3000.0,  # normal load, bad capacity
}
QPS_DEFAULT = 3000.0


@app.get("/api/v1/query_range")
def query_range(query: str = "", window: str = "30m") -> dict[str, Any]:
    stamps = [NOW - 14 * 60, NOW - 10 * 60, SPIKE_AT, NOW - 6 * 60, NOW - 4 * 60]

    # The same endpoint serves several metrics; the caller selects via `query`.
    if "qps" in query.lower():
        qps = _QPS_BY_FAULT.get(_fault_type(), QPS_DEFAULT)
        result = [
            {
                "metric": {"__name__": "qps", "service": "order-service"},
                "values": [[ts, str(qps)] for ts in stamps],
            }
        ]
        return {
            "status": "success",
            "data": {"resultType": "matrix", "result": result},
            "query": query,
            "window": window,
            "fault": _fault_type(),
            "noise": _noise_on(),
        }

    values = _SERIES_BY_FAULT.get(_fault_type(), _SERIES_BY_FAULT["deploy_regression"])
    result = [
        {
            "metric": {"__name__": "latency_p99_ms", "service": "order-service"},
            "values": [[ts, str(value)] for ts, value in zip(stamps, values)],
        }
    ]
    if _noise_on():
        result.append(
            {
                "metric": {"__name__": "latency_p99_ms", "service": "user-service"},
                "values": [[ts, "180"] for ts in stamps],
            }
        )
    return {
        "status": "success",
        "data": {"resultType": "matrix", "result": result},
        "query": query,
        "window": window,
        "fault": _fault_type(),
        "noise": _noise_on(),
    }


# ---------------------------------------------------------------------- loki
_LOGS_BY_FAULT: dict[str, list[str]] = {
    "deploy_regression": [
        'level=error msg="connection pool exhausted" svc=order user=13800138000',
        'level=error msg="upstream timeout" host=pay-gw.corp',
        'level=warn msg="retry 3/3" ip=10.12.3.44',
    ],
    "resource_saturation": [
        "level=warn msg=\"cpu throttling 78%\" pod=order-7f9c",
        'level=error msg="request queue full" svc=order',
        'level=warn msg="memory pressure" node=k8s-node-3.corp',
    ],
    "dependency_timeout": [
        'level=error msg="context deadline exceeded calling inventory"',
        'level=warn msg="circuit breaker half-open" target=inventory',
        'level=error msg="upstream timeout" host=inventory.corp',
    ],
    "config_drift": [
        'level=warn msg="config reloaded runtime pool.size=50"',
        'level=error msg="connection pool exhausted" svc=order',
        'level=info msg="config version changed 41 -> 42"',
    ],
    "pod_kill": [
        'level=error msg="liveness probe failed, restarting" pod=order-7f9c',
        'level=info msg="container started" pod=order-7f9c',
        'level=warn msg="readiness gate closed" pod=order-7f9c',
    ],
    "network_partition": [
        'level=error msg="dial tcp 10.2.0.9:6379: i/o timeout"',
        'level=warn msg="retry 2/3" target=redis-session',
        'level=error msg="connection reset by peer" peer=10.2.0.9',
    ],
    "dns_error": [
        'level=error msg="lookup inventory.svc.cluster.local: no such host"',
        'level=warn msg="dns resolution retry" attempts=3',
        'level=error msg="upstream unreachable" host=inventory',
    ],
    "io_stall": [
        'level=warn msg="fsync took 4200ms" fs=/data',
        'level=error msg="database write timeout" svc=mysql-order',
        'level=warn msg="iowait 63%" node=k8s-node-3.corp',
    ],
    "traffic_spike": [
        'level=warn msg="qps 12000 exceeds capacity 6000"',
        'level=error msg="connection pool exhausted" svc=order',
        'level=warn msg="rate limit engaged" svc=api-gateway',
    ],
}


@app.get("/loki/api/v1/query_range")
def loki_query_range(query: str = "", limit: int = 100) -> dict[str, Any]:
    lines = list(_LOGS_BY_FAULT.get(_fault_type(), _LOGS_BY_FAULT["deploy_regression"]))
    if _noise_on():
        lines = lines + _NOISE_LOGS
    stamps = [LOG_AT + i * 20 for i in range(len(lines))]
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"service": "order-service"},
                    "values": [[_iso(ts), line] for ts, line in zip(stamps, lines)][:limit],
                }
            ],
        },
        "query": query,
        "fault": _fault_type(),
        "noise": _noise_on(),
    }


# ---------------------------------------------------------------------- cmdb
@app.get("/api/v1/topology/{service}")
def topology(service: str) -> dict[str, Any]:
    ft = _fault_type()
    healthy = 5 if ft == "pod_kill" else 6
    downstream = ["mysql-order", "redis-session", "pay-gw"]
    if ft == "dependency_timeout":
        downstream = ["mysql-order", "redis-session", "inventory"]
    if _noise_on():
        downstream = downstream + ["user-service", "search-service"]
    return {
        "service": service,
        "upstream": ["api-gateway"],
        "downstream": downstream,
        "instances": 6,
        "healthy_instances": healthy,
        "fault": ft,
        "noise": _noise_on(),
    }


# -------------------------------------------------------------------- deploy
_CHANGE_BY_FAULT: dict[str, list[dict[str, Any]]] = {
    "deploy_regression": [
        {"change_id": "CHG-20261119-031", "at": _iso(CHANGE_AT), "kind": "deploy",
         "summary": "order-service v2.31: 连接池上限 200 -> 50", "operator": "ci-bot"},
    ],
    "config_drift": [
        {"change_id": "CFG-20261119-007", "at": _iso(CHANGE_AT), "kind": "config",
         "summary": "runtime config pool.size set to 50 (outside release)", "operator": "ops-oncall"},
    ],
}


def _noise_change_records() -> list[dict[str, Any]]:
    records = []
    for offset, item in enumerate(_NOISE_CHANGES):
        entry = dict(item)
        entry["at"] = _iso(NOW - (30 + offset * 15) * 60)
        records.append(entry)
    return records


@app.get("/api/v1/changes")
def changes(service: str = "", window: str = "2h") -> dict[str, Any]:
    ft = _fault_type()
    items = list(_CHANGE_BY_FAULT.get(ft, []))
    if _noise_on():
        items = _noise_change_records() + items
    return {"service": service, "window": window, "changes": items,
            "fault": ft, "noise": _noise_on()}


# ------------------------------------------------------------------------ kb
@app.get("/api/v1/search")
def search(q: str = "", limit: int = 3) -> dict[str, Any]:
    hits = [
        {"doc": "PM-2026-014", "title": "订单服务连接池打满导致支付超时", "score": 0.87},
        {"doc": "PM-2025-102", "title": "大促前配置变更未灰度", "score": 0.62},
    ]
    return {"query": q, "hits": hits[:limit], "fault": _fault_type()}
