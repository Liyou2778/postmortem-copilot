"""Real-Prometheus evaluation: inject a controlled fault on the ECS, run the
actual analysis chain, and score the root-cause claims against known truth.

This is intentionally separate from the 63-scenario devstack harness:

* devstack scenarios are deterministic and CI-friendly;
* these scenarios are real host faults (CPU / memory / disk IO), slow (minutes),
  and must run on the ECS because the fault is local. They answer a different
  question: "does the chain work on real telemetry?"

Run on the ECS:
    /opt/copilot/.venv/bin/python scripts/real_eval.py --scenarios cpu,memory,disk

Output: eval-real-m9.json + eval-real-m9.md in the current directory.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

BASE = os.getenv("COPILOT_BASE", "http://127.0.0.1:8000")
FAULT_SECONDS = int(os.getenv("FAULT_SECONDS", "180"))
WARMUP_SECONDS = int(os.getenv("WARMUP_SECONDS", "45"))
# Memory faults leave page cache / allocator effects behind; without a cooldown
# the next scenario inherits a low MemAvailable and can be misclassified as a
# memory root cause. Disk-only runs passed, so this is test isolation, not a
# disk detector bug.
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "45"))
ANALYSIS_WINDOW = os.getenv("ANALYSIS_WINDOW", "3m")
OUT_JSON = Path("eval-real-m9.json")
OUT_MD = Path("eval-real-m9.md")

KEYWORDS = {
    # "负载/load" are generic symptoms (a disk fault also raises load1); they
    # must not count as CPU root-cause evidence.
    "cpu": ["cpu", "运行队列", "资源饱和", "占用", "saturat"],
    "memory": ["内存", "memory", "oom", "可用内存"],
    "disk": ["磁盘", "disk", "io", "iowait", "写入", "读写"],
}


@dataclass
class Fault:
    name: str
    processes: list[subprocess.Popen] = field(default_factory=list)

    def _popen(self, argv: list[str]) -> None:
        # `yes` and `timeout` spawn children. Without a new session, killing the
        # parent leaves the child burning CPU into the next scenario (we hit
        # exactly this during the first real-eval run).
        self.processes.append(
            subprocess.Popen(
                argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        )

    @staticmethod
    def cleanup_stale() -> None:
        """Kill any fault process that survived a previous interrupted run.

        Markers are unique enough not to match normal copilot processes; the
        first eval run leaked `yes` into the next scenario, so this is now a
        pre-flight as well as a shutdown step.
        """
        for marker in ("m9-fault-cpu", "m9-fault-memory", "m9-fault-disk"):
            try:
                subprocess.run(["pkill", "-9", "-f", marker],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               check=False)
            except Exception:  # noqa: BLE001
                pass

    def start(self) -> None:
        Fault.cleanup_stale()
        if self.name == "cpu":
            for _ in range(2):
                self._popen(
                    ["bash", "-c",
                     f"timeout {FAULT_SECONDS} yes m9-fault-cpu >/dev/null"]
                )
        elif self.name == "memory":
            code = (
                "import time\n"
                "n = 1024 * 1024 * 1024\n"
                "b = bytearray(n)\n"
                "for i in range(0, n, 4096):\n"
                "    b[i] = 1\n"
                "# m9-fault-memory marker for stale-process cleanup\n"
                f"time.sleep({FAULT_SECONDS})\n"
            )
            self._popen([sys.executable, "-c", code])
        elif self.name == "disk":
            self._popen(
                ["bash", "-c",
                 f"timeout {FAULT_SECONDS} bash -c "
                 "'while :; do dd if=/dev/zero of=/tmp/m9-disk-io.bin "
                 "bs=32M count=16 conv=fdatasync status=none; done' "
                 "# m9-fault-disk"]
            )
        else:
            raise ValueError(f"unknown fault {self.name}")
        time.sleep(3)
        if self.name == "memory":
            dead = [p for p in self.processes if p.poll() is not None]
            if dead:
                raise RuntimeError(
                    "memory fault process exited before analysis; host memory too tight"
                )

    def healthy(self) -> bool:
        """Verify the fault is actually active before asking the model to judge it."""
        if not self.processes or any(p.poll() is not None for p in self.processes):
            return False
        if self.name == "cpu":
            return os.getloadavg()[0] >= 2.5
        if self.name == "memory":
            total = 0
            for proc in self.processes:
                try:
                    with open(f"/proc/{proc.pid}/status", encoding="utf-8") as fh:
                        for line in fh:
                            if line.startswith("VmRSS:"):
                                total += int(line.split()[1]) * 1024
                except OSError:
                    pass
            return total >= 500 * 1024 * 1024
        return True

    def stop(self) -> None:
        for proc in self.processes:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:  # noqa: BLE001
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
        for proc in self.processes:
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:  # noqa: BLE001
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
        if self.name == "disk":
            try:
                os.remove("/tmp/m9-disk-io.bin")
            except OSError:
                pass


def hit(text: str, keywords: list[str]) -> bool:
    """Keyword match with an ASCII word boundary.

    A naive substring check scored "exception" as a disk hit because it
    contains the letters "io"; that is exactly the kind of false positive that
    makes an eval report untrustworthy.
    """
    lowered = (text or "").lower()
    for keyword in keywords:
        needle = keyword.lower()
        if needle.isascii():
            if re.search(rf"(?<![a-z]){re.escape(needle)}(?![a-z])", lowered):
                return True
        elif needle in lowered:
            return True
    return False


def run_scenario(name: str, client: httpx.Client, base: str) -> dict:
    print(f"\n=== {name} ===", flush=True)
    fault = Fault(name)
    result: dict = {
        "scenario": name,
        "fault_seconds": FAULT_SECONDS,
        "analysis_window": ANALYSIS_WINDOW,
        "keywords": KEYWORDS[name],
    }
    try:
        fault.start()
        print(f"fault started; warming {WARMUP_SECONDS}s", flush=True)
        time.sleep(WARMUP_SECONDS)
        if not fault.healthy():
            raise RuntimeError(
                f"{name} fault is not active after warmup; refusing to score "
                "a scenario that may not have been injected"
            )
        created = client.post(f"{base}/api/v1/tasks",
                              json={"title": f"real-eval-{name}"})
        created.raise_for_status()
        task_id = created.json()["task_id"]
        started = time.perf_counter()
        resp = client.post(
            f"{base}/api/v1/tasks/{task_id}/run",
            json={"service_name": "ecs-copilot", "window": ANALYSIS_WINDOW},
        )
        resp.raise_for_status()
        data = resp.json()
        elapsed = time.perf_counter() - started
        claims = data.get("claims") or []
        evidence = data.get("evidence") or {}
        ranks = []
        for index, claim in enumerate(claims[:3], start=1):
            ranks.append({
                "rank": index,
                "hit": hit(claim.get("statement", ""), KEYWORDS[name]),
                "statement": (claim.get("statement") or "")[:300],
            })
        result.update({
            "task_id": task_id,
            "elapsed_s": round(elapsed, 2),
            "status": data.get("status"),
            "claims": len(claims),
            "rank1_hit": bool(ranks and ranks[0]["hit"]),
            "recall_at_3": any(item["hit"] for item in ranks),
            "ranks": ranks,
            "evidence_total": len(evidence),
            "evidence_real": sum(1 for ev in evidence.values()
                                 if ev.get("data_mode") == "real"),
            "impact_present": bool(data.get("impact")),
            "impact_summary": (data.get("impact") or {}).get("summary", {}),
            "tool_calls": [
                {"tool": item.get("tool"), "ok": item.get("ok"),
                 "error": (item.get("error") or "")[:100]}
                for item in (data.get("tool_calls") or [])
            ],
        })
        print(json.dumps({k: result[k] for k in
                          ("task_id", "status", "claims", "rank1_hit",
                           "recall_at_3", "evidence_real", "evidence_total",
                           "impact_present")}, ensure_ascii=False), flush=True)
    finally:
        fault.stop()
        time.sleep(COOLDOWN_SECONDS)
    return result


def write_report(results: list[dict]) -> None:
    total = len(results)
    rank1 = sum(1 for item in results if item.get("rank1_hit"))
    recall3 = sum(1 for item in results if item.get("recall_at_3"))
    OUT_JSON.write_text(json.dumps({"results": results}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    lines = [
        "# 真实 Prometheus 评测（M9）",
        "",
        "> 场景：ECS 本机受控故障；分析链：真实 Prometheus + Loki + MCP。",
        "> 样本量小，仅用于验证链路与召回方向，不作统计显著性结论。",
        "",
        f"- 场景数：{total}",
        f"- Rank1 命中：{rank1}/{total}",
        f"- Recall@3：{recall3}/{total}",
        "",
        "| 场景 | 状态 | 结论数 | Rank1 | Recall@3 | 真实证据 | 影响面 | 主要结论 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for item in results:
        first = item.get("ranks", [{}])[0].get("statement", "") if item.get("ranks") else ""
        lines.append(
            f"| {item['scenario']} | {item.get('status')} | {item.get('claims')} | "
            f"{'✅' if item.get('rank1_hit') else '❌'} | "
            f"{'✅' if item.get('recall_at_3') else '❌'} | "
            f"{item.get('evidence_real')}/{item.get('evidence_total')} | "
            f"{'✅' if item.get('impact_present') else '❌'} | "
            f"{first[:120]} |"
        )
    lines += ["", "## 逐场景明细", ""]
    for item in results:
        lines += [
            f"### {item['scenario']}",
            "",
            f"- task: `{item.get('task_id')}`",
            f"- 耗时: {item.get('elapsed_s')}s",
            f"- 关键词: {', '.join(item.get('keywords', []))}",
            f"- 影响面: `{json.dumps(item.get('impact_summary'), ensure_ascii=False)}`",
            "",
        ]
        for rank in item.get("ranks", []):
            mark = "✅" if rank["hit"] else "❌"
            lines.append(f"- {mark} rank{rank['rank']}: {rank['statement']}")
        lines.append("")
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwrote {OUT_JSON} and {OUT_MD}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenarios", default="cpu,memory,disk")
    parser.add_argument("--base", default=BASE)
    parser.add_argument("--rescore", action="store_true",
                        help="re-score an existing eval-real-m9.json without rerunning faults")
    args = parser.parse_args()
    if args.rescore:
        payload = json.loads(OUT_JSON.read_text(encoding="utf-8"))
        for item in payload.get("results", []):
            for rank in item.get("ranks", []):
                rank["hit"] = hit(rank.get("statement", ""), item.get("keywords", []))
            item["rank1_hit"] = bool(item["ranks"] and item["ranks"][0]["hit"])
            item["recall_at_3"] = any(rank["hit"] for rank in item["ranks"])
        write_report(payload["results"])
        return 0
    base = args.base.rstrip("/")
    names = [name.strip() for name in args.scenarios.split(",") if name.strip()]
    results = []
    api_key = os.getenv("COPILOT_API_KEY", "").strip()
    if not api_key:
        for env_path in ("/opt/copilot/.env", ".env"):
            try:
                for raw in open(env_path, encoding="utf-8"):
                    if raw.startswith("COPILOT_API_KEY="):
                        api_key = raw.split("=", 1)[1].strip().strip("'\"")
                        break
            except OSError:
                continue
    headers = {"X-API-Key": api_key} if api_key else {}
    with httpx.Client(timeout=300.0, headers=headers) as client:
        for name in names:
            results.append(run_scenario(name, client, base))
    write_report(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
