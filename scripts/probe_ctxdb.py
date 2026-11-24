r"""Probe the RDS ContextDB API before writing any business code.

Why a probe first: the docs describe operations (AddMemories / SearchMemories /
knowledge query) but the exact paths and required fields are easy to get wrong.
Guessing produces code that "looks right" and 404s in production — the same
trap as the OSS signed-URL and the quoted-password issues found earlier.

This script only reads what it is told and reports status codes verbatim.
It creates at most one knowledge base named `probe-test` so the workspace is
not polluted; that base is safe to delete from the console afterwards.

Run on the ECS:
    python scripts/probe_ctxdb.py
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


def show(resp: httpx.Response, limit: int = 400) -> str:
    body = resp.text
    if len(body) > limit:
        body = body[:limit] + "…"
    return f"HTTP {resp.status_code}  {body}"


def main() -> int:
    env = load_env(ENV)
    key = env.get("CONTEXTDB_API_KEY", "")
    base = env.get("CONTEXTDB_ENDPOINT", "https://context-database.aliyuncs.com").rstrip("/")
    if not key:
        print("CONTEXTDB_API_KEY 未配置")
        return 2

    headers = {"Authorization": f"Token {key}", "Content-Type": "application/json"}
    print(f"endpoint: {base}")
    print(f"key     : {key[:12]}... (len={len(key)})\n")

    with httpx.Client(timeout=30.0, headers=headers) as c:
        # ---- 1. documented knowledge-base write path ----
        print("[1] POST /v1/knowledge/knowledge_bases（文档给出的路径）")
        r = c.post(f"{base}/v1/knowledge/knowledge_bases",
                   json={"name": "probe-test", "description": "api probe, safe to delete"})
        print("   ", show(r))
        kb_id = ""
        try:
            data = r.json()
            kb_id = (data.get("data") or {}).get("id") or (data.get("data") or {}).get("kb_id") or ""
            if not kb_id and isinstance(data.get("data"), dict):
                kb_id = data["data"].get("knowledge_base_id", "")
        except Exception:
            pass
        print(f"    kb_id = {kb_id or '(未解析到)'}")

        # ---- 2. documented knowledge query ----
        print("\n[2] POST /v1/knowledge/query（知识召回）")
        r = c.post(f"{base}/v1/knowledge/query",
                   json={"question": "连接池耗尽的原因", "dataset_ids": [], "top_k": 3})
        print("   ", show(r))

        # ---- 3. memory endpoints: discover the real path ----
        print("\n[3] 记忆接口路径探测（404/405 说明路径不对，401/403 说明路径对但无权）")
        candidates = [
            ("POST", "/v1/memory/memories", {"messages": [{"role": "user", "content": "probe"}]}),
            ("POST", "/v1/memories", {"messages": [{"role": "user", "content": "probe"}]}),
            ("POST", "/v1/memory/add", {"messages": [{"role": "user", "content": "probe"}]}),
            ("GET", "/v1/memory/memories", None),
            ("GET", "/v1/memories", None),
            ("POST", "/v1/memory/search", {"query": "probe"}),
            ("POST", "/v1/memory/search_memories", {"query": "probe"}),
            ("GET", "/v1/memory/search", None),
        ]
        hits = []
        for method, path, body in candidates:
            try:
                r = c.request(method, f"{base}{path}", json=body)
                tag = "★ 可能可用" if r.status_code < 400 else ""
                if r.status_code not in (404, 405):
                    hits.append((method, path, r.status_code))
                print(f"    {method:<5} {path:<32} HTTP {r.status_code} {tag}")
                if r.status_code < 400:
                    print(f"          {show(r, 200)}")
            except Exception as exc:  # noqa: BLE001
                print(f"    {method:<5} {path:<32} ERROR {type(exc).__name__}: {exc}")

        # ---- 4. list things we can see, to infer the surface ----
        print("\n[4] 枚举可访问的 GET 资源")
        for path in ("/v1/knowledge/knowledge_bases", "/v1/memory", "/v1/health",
                     "/v1/memories?limit=1", "/v1/knowledge/documents"):
            try:
                r = c.get(f"{base}{path}")
                print(f"    GET {path:<34} HTTP {r.status_code}  {r.text[:120]}")
            except Exception as exc:  # noqa: BLE001
                print(f"    GET {path:<34} ERROR {type(exc).__name__}")

    print("\n=== 结论 ===")
    if hits:
        print("  非 404/405 的记忆类端点:", hits)
    else:
        print("  记忆类端点全部 404/405 —— 路径与文档描述不同，需要进一步查文档")
    print("  知识库写入/查询若返回 2xx，说明 Key 与端点可用，可以开始集成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
