"""Print Aliyun-discovered CIs and graph impact; no secrets."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "core"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.config import get  # noqa: E402

base = "http://127.0.0.1:9200"
headers = {"X-CMDB-Token": get("CMDB_TOKEN_VIEWER").strip()}
with httpx.Client(timeout=10) as client:
    cis = client.get(f"{base}/api/v2/cis", headers=headers).json().get("cis", [])
    print("aliyun CIs:")
    print(json.dumps([
        {"ci_id": c["ci_id"], "ci_type": c["ci_type"], "owner": c.get("owner"),
         "unowned": (c.get("attributes") or {}).get("unowned")}
        for c in cis if c["ci_id"].startswith("aliyun-")
    ], ensure_ascii=False, indent=2))
    impact = client.get(f"{base}/api/v2/impact/ecs-copilot?depth=2",
                        headers=headers).json()
    print("impact affected:", impact.get("affected"))
    relations = client.get(
        f"{base}/api/v2/cis/ecs-copilot/relations?direction=downstream",
        headers=headers,
    ).json().get("relations", [])
    print("downstream relations:")
    print(json.dumps([
        {"to": r["to_ci"], "type": r["relation_type"],
         "criticality": r.get("criticality")}
        for r in relations
    ], ensure_ascii=False, indent=2))
    quality = client.get(f"{base}/api/v2/quality", headers=headers).json()
    print("quality:", quality)
