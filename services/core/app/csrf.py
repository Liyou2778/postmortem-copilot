"""Signed CSRF tokens for HTML forms.

A double-submit cookie: the same signed token is placed in a HttpOnly cookie
and a hidden form field. Verification requires both to match and the signature
to be valid. This is stateless and works across multiple replicas.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from .config import Settings

COOKIE = "csrf_token"


def secret(settings: Settings) -> str:
    return (
        settings.oidc_state_secret or settings.oidc_client_secret
        or settings.bootstrap_admin_password or settings.admin_password
        or "dev-csrf-secret"
    )


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def issue(cfg: Settings) -> str:
    body = _b64(json.dumps(
        {"n": secrets.token_urlsafe(16), "exp": time.time() + 3600},
        separators=(",", ":"),
    ).encode("utf-8"))
    sig = hmac.new(secret(cfg).encode("utf-8"), body.encode("ascii"),
                   hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify(token: str, cfg: Settings) -> dict[str, Any] | None:
    try:
        body, sig = token.rsplit(".", 1)
        expected = hmac.new(secret(cfg).encode("utf-8"), body.encode("ascii"),
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(
            body + "=" * (-len(body) % 4)
        ))
        if float(payload.get("exp", 0)) < time.time():
            return None
        return payload
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
