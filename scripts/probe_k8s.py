"""End-to-end probe for the K8s deployment (does not mutate cloud resources)."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "core"))

import httpx  # noqa: E402

from app.config import get  # noqa: E402

def _api_key() -> str:
    key = os.environ.get("COPILOT_API_KEY", "").strip()
    if key:
        return key
    for path in (Path("/opt/copilot/.env"), Path(".env")):
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            if raw.startswith("COPILOT_API_KEY="):
                return raw.split("=", 1)[1].strip().strip("'\"")
    return ""


BASE = os.environ.get("PROBE_BASE", "http://127.0.0.1:18000")
_API_KEY = _api_key()
_HEADERS = {"X-API-Key": _API_KEY} if _API_KEY else {}
with httpx.Client(timeout=300, headers=_HEADERS) as client:
    created = client.post(f"{BASE}/api/v1/tasks",
                          json={"title": "k8s probe"})
    created.raise_for_status()
    task_id = created.json()["task_id"]
    started = time.perf_counter()
    resp = client.post(f"{BASE}/api/v1/tasks/{task_id}/run",
                       json={"service_name": "ecs-copilot", "window": "3m"})
    resp.raise_for_status()
    data = resp.json()
    trace = client.get(f"{BASE}/api/v1/tasks/{task_id}/trace").json()
    with httpx.Client(timeout=20) as admin_client:
        admin = admin_client.get(
            f"{BASE}/admin/cmdb",
            auth=(get("ADMIN_USER", "admin"), get("ADMIN_PASSWORD", "")),
        )
    evidence = data.get("evidence") or {}
    modes = {}
    for item in evidence.values():
        modes[item.get("data_mode")] = modes.get(item.get("data_mode"), 0) + 1
    print(json.dumps({
        "status": data.get("status"),
        "elapsed_s": round(time.perf_counter() - started, 2),
        "claims": len(data.get("claims") or []),
        "tool_calls": len(data.get("tool_calls") or []),
        "tool_failures": [t for t in (data.get("tool_calls") or []) if not t.get("ok")],
        "evidence_modes": modes,
        "failed_steps": data.get("failed_steps"),
        "skipped_steps": data.get("skipped_steps"),
        "trace_spans": len(trace.get("spans") or []),
        "cost_configured": (data.get("cost") or {}).get("configured"),
        "admin_cmdb_status": admin.status_code,
    }, ensure_ascii=False, indent=2))
