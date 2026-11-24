"""MCP server: ticket creation (WRITE tool).

This is the only mutating tool in the toolkit. It is declared explicitly as a
write operation so the orchestrator must register a compensation for it; the
read-only guard never silently allows writes.
"""
from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from services.mcp_servers._shared import redact_deep  # noqa: E402

mcp = FastMCP("ticket-write")

_CREATED: dict[str, dict[str, Any]] = {}


@mcp.tool()
def create(service: str, title: str) -> dict[str, Any]:
    """创建一个跟进工单（写操作）。

    幂等：同一 (service, title) 重复调用返回同一工单号，避免排队重试时
    产生重复工单。

    Args:
        service: 服务名
        title: 工单标题
    """
    digest = hashlib.sha256(f"{service}|{title}".encode()).hexdigest()[:8].upper()
    ticket_id = f"T-{digest}"
    if ticket_id not in _CREATED:
        _CREATED[ticket_id] = {
            "ticket_id": ticket_id,
            "service": service,
            "title": title,
            "status": "open",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": "ticket",
        }
    return redact_deep(dict(_CREATED[ticket_id]))


if __name__ == "__main__":
    mcp.run()
