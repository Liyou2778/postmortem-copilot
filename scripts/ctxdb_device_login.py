r"""Complete the ContextDB OAuth device flow and store the resulting token.

Why this exists: the workspace API key (``Token ctxdb-...``) authenticates the
"DATA API contract", which declares only knowledge/skill routes. The memory API
(``/v1/memories``) is served for OAuth **session** tokens obtained through the
device authorization grant — that is how the official ctxdb CLI gets them.

Flow implemented here (RFC 8628):
    POST /v1/auth/device/authorize   -> user_code + device_code
    (user opens the console page and approves)
    POST /v1/auth/device/token       -> access_token + refresh_token

Usage (on the ECS):
    python scripts/ctxdb_device_login.py --start          # print code + URL
    python scripts/ctxdb_device_login.py --poll <device_code>
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    pass

import httpx

BASE = "https://api.cn-hangzhou.agentcontext.aliyuncs.com"
AUTH_PAGE = "https://rdsnext.console.aliyun.com/contextDbCliAuthorization/cn-hangzhou"
CLIENT_ID = "ctxdb-cli"
GRANT = "urn:ietf:params:oauth:grant-type:device_code"
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


def write_env(pairs: dict[str, str]) -> None:
    lines = ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []
    seen: set[str] = set()
    for i, line in enumerate(lines):
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key = line.split("=", 1)[0].strip()
        if key in pairs:
            lines[i] = f"{key}='{pairs[key]}'"
            seen.add(key)
    for k, v in pairs.items():
        if k not in seen:
            lines.append(f"{k}='{v}'")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    import os

    os.chmod(ENV, 0o600)


def start() -> int:
    with httpx.Client(timeout=30) as c:
        r = c.post(f"{BASE}/v1/auth/device/authorize", json={"client_id": CLIENT_ID})
    if r.status_code >= 400:
        print("authorize 失败", r.status_code, r.text[:300])
        return 1
    data = r.json().get("data") or {}
    device_code = data.get("device_code", "")
    user_code = data.get("user_code", "")
    print(json.dumps({"device_code": device_code, "user_code": user_code,
                      "expires_in": data.get("expires_in"),
                      "interval": data.get("interval")}, ensure_ascii=False))
    print()
    print("授权链接:", f"{AUTH_PAGE}?user_code={user_code}")
    print("用户码  :", user_code)
    return 0


def poll(device_code: str, timeout_s: int = 600, interval_s: int = 5) -> int:
    deadline = time.time() + timeout_s
    with httpx.Client(timeout=30) as c:
        while time.time() < deadline:
            r = c.post(f"{BASE}/v1/auth/device/token", json={
                "grant_type": GRANT, "client_id": CLIENT_ID, "device_code": device_code,
            })
            try:
                payload = r.json()
            except Exception:  # noqa: BLE001
                payload = {}
            if r.status_code < 400:
                data = payload.get("data") or payload
                token = data.get("access_token")
                if token:
                    write_env({
                        "CONTEXTDB_OAUTH_ACCESS_TOKEN": token,
                        "CONTEXTDB_OAUTH_REFRESH_TOKEN": data.get("refresh_token") or "",
                        "CONTEXTDB_WORKSPACE_ID": data.get("workspace_id") or "",
                        "CONTEXTDB_MEMBER_ID": data.get("member_id") or "",
                    })
                    print("授权成功")
                    print("  workspace:", data.get("workspace_id"))
                    print("  member   :", data.get("member_id"))
                    print("  scope    :", data.get("scope"))
                    print("  expires_in:", data.get("expires_in"))
                    return 0
            err = (payload.get("data") or {}).get("error") if isinstance(payload.get("data"), dict) else None
            err = err or payload.get("error") or payload.get("errorCode")
            if err in ("authorization_pending", "slow_down", 0, None):
                print(f"  等待授权… ({int(deadline - time.time())}s 剩余)")
                time.sleep(interval_s)
                continue
            print("授权失败:", json.dumps(payload, ensure_ascii=False)[:300])
            return 1
    print("超时未完成授权")
    return 1


def verify() -> int:
    env = load_env(ENV)
    token = env.get("CONTEXTDB_OAUTH_ACCESS_TOKEN", "")
    if not token:
        print("没有 OAuth token")
        return 2
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    with httpx.Client(timeout=30, headers=h) as c:
        print("\n=== 用 Bearer token 探测记忆接口 ===")
        for method, path, body in [
            ("GET", "/v1/memories?user_id=liyou&page=1&page_size=5", None),
            ("POST", "/v1/memories", {"user_id": "liyou", "agent_id": "postmortem-copilot",
                                      "messages": [{"role": "user", "content": "order-service 连接池耗尽导致延迟"}]}),
        ]:
            r = c.request(method, BASE + path, json=body)
            print(f"  {method:<5} {path:<52} HTTP {r.status_code}  {r.text[:220]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", action="store_true")
    ap.add_argument("--poll", default="")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    if args.start:
        return start()
    if args.poll:
        return poll(args.poll)
    if args.verify:
        return verify()
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
