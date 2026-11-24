"""Shared helpers for the MCP tool servers.

Each server is an independently deployable process, so this module deliberately
has no dependency on the core application. Only the standard library plus
``httpx`` are used.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import urlparse

import httpx

# These child processes write to the same systemd unit as the API, and the
# journald collector ships that unit to Loki. Their INFO logs contain the full
# PromQL/LogQL query, which then matches the LogAgent's own error regex on the
# next query (self-referential evidence). Keep only warnings/errors.
for _noisy in ("httpx", "httpcore", "mcp", "mcp.server", "mcp.client"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b1[3-9]\d{9}\b"), "<phone>"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b"), "<email>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
    (re.compile(r"\b[\w-]+\.(?:internal|corp|local|lan)\b", re.IGNORECASE), "<internal-host>"),
)


class ToolSecurityError(RuntimeError):
    """Raised when a request would violate the tool's safety policy."""


def redact(text: str) -> str:
    if not text:
        return text
    out = text
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def redact_deep(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_deep(v) for v in value]
    if isinstance(value, dict):
        return {k: redact_deep(v) for k, v in value.items()}
    return value


def parse_allowlist(env_name: str, default: str = "localhost,127.0.0.1") -> tuple[str, ...]:
    raw = os.getenv(env_name, default)
    return tuple(host.strip().lower() for host in raw.split(",") if host.strip())


def assert_host_allowed(url: str, allowlist: tuple[str, ...]) -> None:
    """SSRF guard: only http(s) to an allowlisted host (or its subdomain)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ToolSecurityError(f"unsupported scheme: {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ToolSecurityError("url has no host")
    if host in allowlist:
        return
    for allowed in allowlist:
        if host.endswith("." + allowed):
            return
    raise ToolSecurityError(f"host {host!r} is not allowlisted")


def http_get_json(
    url: str,
    allowlist: tuple[str, ...],
    timeout: float = 8.0,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """GET JSON with an SSRF guard. Raises on any failure; callers decide how
    to degrade."""
    assert_host_allowed(url, allowlist)
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


def fixture_note(payload: dict[str, Any], reason: str) -> dict[str, Any]:
    """Mark a result as synthetic so downstream code never mistakes it for live
    data. This flag is preserved all the way into the evidence record."""
    payload = dict(payload)
    payload["source"] = "fixture"
    # Fixture is the lowest trust level. Set it here so every server that
    # degrades is labelled consistently, without each one remembering to.
    payload["data_mode"] = "fixture"
    payload["degraded_reason"] = reason[:160]
    return redact_deep(payload)
