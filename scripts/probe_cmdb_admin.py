"""Probe the admin CMDB UI over Basic Auth; never prints credentials."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "core"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.config import get  # noqa: E402

base = "http://127.0.0.1:8000"
user = get("ADMIN_USER", "admin")
password = get("ADMIN_PASSWORD", "")
with httpx.Client(timeout=10) as client:
    anon = client.get(f"{base}/admin/cmdb")
    print("anonymous:", anon.status_code)
    auth = client.get(f"{base}/admin/cmdb", auth=(user, password))
    print("authenticated:", auth.status_code, "bytes=", len(auth.text))
    print("contains title:", "企业级 CMDB" in auth.text)
    print("contains quality:", "数据质量" in auth.text)
    print("contains ci:", "ecs-copilot" in auth.text)
