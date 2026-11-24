"""MCP server: CMDB topology (read-only)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from services.mcp_servers._shared import (  # noqa: E402
    ToolSecurityError,
    fixture_note,
    http_get_json,
    parse_allowlist,
    redact_deep,
)

mcp = FastMCP("cmdb-readonly")

BASE_URL = os.getenv("CMDB_URL", "http://127.0.0.1:9300")
ALLOWLIST = parse_allowlist("CMDB_ALLOWLIST", "localhost,127.0.0.1,cmdb")

_FIXTURE = {
    "upstream": ["api-gateway"],
    "downstream": ["mysql-order", "redis-session", "pay-gw"],
    "instances": 6,
    "healthy_instances": 6,
    "data_mode": "fixture",
}


@mcp.tool()
def topology(service: str, datasource_url: str = "", data_mode: str = "real") -> dict[str, Any]:
    """查询服务的上下游依赖与实例健康度。

    Args:
        service: 服务名
        datasource_url: 数据源地址，由服务目录注入
        data_mode: real / mock，用于证据可信度标注
    """
    base = (datasource_url or BASE_URL).strip().rstrip("/")
    url = f"{base}/api/v1/topology/{service}"
    try:
        payload = http_get_json(url, ALLOWLIST)
        payload["source"] = "cmdb"
        payload["data_mode"] = data_mode
        payload["datasource"] = base
        payload["service"] = service
        return redact_deep(payload)
    except ToolSecurityError as exc:
        return fixture_note({**_FIXTURE, "service": service, "datasource": base,
                             "degraded_reason": f"url blocked by allowlist: {exc}"[:160]},
                            "url blocked by allowlist")
    except Exception as exc:  # noqa: BLE001
        return fixture_note({**_FIXTURE, "service": service, "datasource": base},
                            f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    mcp.run()
