import re
import sys

sys.path.insert(0, "services/core")
import httpx
from app.config import get

base = "http://127.0.0.1:8000"
user = get("ADMIN_USER", "admin")
password = get("ADMIN_PASSWORD", "")
with httpx.Client(timeout=15, auth=(user, password), follow_redirects=False) as client:
    page = client.get(base + "/admin/catalog")
    print("admin_page", page.status_code,
          "csrf_present", bool(re.search(r'name="csrf_token"', page.text)),
          "cookie", "csrf_token" in client.cookies)
    missing = client.post(base + "/admin/catalog", data={"name": "csrf-check"})
    print("post_without_token", missing.status_code)
