"""Create and run one analysis task, then print the fields needed for review.

Usage (on the ECS):
    .venv/bin/python scripts/run_analysis.py --service ecs-copilot --window 5m
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import httpx


def _api_key() -> str:
    key = os.getenv("COPILOT_API_KEY", "").strip()
    if key:
        return key
    for path in (Path("/opt/copilot/.env"), Path(".env")):
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            if raw.startswith("COPILOT_API_KEY="):
                return raw.split("=", 1)[1].strip().strip("'\"")
    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--service", default="ecs-copilot")
    parser.add_argument("--window", default="5m")
    parser.add_argument("--title", default="")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    started = time.perf_counter()
    headers = {}
    api_key = _api_key()
    if api_key:
        headers["X-API-Key"] = api_key
    with httpx.Client(timeout=300.0, headers=headers) as client:
        created = client.post(
            f"{base}/api/v1/tasks",
            json={"title": args.title or f"M9 probe {args.service}"},
        )
        created.raise_for_status()
        task_id = created.json()["task_id"]
        resp = client.post(
            f"{base}/api/v1/tasks/{task_id}/run",
            json={"service_name": args.service, "window": args.window},
        )
        resp.raise_for_status()
        data = resp.json()
        trace_data = client.get(f"{base}/api/v1/tasks/{task_id}/trace").json()

    print(json.dumps({
        "task_id": task_id,
        "elapsed_s": round(time.perf_counter() - started, 2),
        "status": data.get("status"),
        "model_mode": data.get("model_mode"),
        "claims": len(data.get("claims") or []),
        "tool_calls": [
            {"tool": t.get("tool"), "ok": t.get("ok"), "error": (t.get("error") or "")[:120]}
            for t in (data.get("tool_calls") or [])
        ],
        "evidence": {
            eid: {"mode": ev.get("data_mode"), "source": ev.get("source"),
                  "excerpt": (ev.get("excerpt") or "")[:260]}
            for eid, ev in (data.get("evidence") or {}).items()
        },
        "impact": data.get("impact") or {},
        "usage": data.get("usage") or {},
        "cost": data.get("cost") or {},
        "trace": {
            "spans": len(trace_data.get("spans") or []),
            "summary": trace_data.get("summary") or {},
        },
        "report_head": (data.get("report_md") or "").splitlines()[:24],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
