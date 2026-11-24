"""Admin UI for the enterprise CMDB.

The browser only talks to the core app; CMDB role tokens stay server-side and
are attached by this proxy. This keeps the control-plane credentials out of the
HTML/JS and reuses the existing Basic-Auth admin boundary.
"""
from __future__ import annotations

import base64
import binascii
import secrets
from typing import Any
from urllib.parse import parse_qs, quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .config import Settings
from .render import cmdb_admin_page, cmdb_ci_page


def build_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/admin/cmdb")

    def require_admin(request: Request) -> None:
        password = settings.admin_password
        if not password:
            return
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("basic "):
            raise HTTPException(
                status_code=401, detail="admin auth required",
                headers={"WWW-Authenticate": 'Basic realm="postmortem-copilot"'},
            )
        try:
            decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
            user, _, supplied = decoded.partition(":")
        except (binascii.Error, UnicodeDecodeError):
            raise HTTPException(401, "invalid basic auth")
        if not (
            secrets.compare_digest(user, settings.admin_user)
            and secrets.compare_digest(supplied, password)
        ):
            raise HTTPException(401, "invalid credentials")

    router.dependencies.append(Depends(require_admin))

    def token(role: str) -> str:
        return getattr(settings, f"cmdb_token_{role}", "")

    def call(method: str, path: str, role: str = "viewer",
             json_body: dict[str, Any] | None = None,
             params: dict[str, Any] | None = None) -> Any:
        auth = token(role)
        if not auth:
            raise RuntimeError(f"CMDB {role} token is not configured")
        headers = {"X-CMDB-Token": auth, "X-CMDB-Actor": "admin-ui"}
        with httpx.Client(base_url=settings.cmdb_url, timeout=15) as client:
            resp = client.request(method, path, headers=headers, json=json_body,
                                  params=params)
            if resp.status_code >= 400:
                raise RuntimeError(f"CMDB {resp.status_code}: {resp.text[:300]}")
            return resp.json()

    async def form(request: Request) -> dict[str, str]:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        return {key: values[0] for key, values in parsed.items() if values}

    def flash(message: str = "", error: str = "") -> RedirectResponse:
        query = ""
        if message:
            query = f"?msg={quote(message)}"
        elif error:
            query = f"?error={quote(error[:300])}"
        return RedirectResponse(f"/admin/cmdb{query}", status_code=303)

    @router.get("", response_class=HTMLResponse)
    def page(request: Request) -> HTMLResponse:
        message = request.query_params.get("msg", "")
        error = request.query_params.get("error", "")
        try:
            cis = call("GET", "/api/v2/cis").get("cis", [])
            quality = call("GET", "/api/v2/quality")
            changes = call("GET", "/api/v2/changes", params={"limit": 50}).get(
                "changes", []
            )
            audit: list[dict] = []
            if token("approver"):
                audit = call("GET", "/api/v2/audit", "approver").get("audit", [])
            return HTMLResponse(cmdb_admin_page(cis, quality, changes, audit,
                                                message=message, error=error))
        except Exception as exc:  # noqa: BLE001 - render a useful admin error
            return HTMLResponse(
                cmdb_admin_page([], {}, [], [], message=message,
                                error=f"{type(exc).__name__}: {exc}"),
                status_code=503,
            )

    @router.get("/ci/{ci_id}", response_class=HTMLResponse)
    def ci_page(ci_id: str) -> HTMLResponse:
        try:
            ci = call("GET", f"/api/v2/cis/{quote(ci_id)}")
            relations = call(
                "GET", f"/api/v2/cis/{quote(ci_id)}/relations",
                params={"direction": "both"},
            ).get("relations", [])
            impact = call("GET", f"/api/v2/impact/{quote(ci_id)}",
                          params={"depth": 3})
            return HTMLResponse(cmdb_ci_page(ci, relations, impact))
        except Exception as exc:  # noqa: BLE001
            return HTMLResponse(
                cmdb_admin_page([], {}, [], [],
                                error=f"{type(exc).__name__}: {exc}"),
                status_code=503,
            )

    @router.post("/changes")
    async def create_change(request: Request) -> RedirectResponse:
        data = await form(request)
        try:
            call("POST", "/api/v2/changes", "editor", {
                "service_ci": data.get("service_ci", ""),
                "title": data.get("title", ""),
                "risk": data.get("risk", "medium"),
            })
            return flash("变更单已创建")
        except Exception as exc:  # noqa: BLE001
            return flash(error=f"{type(exc).__name__}: {exc}")

    @router.post("/changes/{change_id}/{action}")
    async def transition(change_id: str, action: str) -> RedirectResponse:
        role = "approver" if action in ("approve", "reject") else "editor"
        try:
            if action in ("approve", "reject"):
                call("POST", f"/api/v2/changes/{quote(change_id)}/approve", role, {
                    "decision": "approve" if action == "approve" else "reject",
                    "comment": "admin-ui",
                })
            elif action == "submit":
                call("POST", f"/api/v2/changes/{quote(change_id)}/submit", role)
            elif action == "execute":
                call("POST", f"/api/v2/changes/{quote(change_id)}/execute", role,
                     {"success": True})
            elif action == "rollback":
                call("POST", f"/api/v2/changes/{quote(change_id)}/rollback", role)
            else:
                raise RuntimeError(f"unknown action {action}")
            return flash(f"{action} 成功")
        except Exception as exc:  # noqa: BLE001
            return flash(error=f"{type(exc).__name__}: {exc}")

    return router
