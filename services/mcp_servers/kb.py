"""MCP server: historical postmortem knowledge base (read-only).

In production this would query a vector store; the interface is what matters
here, and the fixture keeps the chain testable offline.
"""
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

mcp = FastMCP("kb-readonly")

BASE_URL = os.getenv("KB_URL", "http://127.0.0.1:9500")
ALLOWLIST = parse_allowlist("KB_ALLOWLIST", "localhost,127.0.0.1,kb")

_FIXTURE = {
    "hits": [
        {"doc": "PM-2026-014", "title": "订单服务连接池打满导致支付超时", "score": 0.87},
        {"doc": "PM-2025-102", "title": "大促前配置变更未灰度", "score": 0.62},
    ]
}


@mcp.tool()
def search_postmortems(query: str, limit: int = 3) -> dict[str, Any]:
    """检索历史复盘，用于对照同类故障。

    Args:
        query: 检索语句
        limit: 返回条数上限
    """
    url = f"{BASE_URL}/api/v1/search"
    try:
        payload = http_get_json(url, ALLOWLIST, params={"q": query, "limit": limit})
        payload["source"] = "kb"
        return redact_deep(payload)
    except ToolSecurityError:
        return fixture_note({**_FIXTURE, "query": query}, "url blocked by allowlist")
    except Exception as exc:  # noqa: BLE001
        return fixture_note({**_FIXTURE, "query": query}, f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    mcp.run()
