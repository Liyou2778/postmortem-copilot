import re
import httpx

base = "http://127.0.0.1:8000"
with httpx.Client(follow_redirects=False, timeout=15) as client:
    page = client.get(base + "/login")
    print("login_page", page.status_code,
          "token_present", bool(re.search(r'name="csrf_token"', page.text)))
    missing = client.post(base + "/login", data={"username": "admin", "password": "x"})
    print("no_csrf", missing.status_code)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    bad = client.post(base + "/login", data={
        "username": "admin", "password": "wrong", "csrf_token": token,
    })
    print("bad_password_with_csrf", bad.status_code)
