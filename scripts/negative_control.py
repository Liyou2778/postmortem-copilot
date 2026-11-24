"""Negative control: no injected fault; check that the pipeline stays honest.

This is not a claim of zero false positives forever. It records one stable-window
run so regressions (mock evidence, tool failures, fabricated business numbers)
are visible in the acceptance report.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "core"))

import httpx  # noqa: E402

from app.config import get  # noqa: E402

BASE = os.environ.get("PROBE_BASE", "http://127.0.0.1:8000")
key = get("COPILOT_API_KEY", "")
headers = {"X-API-Key": key} if key else {}
with httpx.Client(timeout=300, headers=headers) as client:
    task = client.post(f"{BASE}/api/v1/tasks", json={"title": "negative control"})
    task.raise_for_status()
    task_id = task.json()["task_id"]
    resp = client.post(f"{BASE}/api/v1/tasks/{task_id}/run",
                       json={"service_name": "ecs-copilot", "window": "3m"})
    resp.raise_for_status()
    data = resp.json()
    claims = data.get("claims") or []
    modes = {}
    for ev in (data.get("evidence") or {}).values():
        modes[ev.get("data_mode")] = modes.get(ev.get("data_mode"), 0) + 1
    high = [
        {"statement": c.get("statement", "")[:160],
         "confidence": c.get("confidence")}
        for c in claims if float(c.get("confidence") or 0) >= 0.8
    ]
    print(json.dumps({
        "task_id": task_id,
        "status": data.get("status"),
        "claims": len(claims),
        "high_confidence_claims": high,
        "evidence_modes": modes,
        "tool_failures": [t for t in (data.get("tool_calls") or []) if not t.get("ok")],
        "failed_steps": data.get("failed_steps"),
        "skipped_steps": data.get("skipped_steps"),
        "impact": (data.get("impact") or {}).get("business_impact"),
    }, ensure_ascii=False, indent=2))
