r"""Measure how many analyses the service can actually run at once.

RPS on /healthz is the wrong capacity metric for this product. The real workload
is: a handful of postmortem analyses per incident, each holding a request for
15-25 seconds while it calls tools and an LLM. What matters is therefore
*concurrent analyses*, not requests per second.

This probe runs N analyses simultaneously and reports,
  - wall time for all of them,
  - per-analysis latency,
  - the serialisation factor (wall / slowest single).

If the factor is ~1, the service handles them in parallel.
If it is ~N, they are effectively serialised — which is the expected result for
a synchronous handler on a single worker.

Run: python scripts/probe_concurrency.py --base http://HOST:8000 -n 1
     python scripts/probe_concurrency.py --base http://HOST:8000 -n 2
"""
from __future__ import annotations

import argparse
import asyncio
import time
import uuid

try:
    import sys
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    pass

import httpx


async def one(client: httpx.AsyncClient, base: str, idx: int) -> dict:
    """Create a task, then run the analysis, timing only the analysis."""
    created = await client.post(
        f"{base}/api/v1/tasks", json={"title": f"并发探针 #{idx} {uuid.uuid4().hex[:6]}"}
    )
    created.raise_for_status()
    task_id = created.json()["task_id"]

    started = time.perf_counter()
    resp = await client.post(
        f"{base}/api/v1/tasks/{task_id}/run",
        json={"service_name": "order-service", "window": "30m"},
    )
    elapsed = time.perf_counter() - started
    payload = resp.json() if resp.status_code == 200 else {}
    return {
        "idx": idx,
        "task_id": task_id,
        "status": resp.status_code,
        "seconds": round(elapsed, 2),
        "claims": len(payload.get("claims", [])),
        "model": payload.get("model_mode", "?"),
    }


async def main_async(base: str, n: int, timeout: float) -> int:
    limits = httpx.Limits(max_connections=n + 2)
    async with httpx.AsyncClient(limits=limits, timeout=httpx.Timeout(timeout)) as client:
        wall_start = time.perf_counter()
        results = await asyncio.gather(*(one(client, base, i) for i in range(n)))
        wall = time.perf_counter() - wall_start

    slowest = max(r["seconds"] for r in results)
    serialisation = wall / slowest if slowest else 0

    print(f"concurrent analyses : {n}")
    print(f"wall time           : {wall:.1f} s")
    print(f"slowest single      : {slowest:.1f} s")
    print(f"serialisation factor: {serialisation:.2f}  (~1 = parallel, ~{n} = serialised)")
    print()
    for r in results:
        print(f"  #{r['idx']}  http {r['status']}  {r['seconds']:>6.1f}s  "
              f"claims={r['claims']}  model={r['model']}")

    ok = all(r["status"] == 200 for r in results)
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("-n", "--count", type=int, default=2)
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args()
    return asyncio.run(main_async(args.base.rstrip("/"), args.count, args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
