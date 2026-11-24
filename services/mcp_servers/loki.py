"""MCP server: Loki (read-only log search).

The caller supplies the stream selector and the datasource URL (from the
service catalog); this server only executes the query and labels the result.
An unreachable upstream degrades to a fixture marked ``source: fixture``.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from services.mcp_servers._shared import (  # noqa: E402
    ToolSecurityError,
    assert_host_allowed,
    fixture_note,
    http_get_json,
    parse_allowlist,
    redact_deep,
)

mcp = FastMCP("loki-readonly")

BASE_URL = os.getenv("LOKI_URL", "http://127.0.0.1:9200")
ALLOWLIST = parse_allowlist("LOKI_ALLOWLIST", "localhost,127.0.0.1,loki")


def _ts(seconds: int) -> str:
    return datetime.fromtimestamp(time.time() - seconds, timezone.utc).isoformat()


def parse_window(window: str) -> int:
    text = (window or "30m").strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if len(text) < 2 or text[-1] not in units:
        return 1800
    try:
        return max(60, int(float(text[:-1])) * units[text[-1]])
    except ValueError:
        return 1800


def build_query(selector: str, pattern: str, service: str) -> str:
    """Build a Loki selector + line filter.

    ``re:`` opts into the regex filter. The negative filter removes the MCP
    server's own request logs, which would otherwise match the error regex and
    become self-referential evidence (they contain the query text itself).
    """
    sel = (selector or "").strip()
    if sel.startswith("{") and sel.endswith("}"):
        sel = sel[1:-1].strip()
    if not sel:
        escaped = service.replace("\\", "\\\\").replace('"', '\\"')
        sel = f'service="{escaped}"'
    if pattern.startswith("re:"):
        expr = (
            f'{{{sel}}} |~ "{pattern[3:]}" '
            '!~ "loki/api/v1|Processing request of type"'
        )
    else:
        expr = f'{{{sel}}} |= "{pattern}"'
    return expr


def _fixture(selector: str, query: str, datasource: str) -> dict[str, Any]:
    lines = [
        {"t": _ts(420), "line": "level=error msg=\"connection pool exhausted\" svc=order user=13800138000"},
        {"t": _ts(360), "line": "level=error msg=\"upstream timeout\" host=pay-gw.corp"},
        {"t": _ts(300), "line": "level=warn msg=\"retry 3/3\" ip=10.12.3.44"},
    ]
    return {
        "count": len(lines),
        "lines": lines,
        "selector": selector,
        "query": query,
        "datasource": datasource,
        "data_mode": "fixture",
    }


@mcp.tool()
def query(
    service: str,
    pattern: str = "error",
    window: str = "30m",
    selector: str = "",
    datasource_url: str = "",
    data_mode: str = "real",
) -> dict[str, Any]:
    """按服务与关键字检索日志。

    Args:
        service: 服务名，例如 order-service
        pattern: 关键字，例如 error
        window: 时间窗，例如 30m
        selector: Loki 流选择器，由服务目录注入，例如 service="order-service"
        datasource_url: 数据源地址，由服务目录注入
        data_mode: real / mock，用于证据可信度标注
    """
    base = (datasource_url or BASE_URL).strip().rstrip("/")
    query_expr = build_query(selector, pattern, service)
    seconds = parse_window(window)
    url = f"{base}/loki/api/v1/query_range"
    try:
        assert_host_allowed(url, ALLOWLIST)
        payload = http_get_json(
            url,
            ALLOWLIST,
            params={
                "query": query_expr,
                "limit": 100,
                "start": _ts(seconds),
                "end": _ts(0),
                "window": window,
            },
        )
        streams = (payload.get("data") or {}).get("result") or []
        lines: list[dict[str, Any]] = []
        for stream in streams:
            for entry in stream.get("values") or []:
                if isinstance(entry, list) and len(entry) == 2:
                    lines.append({"t": entry[0], "line": entry[1]})
        lines.sort(key=lambda x: str(x["t"]), reverse=True)
        return redact_deep(
            {
                "source": "loki",
                "data_mode": data_mode,
                "datasource": base,
                "count": len(lines),
                "lines": lines[:50],
                "selector": selector,
                "query": query_expr,
            }
        )
    except ToolSecurityError as exc:
        return fixture_note(_fixture(sel, query_expr, base),
                            f"url blocked by allowlist: {exc}")
    except Exception as exc:  # noqa: BLE001 - degrade, but label it
        return fixture_note(_fixture(sel, query_expr, base), f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    mcp.run()
