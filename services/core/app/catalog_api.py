"""HTTP surface for the service catalog.

Two audiences, one source of truth:

* ``/admin/*`` — server-rendered pages an SRE uses to register services and
  bind datasources. No JS bundle, no build step, matching the rest of the app.
* ``/api/v1/catalog/*`` — JSON for automation and tests.

Both call :class:`CatalogStore`, where every invariant is enforced, so a form
post and a programmatic PUT cannot diverge.

Forms are parsed manually from the raw body. That avoids a hard dependency on
``python-multipart`` for what is only ``application/x-www-form-urlencoded``,
which matters on the 4 GiB production box where every extra wheel is a risk.
"""
from __future__ import annotations

import base64
import binascii
import secrets
from typing import Any, Literal
from urllib.parse import parse_qs, quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import render
from .catalog import KINDS, MODES, CatalogError, CatalogStore
from .config import Settings
from .tools import SSRFBlocked, assert_url_allowed


class ServiceIn(BaseModel):
    display_name: str = Field(default="", max_length=255)
    owner: str = Field(default="", max_length=128)
    tier: Literal["P0", "P1", "P2", "P3"] = "P2"
    description: str = Field(default="", max_length=1024)
    enabled: bool = True


class BindingIn(BaseModel):
    datasource_name: str = Field(min_length=1, max_length=128)
    mode: Literal["real", "mock"] = "real"
    selector: str = Field(default="", max_length=1000)
    metric: str = Field(default="", max_length=128)
    secondary_metric: str = Field(default="", max_length=128)
    metric_type: Literal["gauge", "counter"] = "gauge"
    secondary_metric_type: Literal["gauge", "counter"] = "gauge"
    extra_metrics: list[dict[str, str]] = Field(default_factory=list)
    impact_policy: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class DatasourceIn(BaseModel):
    kind: str = Field(min_length=1, max_length=32)
    base_url: str = Field(min_length=1, max_length=500)
    mock: bool = False
    description: str = Field(default="", max_length=512)
    enabled: bool = True


def _form_flag(form: dict[str, str]) -> bool:
    return form.get("enabled", "") in ("1", "on", "true", "yes")


async def _form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8", errors="replace")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items() if values}


def _probe(datasource: dict[str, Any], settings: Settings) -> tuple[bool, str]:
    """Read-only connectivity check for one datasource."""
    url = str(datasource.get("base_url") or "").rstrip("/")
    kind = str(datasource.get("kind") or "")
    mock = bool(datasource.get("mock"))
    try:
        assert_url_allowed(url, settings.tool_url_allowlist)
    except SSRFBlocked as exc:
        return False, f"URL 被白名单拒绝: {exc}"
    try:
        with httpx.Client(timeout=6.0) as client:
            if mock:
                resp = client.get(f"{url}/healthz")
                detail = f"Mock 上游 /healthz HTTP {resp.status_code}"
                return resp.status_code < 400, detail
            if kind == "prometheus":
                resp = client.get(f"{url}/api/v1/query", params={"query": "up"})
                if resp.status_code >= 400:
                    return False, f"/api/v1/query HTTP {resp.status_code}"
                payload = resp.json()
                series = (payload.get("data") or {}).get("result") or []
                return True, f"Prometheus 可查，up 序列 {len(series)} 条"
            if kind == "loki":
                resp = client.get(f"{url}/ready")
                return resp.status_code < 400, f"Loki /ready HTTP {resp.status_code}"
            resp = client.get(f"{url}/healthz")
            return resp.status_code < 400, f"/healthz HTTP {resp.status_code}"
    except Exception as exc:  # noqa: BLE001 - report, don't raise
        return False, f"{type(exc).__name__}: {exc}"


