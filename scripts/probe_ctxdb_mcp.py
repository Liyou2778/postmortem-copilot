r"""Discover the ContextDB memory API through its MCP endpoint.

Why: the REST memory routes aren't documented publicly and every guessed path
returns 403 "route is not declared in the DATA API contract". The MCP endpoint
(``/v1/mcp``) advertises the server's tool schema, which names the real
operations and their parameters — enough to reconstruct the REST calls or, if
the schema is good, to call MCP directly.

Run on the ECS:
    python scripts/probe_ctxdb_mcp.py
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    pass

ENV = pathlib.Path("/opt/copilot/.env")


def load_env(path: pathlib.Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        env[k.strip()] = v
    return env


async def discover(url: str, key: str) -> int:
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
    except Exception as exc:  # noqa: BLE001
        print(f"mcp 客户端导入失败: {type(exc).__name__}: {exc}")
        return 2

    headers = {"Authorization": f"Bearer {key}"}
    print(f"MCP endpoint: {url}")
    print(f"auth        : Bearer {key[:12]}...\n")

    try:
        async with streamablehttp_client(url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                print("initialize OK")
                server = getattr(init, "serverInfo", None)
                if server:
                    print(f"  server: {server.name} {server.version}")

                tools = await session.list_tools()
                print(f"\n工具数: {len(tools.tools)}")
                for t in tools.tools:
                    print(f"\n  ■ {t.name}")
                    if t.description:
                        print(f"    描述: {t.description[:300]}")
                    schema = t.inputSchema or {}
                    props = schema.get("properties", {})
                    required = set(schema.get("required", []))
                    for pname, pdef in props.items():
                        mark = "*" if pname in required else " "
                        ptype = pdef.get("type", "?")
                        desc = (pdef.get("description") or "")[:120]
                        print(f"      {mark}{pname:<22} {ptype:<8} {desc}")
                    json.dump(t.inputSchema, sys.stdout, ensure_ascii=False)
                    print()
                return 0
    except Exception as exc:  # noqa: BLE001
        print(f"MCP 调用失败: {type(exc).__name__}: {exc}")
        return 1


def main() -> int:
    env = load_env(ENV)
    key = env.get("CONTEXTDB_API_KEY", "")
    base = env.get("CONTEXTDB_ENDPOINT", "https://context-database.aliyuncs.com").rstrip("/")
    if not key:
        print("CONTEXTDB_API_KEY 未配置")
        return 2
    return asyncio.run(discover(f"{base}/v1/mcp", key))


if __name__ == "__main__":
    raise SystemExit(main())
