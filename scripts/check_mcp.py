r"""Smoke-test the MCP gateway against the tool servers.

Starts every configured server over stdio, lists tools, calls one tool per
server, and prints latency + degradation status.

Run: .\.venv\Scripts\python.exe scripts\check_mcp.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "core"))

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    pass

from app.mcp_gateway import MCPGateway  # noqa: E402

CALLS = [
    ("prometheus.query_range", {"service": "order-service", "metric": "latency_p99_ms", "window": "30m"}),
    ("loki.query", {"service": "order-service", "pattern": "error", "window": "30m"}),
    ("cmdb.topology", {"service": "order-service"}),
    ("deploy.changes", {"service": "order-service", "window": "2h"}),
    ("kb.search_postmortems", {"query": "连接池耗尽", "limit": 3}),
    ("ticket.create", {"service": "order-service", "title": "核查连接池配置"}),
]


def main() -> int:
    print("=" * 70)
    print("MCP 网关连通性检查")
    print("=" * 70)

    with MCPGateway() as gw:
        servers = gw.available_servers()
        failed = gw.failed_servers()
        print(f"\n可用 server : {servers or '无'}")
        if failed:
            print("失败 server :")
            for name, err in failed.items():
                print(f"  - {name}: {err[:120]}")

        tools = gw.tools()
        print(f"\n共发现 {len(tools)} 个工具:")
        for t in tools:
            print(f"  - {t['name']}")

        print("\n调用测试:")
        failures = 0
        for name, args in CALLS:
            result = gw.call(name, **args)
            status = "OK  " if result.ok else "FAIL"
            print(f"  [{status}] {name:28} {result.latency_ms:>4} ms  {result.error[:70]}")
            if not result.ok:
                failures += 1
            elif isinstance(result.data, dict):
                source = result.data.get("source", "-")
                print(f"          data.source={source}")

        print(f"\n审计记录: {len(gw.audit)} 条")
        print(f"结果: {len(CALLS) - failures}/{len(CALLS)} 成功")
        return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
