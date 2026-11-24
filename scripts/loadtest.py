r"""Async load generator (zero extra dependencies).

Why hand-rolled instead of wrk/locust: the server already ships `httpx`, the
scenarios are trivial HTTP calls, and pulling a load-testing stack onto a 2 vCPU
box that is also the system under test would poison the measurement.

Critical: run this from a *separate* machine from the server. Generating load on
the same 2 vCPU instance competes with the service for CPU and makes every
number meaningless.

Usage:
    python scripts/loadtest.py --url http://HOST:8000/healthz -c 20 -d 15
    python scripts/loadtest.py --url http://HOST:8000/api/v1/tasks/<id> -c 10 -d 10
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from dataclasses import dataclass, field

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    pass

import httpx


@dataclass
class Result:
    latencies: list[float] = field(default_factory=list)
    errors: int = 0
    status_counts: dict[int, int] = field(default_factory=dict)
    start: float = 0.0
    end: float = 0.0

    def percentile(self, p: float) -> float:
        if not self.latencies:
            return 0.0
        ordered = sorted(self.latencies)
        idx = min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1))))
        return ordered[idx]

    @property
    def rps(self) -> float:
        elapsed = max(self.end - self.start, 1e-6)
        return len(self.latencies) / elapsed


async def worker(
    client: httpx.AsyncClient,
    url: str,
    method: str,
    body: dict | None,
    deadline: float,
    result: Result,
) -> None:
    while time.perf_counter() < deadline:
        t0 = time.perf_counter()
        try:
            if method == "POST":
                resp = await client.post(url, json=body or {})
            else:
                resp = await client.get(url)
            elapsed = (time.perf_counter() - t0) * 1000
            result.latencies.append(elapsed)
            result.status_counts[resp.status_code] = result.status_counts.get(resp.status_code, 0) + 1
            if resp.status_code >= 400:
                result.errors += 1
        except Exception:  # noqa: BLE001 - connection resets count as errors
            result.errors += 1


async def run(url: str, concurrency: int, duration: float, method: str,
              body: dict | None) -> Result:
    result = Result()
    limits = httpx.Limits(max_connections=concurrency + 5, max_keepalive_connections=concurrency)
    timeout = httpx.Timeout(30.0)
    result.start = time.perf_counter()
    deadline = result.start + duration
    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        await asyncio.gather(
            *(worker(client, url, method, body, deadline, result) for _ in range(concurrency))
        )
    result.end = time.perf_counter()
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("-c", "--concurrency", type=int, default=10)
    ap.add_argument("-d", "--duration", type=float, default=10.0)
    ap.add_argument("--method", default="GET", choices=["GET", "POST"])
    ap.add_argument("--body", default="", help="inline JSON body (quoting is fragile in shells)")
    ap.add_argument("--body-file", default="", help="path to a JSON file, preferred over --body")
    args = ap.parse_args()

    import json

    if args.body_file:
        # utf-8-sig: PowerShell's UTF8 encoding writes a BOM, which json.load
        # rejects with a confusing "unexpected BOM" error.
        with open(args.body_file, encoding="utf-8-sig") as fh:
            body = json.load(fh)
    elif args.body:
        body = json.loads(args.body)
    else:
        body = None
    result = asyncio.run(run(args.url, args.concurrency, args.duration, args.method, body))

    total = len(result.latencies)
    ok = total - result.errors
    print(f"target      : {args.url}")
    print(f"concurrency : {args.concurrency}   duration: {args.duration}s")
    print(f"requests    : {total}  ok={ok}  errors={result.errors}")
    print(f"RPS         : {result.rps:.1f}")
    if total:
        print(f"latency p50 : {result.percentile(50):.1f} ms")
        print(f"latency p95 : {result.percentile(95):.1f} ms")
        print(f"latency p99 : {result.percentile(99):.1f} ms")
        print(f"latency max : {max(result.latencies):.1f} ms")
        print(f"latency mean: {statistics.fmean(result.latencies):.1f} ms")
    if result.status_counts:
        print(f"status      : {dict(sorted(result.status_counts.items()))}")
    return 0 if result.errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
