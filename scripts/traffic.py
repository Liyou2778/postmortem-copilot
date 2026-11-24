"""Small HTTP load generator for impact-stat verification.

Usage:
    python scripts/traffic.py --seconds 60 --concurrency 20

It deliberately hits only cheap, unauthenticated routes. The point is to put
real request-rate/histogram samples into Prometheus, not to stress the LLM.
"""
from __future__ import annotations

import argparse
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--paths", default="/,/healthz,/api/v1/tasks/does-not-exist")
    args = parser.parse_args()
    paths = [p.strip() for p in args.paths.split(",") if p.strip()]
    counts: Counter[int] = Counter()
    deadline = time.time() + args.seconds
    urls = [f"{args.base}{p}" for p in paths]

    def one(client: httpx.Client, url: str) -> None:
        try:
            resp = client.get(url)
            counts[resp.status_code] += 1
        except Exception as exc:  # noqa: BLE001
            counts[0] += 1
            print("request failed:", type(exc).__name__)

    with httpx.Client(timeout=10.0, follow_redirects=False) as client:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            idx = 0
            while time.time() < deadline:
                pool.submit(one, client, urls[idx % len(urls)])
                idx += 1
                time.sleep(0.01)
    print("requests:", idx, "status:", dict(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
