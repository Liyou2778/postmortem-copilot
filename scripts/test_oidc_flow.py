"""End-to-end OIDC authorization-code test against the deployed Keycloak.

Reads the test password from OIDC_TEST_PASSWORD_FILE; never prints it.
"""
from __future__ import annotations

import html as html_lib
import os
import re
import sys
from urllib.parse import urljoin

import httpx

APP = os.getenv("APP_BASE", "http://121.41.224.245:8000")
USER = os.getenv("OIDC_TEST_USER", "sso-test")
PW_FILE = os.getenv("OIDC_TEST_PASSWORD_FILE", "")


def main() -> int:
    if not PW_FILE:
        print("OIDC_TEST_PASSWORD_FILE is required")
        return 2
    password = open(PW_FILE, encoding="utf-8").read().strip()
    with httpx.Client(follow_redirects=False, timeout=30.0) as client:
        start = client.get(APP + "/api/v1/auth/oidc/login")
        print("app login:", start.status_code, "->", start.headers.get("location", "")[:120])
        if start.status_code != 302:
            return 1
        login_url = start.headers["location"]
        page = client.get(login_url)
        print("keycloak login page:", page.status_code)
        action = re.search(r'action="([^"]+)"', page.text)
        if not action:
            print("no form action")
            return 1
        post_url = urljoin(str(page.url), html_lib.unescape(action.group(1)))
        hidden = {
            html_lib.unescape(name): html_lib.unescape(value)
            for name, value in re.findall(
                r'<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"', page.text
            )
            if name not in ("username", "password")
        }
        data = {**hidden, "username": USER, "password": password}
        response = client.post(post_url, data=data, follow_redirects=True)
        print("after login:", response.status_code, "url:", str(response.url)[:120])
        me = client.get(APP + "/api/v1/auth/me")
        print("me:", me.status_code, me.text[:200])
        ok = response.status_code == 200 and me.status_code == 200
        print("OIDC_FLOW_OK" if ok else "OIDC_FLOW_FAILED")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
