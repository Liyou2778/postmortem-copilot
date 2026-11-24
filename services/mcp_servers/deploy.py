"""MCP server: deploy / config-change history (read-only)."""
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

mcp = FastMCP("deploy-readonly")

BASE_URL = os.getenv("DEPLOY_URL", "http://127.0.0.1:9400")
ALLOWLIST = parse_allowlist("DEPLOY_ALLOWLIST", "localhost,127.0.0.1,deploy")

_FIXTURE = {
    "changes": [
        {
            "change_id": "CHG-20261119-031",
            "at": "2026-11-19T10:03:00Z",
            "kind": "deploy",
            "summary": "order-service v2.31: 连接池上限 200 -> 50",
            "operator": "ci-bot",
        }
    ],
    "data_mode": "fixture",
}


@mcp.tool()
def changes(service: str, window: str = "2h", datasource_url: str = "",
            data_mode: str = "real") -> dict[str, Any]:
    """查询服务在时间窗内的发布与配置变更。

    Args:
        service: 服务名
        window: 时间窗，例如 2h
        datasource_url: 数据源地址，由服务目录注入
        data_mode: real / mock，用于证据可信度标注
    """
    base = (datasource_url or BASE_URL).strip().rstrip("/")
    url = f"{base}/api/v1/changes"
    try:
        payload = http_get_json(url, ALLOWLIST,
                                params={"service": service, "window": window})
        payload["source"] = "deploy"
        payload["data_mode"] = data_mode
        payload["datasource"] = base
        payload["service"] = service
        return redact_deep(payload)
    except ToolSecurityError as exc:
        return fixture_note({**_FIXTURE, "service": service, "window": window,
                             "datasource": base,
                             "degraded_reason": f"url blocked by allowlist: {exc}"[:160]},
                            "url blocked by allowlist")
    except Exception as exc:  # noqa: BLE001
        return fixture_note({**_FIXTURE, "service": service, "window": window,
                             "datasource": base},
                            f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    mcp.run()
