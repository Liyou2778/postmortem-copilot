"""Direct probe of the Prometheus impact tool (no MCP, no LLM)."""
from __future__ import annotations

import json
import sys

sys.path.insert(0, "/opt/copilot")

from services.mcp_servers import prometheus  # noqa: E402

prometheus.BASE_URL = "http://127.0.0.1:9090"
prometheus.ALLOWLIST = ("127.0.0.1", "localhost")

result = prometheus.impact_overview(
    "ecs-copilot",
    window="5m",
    selector='service="ecs-copilot"',
    datasource_url="http://127.0.0.1:9090",
    data_mode="real",
)
print(json.dumps({
    "summary": result.get("summary"),
    "impact": result.get("impact"),
    "errors": result.get("errors"),
    "no_data": result.get("no_data"),
    "queries": {k: v.get("expr") for k, v in (result.get("queries") or {}).items()},
    "incident_samples": {
        k: (v.get("incident") or {}).get("points")
        for k, v in result.get("queries", {}).items()
    },
}, ensure_ascii=False, indent=2))
