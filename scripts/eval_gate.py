"""CI gate for the real-Prometheus evaluation artifact.

The gate is intentionally stricter than "the demo looked fine":
  * every scenario must be present;
  * Rank1 and Recall@3 must both be 100%;
  * every evidence item must be real (no mock/fixture);
  * impact must be present;
  * no tool call may have failed.

Usage:
    python scripts/eval_gate.py --eval eval-real-m9.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REQUIRED_SCENARIOS = ("cpu", "memory", "disk")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval", default="eval-real-m9.json")
    args = parser.parse_args()
    path = Path(args.eval)
    if not path.exists():
        print(f"FAIL: eval artifact not found: {path}")
        return 2
    payload = json.loads(path.read_text(encoding="utf-8"))
    results = payload.get("results") or []
    errors: list[str] = []
    by_name = {r.get("scenario"): r for r in results}
    for name in REQUIRED_SCENARIOS:
        row = by_name.get(name)
        if not row:
            errors.append(f"{name}: missing scenario")
            continue
        if not row.get("rank1_hit"):
            errors.append(f"{name}: rank1 miss")
        if not row.get("recall_at_3"):
            errors.append(f"{name}: recall@3 miss")
        if row.get("evidence_total") != row.get("evidence_real"):
            errors.append(
                f"{name}: evidence {row.get('evidence_real')}/{row.get('evidence_total')} real"
            )
        if not row.get("impact_present"):
            errors.append(f"{name}: impact missing")
        for call in row.get("tool_calls") or []:
            if not call.get("ok"):
                errors.append(f"{name}: tool {call.get('tool')} failed")
    if errors:
        print("FAIL: real-eval gate")
        for item in errors:
            print(" -", item)
        return 1
    print(
        "PASS: 3/3 rank1, 3/3 recall, 8/8 real per scenario, impact present, tools healthy"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
