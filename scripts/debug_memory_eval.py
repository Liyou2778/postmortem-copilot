"""One memory-fault analysis with full claims/evidence, for root-cause debugging."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "core"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.config import get  # noqa: E402
from real_eval import Fault  # noqa: E402

BASE = os.getenv("PROBE_BASE", "http://127.0.0.1:8000")
key = get("COPILOT_API_KEY", "")
headers = {"X-API-Key": key}
fault = Fault("memory")
try:
    fault.start()
    print("fault started; warming 45s", flush=True)
    time.sleep(45)
    with httpx.Client(timeout=300, headers=headers) as client:
        created = client.post(f"{BASE}/api/v1/tasks",
                              json={"title": "debug-memory"})
        created.raise_for_status()
        task_id = created.json()["task_id"]
        resp = client.post(f"{BASE}/api/v1/tasks/{task_id}/run",
                           json={"service_name": "ecs-copilot", "window": "3m"})
        resp.raise_for_status()
        data = resp.json()
    print(json.dumps({
        "task_id": task_id,
        "status": data.get("status"),
        "claims": data.get("claims"),
        "failed_steps": data.get("failed_steps"),
        "skipped_steps": data.get("skipped_steps"),
        "evidence": {
            k: {"mode": v.get("data_mode"), "excerpt": v.get("excerpt")}
            for k, v in (data.get("evidence") or {}).items()
        },
        "tool_calls": data.get("tool_calls"),
        "impact": data.get("impact"),
    }, ensure_ascii=False, indent=2))
finally:
    fault.stop()
    time.sleep(35)
