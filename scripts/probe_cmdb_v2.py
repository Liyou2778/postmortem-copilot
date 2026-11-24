"""Read-only probe for the v2 CMDB API; prints status/counts, never secrets."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "core"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.config import get  # noqa: E402

BASE = "http://127.0.0.1:9200"
viewer = get("CMDB_TOKEN_VIEWER").strip()
editor = get("CMDB_TOKEN_EDITOR").strip()
approver = get("CMDB_TOKEN_APPROVER").strip()

with httpx.Client(timeout=10) as client:
    no_token = client.get(f"{BASE}/api/v2/cis")
    print("v2 no-token:", no_token.status_code)
    headers = {"X-CMDB-Token": viewer}
    cis = client.get(f"{BASE}/api/v2/cis", headers=headers)
    print("v2 viewer:", cis.status_code, "cis=", len(cis.json().get("cis", []))
          if cis.status_code == 200 else cis.text[:200])
    impact = client.get(f"{BASE}/api/v2/impact/ecs-copilot?depth=2", headers=headers)
    print("v2 impact:", impact.status_code,
          impact.json() if impact.status_code == 200 else impact.text[:200])
    topology = client.get(f"{BASE}/api/v1/topology/ecs-copilot")
    print("v1 topology:", topology.status_code, topology.json())
    changes = client.get(f"{BASE}/api/v1/changes?service=ecs-copilot&window=2h")
    payload = changes.json().get("changes", [])
    print("v1 changes:", changes.status_code, len(payload))
    print("latest changes:", payload[:5])
    print("tokens configured:", bool(viewer), bool(editor), bool(approver))