def build_router(catalog: CatalogStore, settings: Settings) -> APIRouter:
    router = APIRouter()

    def require_admin(request: Request) -> None:
        """Optional HTTP Basic auth. Empty password = explicitly disabled."""
        password = settings.admin_password
        if not password:
            return
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("basic "):
            raise HTTPException(
                status_code=401,
                detail="admin auth required",
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

    # Router-level guard: the JSON API can mutate the catalog too, so it must
    # be protected exactly like the HTML pages. (Found during production
    # verification: the read endpoint was accidentally open.)
    router.dependencies.append(Depends(require_admin))

    def admin_page() -> list[Any]:
        return [Depends(require_admin)]

    def fail_redirect(path: str, exc: Exception) -> RedirectResponse:
        message = str(exc)[:300]
        return RedirectResponse(url=f"{path}?error={quote(message)}", status_code=303)

    # ------------------------------------------------------------------ HTML
    @router.get("/admin/catalog", response_class=HTMLResponse, dependencies=admin_page())
    def admin_catalog(saved: str = "", error: str = "") -> str:
        message = "已保存" if saved else ""
        return render.catalog_list(
            catalog.list_services(), catalog.stats(), message=message, error=error
        )

    @router.post("/admin/catalog", dependencies=admin_page())
    async def admin_create_service(request: Request) -> RedirectResponse:
        form = await _form(request)
        name = (form.get("name") or "").strip()
        try:
            catalog.upsert_service(
                name,
                display_name=form.get("display_name", ""),
                owner=form.get("owner", ""),
                tier=form.get("tier", "P2"),
                description=form.get("description", ""),
            )
        except CatalogError as exc:
            return fail_redirect("/admin/catalog", exc)
        return RedirectResponse(url=f"/admin/catalog/{quote(name)}?saved=1", status_code=303)

    @router.get("/admin/catalog/{name}", response_class=HTMLResponse,
                dependencies=admin_page())
    def admin_catalog_detail(name: str, saved: str = "", error: str = "") -> str:
        service = catalog.get_service(name)
        if service is None:
            raise HTTPException(404, f"服务 {name} 不存在")
        return render.catalog_detail(
            service, catalog.list_datasources(),
            message="已保存" if saved else "", error=error,
        )

    @router.post("/admin/catalog/{name}", dependencies=admin_page())
    async def admin_update_service(name: str, request: Request) -> RedirectResponse:
        form = await _form(request)
        path = f"/admin/catalog/{quote(name)}"
        try:
            catalog.upsert_service(
                name,
                display_name=form.get("display_name", ""),
                owner=form.get("owner", ""),
                tier=form.get("tier", "P2"),
                description=form.get("description", ""),
                enabled=_form_flag(form),
            )
        except CatalogError as exc:
            return fail_redirect(path, exc)
        return RedirectResponse(url=f"{path}?saved=1", status_code=303)

    @router.post("/admin/catalog/{name}/delete", dependencies=admin_page())
    async def admin_delete_service(name: str) -> RedirectResponse:
        catalog.delete_service(name)
        return RedirectResponse(url="/admin/catalog?saved=1", status_code=303)

    @router.post("/admin/catalog/{name}/bindings", dependencies=admin_page())
    async def admin_upsert_binding(name: str, request: Request) -> RedirectResponse:
        form = await _form(request)
        path = f"/admin/catalog/{quote(name)}"
        try:
            catalog.upsert_binding(
                name,
                form.get("kind", ""),
                datasource_name=form.get("datasource_name", ""),
                mode=form.get("mode", "real"),
                selector=form.get("selector", ""),
                metric=form.get("metric", ""),
                secondary_metric=form.get("secondary_metric", ""),
                metric_type=form.get("metric_type", "gauge"),
                secondary_metric_type=form.get("secondary_metric_type", "gauge"),
                extra_metrics=form.get("extra_metrics", ""),
                impact_policy=form.get("impact_policy", ""),
            )
        except CatalogError as exc:
            return fail_redirect(path, exc)
        return RedirectResponse(url=f"{path}?saved=1", status_code=303)

    @router.post("/admin/catalog/{name}/bindings/{kind}/delete", dependencies=admin_page())
    async def admin_delete_binding(name: str, kind: str) -> RedirectResponse:
        catalog.delete_binding(name, kind)
        return RedirectResponse(url=f"/admin/catalog/{quote(name)}?saved=1", status_code=303)

    @router.get("/admin/datasources", response_class=HTMLResponse,
                dependencies=admin_page())
    def admin_datasources(saved: str = "", error: str = "") -> str:
        return render.datasource_list(
            catalog.list_datasources(), message="已保存" if saved else "", error=error
        )

    @router.post("/admin/datasources", dependencies=admin_page())
    async def admin_create_datasource(request: Request) -> RedirectResponse:
        form = await _form(request)
        try:
            catalog.upsert_datasource(
                form.get("name", ""),
                kind=form.get("kind", ""),
                base_url=form.get("base_url", ""),
                mock=form.get("mock", "") in ("1", "on", "true"),
                description=form.get("description", ""),
                enabled=_form_flag(form),
            )
        except CatalogError as exc:
            return fail_redirect("/admin/datasources", exc)
        return RedirectResponse(url="/admin/datasources?saved=1", status_code=303)

    @router.post("/admin/datasources/{name}/delete", dependencies=admin_page())
    async def admin_delete_datasource(name: str) -> RedirectResponse:
        try:
            catalog.delete_datasource(name)
        except CatalogError as exc:
            return fail_redirect("/admin/datasources", exc)
        return RedirectResponse(url="/admin/datasources?saved=1", status_code=303)

    @router.get("/admin/datasources/{name}/test", response_class=HTMLResponse,
                dependencies=admin_page())
    def admin_test_datasource(name: str) -> str:
        datasource = catalog.get_datasource(name)
        if datasource is None:
            raise HTTPException(404, f"数据源 {name} 不存在")
        ok, detail = _probe(datasource, settings)
        return render.datasource_list(
            catalog.list_datasources(),
            test={"name": name, "ok": ok, "detail": detail},
        )

    # -------------------------------------------------------------------- API
    @router.get("/api/v1/catalog/services")
    def api_services() -> dict:
        return {"services": catalog.list_services(), "stats": catalog.stats()}

    @router.get("/api/v1/catalog/services/{name}")
    def api_service(name: str) -> dict:
        service = catalog.get_service(name)
        if service is None:
            raise HTTPException(404, f"服务 {name} 不存在")
        return service

    @router.put("/api/v1/catalog/services/{name}")
    def api_upsert_service(name: str, body: ServiceIn) -> dict:
        try:
            return catalog.upsert_service(name, actor="api", **body.model_dump())
        except CatalogError as exc:
            raise HTTPException(400, str(exc))

    @router.delete("/api/v1/catalog/services/{name}")
    def api_delete_service(name: str) -> dict:
        catalog.delete_service(name)
        return {"ok": True, "name": name}

    @router.put("/api/v1/catalog/services/{name}/bindings/{kind}")
    def api_upsert_binding(name: str, kind: str, body: BindingIn) -> dict:
        if kind not in KINDS:
            raise HTTPException(400, f"kind 必须是 {KINDS}")
        try:
            return catalog.upsert_binding(name, kind, actor="api", **body.model_dump())
        except CatalogError as exc:
            raise HTTPException(400, str(exc))

    @router.delete("/api/v1/catalog/services/{name}/bindings/{kind}")
    def api_delete_binding(name: str, kind: str) -> dict:
        catalog.delete_binding(name, kind)
        return {"ok": True}

    @router.get("/api/v1/catalog/datasources")
    def api_datasources() -> dict:
        return {"datasources": catalog.list_datasources()}

    @router.put("/api/v1/catalog/datasources/{name}")
    def api_upsert_datasource(name: str, body: DatasourceIn) -> dict:
        try:
            return catalog.upsert_datasource(name, actor="api", **body.model_dump())
        except CatalogError as exc:
            raise HTTPException(400, str(exc))

    @router.delete("/api/v1/catalog/datasources/{name}")
    def api_delete_datasource(name: str) -> dict:
        try:
            catalog.delete_datasource(name)
        except CatalogError as exc:
            raise HTTPException(409, str(exc))
        return {"ok": True}

    @router.post("/api/v1/catalog/datasources/{name}/test")
    def api_test_datasource(name: str) -> dict:
        datasource = catalog.get_datasource(name)
        if datasource is None:
            raise HTTPException(404, f"数据源 {name} 不存在")
        ok, detail = _probe(datasource, settings)
        return {"name": name, "ok": ok, "detail": detail}

    @router.get("/api/v1/catalog/resolve")
    def api_resolve(service: str, kind: str) -> dict:
        if kind not in KINDS:
            raise HTTPException(400, f"kind 必须是 {KINDS}")
        binding = catalog.resolve(service, kind)
        if binding is None:
            raise HTTPException(404, f"{service}/{kind} 没有启用的数据源绑定")
        return binding

    return router
