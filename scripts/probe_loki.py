"""Read-only smoke test for the local Loki (run on the ECS)."""
from __future__ import annotations

import json

import httpx

BASE = "http://127.0.0.1:3100"


def main() -> int:
    with httpx.Client(timeout=15.0) as client:
        ready = client.get(f"{BASE}/ready")
        print("ready:", ready.status_code, ready.text.strip())
        labels = client.get(f"{BASE}/loki/api/v1/labels")
        print("labels:", labels.status_code, labels.text[:400])
        for selector in ('{job="journal"}', '{unit="copilot.service"}'):
            resp = client.get(
                f"{BASE}/loki/api/v1/query_range",
                params={"query": selector, "limit": 3},
            )
            print(f"query {selector}:", resp.status_code)
            try:
                payload = resp.json()
            except json.JSONDecodeError:
                print("  non-JSON:", resp.text[:300])
                continue
            streams = (payload.get("data") or {}).get("result") or []
            print("  streams:", len(streams))
            for stream in streams[:2]:
                values = stream.get("values") or []
                print("  stream labels:", stream.get("stream"), "lines:", len(values))
                for ts, line in values[-2:]:
                    print("   ", ts, line[:160])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
