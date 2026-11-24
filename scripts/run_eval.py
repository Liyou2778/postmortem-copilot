r"""Run the M4 evaluation suite and emit a report.

Usage:
    python scripts/run_eval.py --limit 9 --llm mock      # fast sanity run
    python scripts/run_eval.py --llm real --limit 0      # full 63-scenario run

Requires the devstack to be running on 127.0.0.1:9100.

Exit code: 0 when Recall@3 >= threshold, 1 otherwise (usable as a CI gate).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
sys.path.insert(0, str(ROOT / "services" / "core"))

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    pass

import httpx  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.llm import LLMConfig, ModelClient  # noqa: E402
from eval.runner import ScenarioRunner, aggregate, score_prediction  # noqa: E402
from eval.scenarios import build_scenarios  # noqa: E402


def set_fault(base: str, fault_type: str, service: str, noise: bool = False) -> None:
    with httpx.Client(timeout=10) as client:
        resp = client.post(
            f"{base}/admin/fault",
            json={"type": fault_type, "service": service, "noise": noise},
        )
        resp.raise_for_status()


def clear_fault(base: str) -> None:
    with httpx.Client(timeout=10) as client:
        client.delete(f"{base}/admin/fault")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=9, help="0 = all scenarios")
    ap.add_argument("--per-fault", type=int, default=7)
    ap.add_argument("--llm", choices=["mock", "real"], default="mock")
    ap.add_argument("--devstack", default="http://127.0.0.1:9100")
    ap.add_argument("--threshold", type=float, default=0.70, help="Recall@3 gate")
    ap.add_argument("--noise", action="store_true",
                    help="inject distractor evidence (unrelated deploy/logs/deps)")
    ap.add_argument("--faults", default="",
                    help="comma-separated fault types to run (default: all)")
    ap.add_argument("--out", default="eval-report")
    args = ap.parse_args()

    settings = load_settings()
    if args.llm == "real" and not settings.llm_configured:
        print("!! --llm real 但未配置 LLM_BASE_URL/LLM_MODEL")
        return 2
    llm = ModelClient(LLMConfig()) if args.llm == "mock" else ModelClient()

    scenarios = build_scenarios(args.per_fault)
    if args.faults:
        wanted = {f.strip() for f in args.faults.split(",") if f.strip()}
        scenarios = [s for s in scenarios if s.fault_type in wanted]
    if args.limit:
        scenarios = scenarios[: args.limit]

    print("=" * 72)
    print(f"M4 评测  scenarios={len(scenarios)}  llm={args.llm}  noise={args.noise}  "
          f"devstack={args.devstack}")
    print("=" * 72)

    scores = []
    predictions = []
    started = time.perf_counter()
    with ScenarioRunner(llm=llm, settings=settings, use_mcp=True) as runner:
        for i, scenario in enumerate(scenarios, start=1):
            set_fault(args.devstack, scenario.fault_type, scenario.service, args.noise)
            pred = runner.run(scenario)
            score = score_prediction(pred, scenario)
            scores.append(score)
            predictions.append({
                "scenario": scenario.scenario_id,
                "fault": scenario.fault_type,
                "service": scenario.service,
                "status": pred.status,
                "latency_s": pred.latency_s,
                "hit@1": score.hit_at_1,
                "hit@3": score.hit_at_3,
                "rr": round(score.reciprocal_rank, 3),
                "rank1": score.predicted_rank1,
            })
            flag = "HIT " if score.hit_at_3 else "MISS"
            print(f"  [{i:>2}/{len(scenarios)}] {scenario.scenario_id} {scenario.fault_type:20} "
                  f"{flag} r1={score.hit_at_1} rr={score.reciprocal_rank:.2f} "
                  f"{pred.latency_s:>5.1f}s")
    clear_fault(args.devstack)

    elapsed = time.perf_counter() - started
    summary = aggregate(scores)
    summary["elapsed_s"] = round(elapsed, 1)
    summary["llm"] = args.llm

    print("\n" + "=" * 72)
    print("汇总")
    print("=" * 72)
    print(f"  Recall@1 : {summary['recall_at_1']:.3f}")
    print(f"  Recall@3 : {summary['recall_at_3']:.3f}")
    print(f"  MRR      : {summary['mrr']:.3f}")
    print(f"  耗时     : {summary['elapsed_s']}s")
    print("\n  按故障类型:")
    for fault, stats in summary["by_fault"].items():
        print(f"    {fault:22} n={stats['count']:<3} recall@3={stats['recall_at_3']:.3f}")

    out_json = Path(f"{args.out}.json")
    out_md = Path(f"{args.out}.md")
    out_json.write_text(json.dumps({"summary": summary, "runs": predictions},
                                   ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# M4 评测报告",
        "",
        f"- 场景数：{summary['count']}",
        f"- 模型：{args.llm}",
        f"- Recall@1：**{summary['recall_at_1']:.3f}**",
        f"- Recall@3：**{summary['recall_at_3']:.3f}**",
        f"- MRR：{summary['mrr']:.3f}",
        f"- 总耗时：{summary['elapsed_s']}s",
        "",
        "## 按故障类型",
        "",
        "| 故障类型 | 场景数 | Recall@3 |",
        "|---|---:|---:|",
    ]
    for fault, stats in summary["by_fault"].items():
        lines.append(f"| {fault} | {stats['count']} | {stats['recall_at_3']:.3f} |")
    lines += ["", "## 逐场景", "", "| 场景 | 故障 | hit@1 | hit@3 | RR | 时延(s) |", "|---|---|---|---|---:|---:|"]
    for run in predictions:
        lines.append(
            f"| {run['scenario']} | {run['fault']} | "
            f"{'✅' if run['hit@1'] else '❌'} | {'✅' if run['hit@3'] else '❌'} | "
            f"{run['rr']} | {run['latency_s']} |"
        )
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\n报告: {out_md.resolve()}")
    print(f"数据: {out_json.resolve()}")

    ok = summary["recall_at_3"] >= args.threshold
    print(f"\n门禁: Recall@3={summary['recall_at_3']:.3f} "
          f"{'>=' if ok else '<'} 阈值 {args.threshold}  -> {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
