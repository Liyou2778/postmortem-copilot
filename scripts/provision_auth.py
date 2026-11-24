"""Provision one tenant API key from the bootstrap platform admin.

Run after AUTH_ENABLED=true. The raw key is written to /opt/copilot/.env with
0600 and only its prefix is printed.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "core"))

import httpx  # noqa: E402

from app.config import get  # noqa: E402

BASE = os.environ.get("AUTH_BASE", "http://127.0.0.1:8000")
ENV = Path("/opt/copilot/.env")
username = get("ADMIN_USER", "admin")
password = get("ADMIN_PASSWORD", "")

with httpx.Client(timeout=20) as client:
    anon = client.get(f"{BASE}/api/v1/auth/me")
    login = client.post(f"{BASE}/api/v1/auth/login", json={
        "username": username, "password": password,
    })
    print("anonymous me:", anon.status_code, "login:", login.status_code)
    login.raise_for_status()
    token = login.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    me = client.get(f"{BASE}/api/v1/auth/me", headers=headers)
    print("me:", me.status_code, me.json())
    key_resp = client.post(
        f"{BASE}/api/v1/admin/api-keys", headers=headers,
        json={"name": "ops-ci", "tenant_id": "team-core", "role": "operator"},
    )
    print("api-key create:", key_resp.status_code)
    key_resp.raise_for_status()
    key = key_resp.json()["api_key"]
    # Verify the key works and is tenant scoped.
    key_headers = {"X-API-Key": key}
    me_key = client.get(f"{BASE}/api/v1/auth/me", headers=key_headers)
    print("api-key me:", me_key.status_code, me_key.json())
    task = client.post(f"{BASE}/api/v1/tasks", headers=key_headers,
                       json={"title": "tenant smoke"})
    print("create task with key:", task.status_code)
    task_id = task.json().get("task_id")

text = ENV.read_text(encoding="utf-8")
text = re.sub(r"^COPILOT_API_KEY=.*\n?", "", text, flags=re.MULTILINE)
text = text.rstrip() + f"\nCOPILOT_API_KEY={key}\n"
ENV.write_text(text, encoding="utf-8")
ENV.chmod(0o600)
print("stored COPILOT_API_KEY in", ENV, "prefix", key[:12] + "...",
      "tenant=team-core role=operator task=", task_id)
