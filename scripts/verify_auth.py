"""Verify auth + tenant isolation on the running deployment."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "core"))

import httpx  # noqa: E402

from app.config import get  # noqa: E402

BASE = os.environ.get("AUTH_BASE", "http://127.0.0.1:8000")
admin = (get("ADMIN_USER", "admin"), get("ADMIN_PASSWORD", ""))
api_key = get("COPILOT_API_KEY", "")
with httpx.Client(timeout=20) as anon:
    print("anonymous me:", anon.get(f"{BASE}/api/v1/auth/me").status_code)
    print("login page:", anon.get(f"{BASE}/login").status_code)
    print("admin anonymous:", anon.get(f"{BASE}/admin/cmdb").status_code)
with httpx.Client(timeout=20) as client:
    print("admin basic:", client.get(f"{BASE}/admin/cmdb", auth=admin).status_code)
    login = client.post(f"{BASE}/api/v1/auth/login", json={
        "username": admin[0], "password": admin[1],
    })
    token = login.json()["token"]
    admin_headers = {"Authorization": f"Bearer {token}"}
    print("admin me:", client.get(f"{BASE}/api/v1/auth/me",
                                 headers=admin_headers).json())
    platform_task = client.post(f"{BASE}/api/v1/tasks", headers=admin_headers,
                                json={"title": "platform task"})
    platform_id = platform_task.json()["task_id"]
    print("platform create:", platform_task.status_code)
    team_headers = {"X-API-Key": api_key}
    team_task = client.post(f"{BASE}/api/v1/tasks", headers=team_headers,
                            json={"title": "team task"})
    team_id = team_task.json()["task_id"]
    print("team create:", team_task.status_code)
    print("team key -> own task:", client.get(f"{BASE}/api/v1/tasks/{team_id}",
                                             headers=team_headers).status_code)
    print("team key -> platform task:",
          client.get(f"{BASE}/api/v1/tasks/{platform_id}", headers=team_headers).status_code)
    print("platform token -> team task:",
          client.get(f"{BASE}/api/v1/tasks/{team_id}", headers=admin_headers).status_code)
    print("team me:", client.get(f"{BASE}/api/v1/auth/me",
                                 headers=team_headers).json())
with httpx.Client(timeout=20) as anon:
    print("anonymous task:", anon.get(f"{BASE}/api/v1/tasks/{team_id}").status_code)
