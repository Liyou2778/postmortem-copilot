"""Validate a DAG plan and print topological order + worker assignment."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "core"))

from app.dag import assign_steps, topological_order, validate_plan  # noqa: E402
from app.orchestrator import DEFAULT_PLAN  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", default="", help="JSON file; default DEFAULT_PLAN")
    parser.add_argument("--workers", default="w1,w2,w3")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    steps = list(DEFAULT_PLAN)
    if args.plan:
        steps = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    plan = validate_plan(steps)
    workers = [part.strip() for part in args.workers.split(",") if part.strip()]
    decision = assign_steps(plan, workers)
    payload = {
        "steps": [
            {"step_id": s.step_id, "role": s.role,
             "depends_on": list(s.depends_on),
             "side_effect": s.side_effect}
            for s in plan
        ],
        "order": topological_order(plan),
        "workers": workers,
        "assignments": decision.assignments,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("order:", " -> ".join(payload["order"]))
        for step_id, worker in decision.assignments.items():
            print(f"  {step_id:12s} -> {worker}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
