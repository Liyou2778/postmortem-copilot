r"""Raw JSON-RPC probe of the ContextDB MCP endpoint.

The mcp SDK wrapped the failure in an opaque ExceptionGroup, so this speaks the
streamable-HTTP protocol by hand to expose status codes, headers and bodies.
It performs the standard handshake: initialize -> notifications/initialized ->
tools/list.
"""
from __future__ import annotations

import json
import pathlib
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    pass

import httpx

ENV = pathlib.Path("/opt/copilot/.env")
PROTOCOL = "2025-03-26"


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


def parse_body(resp: httpx.Response):
    """Streamable HTTP may answer as JSON or as an SSE stream."""
    ctype = resp.headers.get("content-type", "")
    text = resp.text
    if "text/event-stream" in ctype:
        payloads = []
        for line in text.splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk:
                    try:
                        payloads.append(json.loads(chunk))
                    except json.JSONDecodeError:
                        payloads.append(chunk)
        return payloads or text[:400]
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return text[:400]


def main() -> int:
    env = load_env(ENV)
    key = env.get("CONTEXTDB_API_KEY", "")
    base = env.get("CONTEXTDB_ENDPOINT", "https://context-database.aliyuncs.com").rstrip("/")
    url = f"{base}/v1/mcp"
    if not key:
        print("CONTEXTDB_API_KEY 未配置")
        return 2

    for auth_scheme in ("Bearer", "Token"):
        headers = {
            "Authorization": f"{auth_scheme} {key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        print(f"\n{'='*60}\n认证方式: {auth_scheme}\n{'='*60}")
        with httpx.Client(timeout=30.0) as c:
            # 1) initialize
            r = c.post(url, headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL,
                    "capabilities": {},
                    "clientInfo": {"name": "ctxdb-probe", "version": "0.1.0"},
                },
            })
            print(f"[initialize] HTTP {r.status_code}")
            print("  resp headers:", {k: v for k, v in r.headers.items()
                                      if k.lower() in ("mcp-session-id", "content-type", "www-authenticate")})
            body = parse_body(r)
            print("  body:", json.dumps(body, ensure_ascii=False)[:400] if not isinstance(body, str) else body)

            if r.status_code >= 400:
                continue

            session_id = r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id")
            if session_id:
                headers["mcp-session-id"] = session_id
                print(f"  session: {session_id}")

            # 2) notifications/initialized
            r2 = c.post(url, headers=headers, json={
                "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
            })
            print(f"[initialized] HTTP {r2.status_code}")

            # 3) tools/list
            r3 = c.post(url, headers=headers, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
            })
            print(f"[tools/list] HTTP {r3.status_code}")
            data = parse_body(r3)
            if isinstance(data, list) and data:
                data = data[-1]
            if isinstance(data, dict) and "result" in data:
                tools = data["result"].get("tools", [])
                print(f"  工具数: {len(tools)}")
                for t in tools:
                    print(f"\n  ■ {t.get('name')}")
                    desc = (t.get("description") or "")[:240]
                    if desc:
                        print(f"    {desc}")
                    schema = t.get("inputSchema") or {}
                    required = set(schema.get("required", []))
                    for pname, pdef in (schema.get("properties") or {}).items():
                        mark = "*" if pname in required else " "
                        print(f"      {mark}{pname:<24} {pdef.get('type','?'):<8} "
                              f"{(pdef.get('description') or '')[:100]}")
                return 0
            print("  body:", json.dumps(data, ensure_ascii=False)[:600] if not isinstance(data, str) else data)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
