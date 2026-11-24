"""CMDB v2 API: CI graph, impact traversal, change approval, audit.

Auth is a small role-token model (viewer < editor < approver), enough for an
internal control-plane service and independent from the MCP v1 compatibility
facade. Tokens are never logged; the actor header is separate so the audit log
records who acted, not which secret was used.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .graph import GraphError, GraphStore

_ROLE_LEVEL = {"viewer": 1, "editor": 2, "approver": 3, "platform_admin": 4}


class CIIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    ci_type: Literal["service", "host", "middleware", "database", "storage",
                     "external"] = "service"
    owner: str = ""
    tier: str = "P2"
    env: str = "prod"
    status: str = "active"
    attributes: dict[str, Any] = Field(default_factory=dict)


class RelationIn(BaseModel):
    relation_type: Literal["depends_on", "routes_to", "calls", "stores_in",
                           "runs_on", "realized_by"] = "depends_on"
    criticality: Literal["low", "medium", "high", "critical"] = "medium"
    source: str = "manual"
    attributes: dict[str, Any] = Field(default_factory=dict)


class ChangeIn(BaseModel):
    service_ci: str
    title: str = Field(min_length=1, max_length=255)
    description: str = ""
    risk: Literal["low", "medium", "high", "critical"] = "medium"
    planned_at: str = ""
    rollback_of: str = ""


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"] = "approve"
    comment: str = ""


class ExecuteIn(BaseModel):
    success: bool = True


def build_router(graph: GraphStore, tokens: dict[str, str],
                 tenant: str = "team-core",
                 platform_token: str = "") -> APIRouter:
    router = APIRouter(prefix="/api/v2")

    def role_for(token: str) -> str | None:
        if not token:
            return None
        if platform_token and token == platform_token:
            return "platform_admin"
        for role, expected in tokens.items():
            if expected and token == expected:
                return role
        return None

    def require(min_role: str):
        def dependency(request: Request,
                       x_cmdb_token: str = Header(default="")) -> str:
            role = role_for(x_cmdb_token)
            if role is None or _ROLE_LEVEL.get(role, 0) < _ROLE_LEVEL[min_role]:
                raise HTTPException(
                    401, "missing or insufficient X-CMDB-Token"
                )
            request.state.cmdb_tenant = "" if role == "platform_admin" else tenant
            request.state.cmdb_role = role
            return role
        return dependency

    def tenant_of(request: Request) -> str:
        return getattr(request.state, "cmdb_tenant", tenant)

    def actor(request: Request, role: str) -> str:
        return (request.headers.get("X-CMDB-Actor") or f"api-{role}")[:128]

    def fail(exc: GraphError) -> None:
        raise HTTPException(400, str(exc))

    @router.get("/cis")
    def list_cis(request: Request, ci_type: str = "", env: str = "",
                 _role: str = Depends(require("viewer"))) -> dict:
        return {"cis": graph.list_cis(
            ci_type or None, env or None, tenant_id=tenant_of(request)
        )}

    @router.get("/cis/{ci_id}")
    def get_ci(ci_id: str, request: Request,
               _role: str = Depends(require("viewer"))) -> dict:
        ci = graph.get_ci(ci_id, tenant_of(request))
        if not ci:
            raise HTTPException(404, f"CI {ci_id} not found")
        return ci

    @router.put("/cis/{ci_id}")
    def upsert_ci(ci_id: str, body: CIIn, request: Request,
                  role: str = Depends(require("editor"))) -> dict:
        try:
            return graph.upsert_ci(
                ci_id, actor=actor(request, role), role=role,
                tenant_id=tenant_of(request), **body.model_dump(),
            )
        except GraphError as exc:
            fail(exc)

    @router.get("/cis/{ci_id}/relations")
    def relations(ci_id: str, request: Request, direction: str = "both",
                  _role: str = Depends(require("viewer"))) -> dict:
        tenant_id = tenant_of(request)
        if not graph.get_ci(ci_id, tenant_id):
            raise HTTPException(404, f"CI {ci_id} not found")
        return {"relations": graph.relations(ci_id, direction, tenant_id)}

    @router.put("/cis/{ci_id}/relations/{to_ci}")
    def upsert_relation(ci_id: str, to_ci: str, body: RelationIn,
                        request: Request,
                        role: str = Depends(require("editor"))) -> dict:
        try:
            return graph.upsert_relation(
                ci_id, to_ci, actor=actor(request, role), role=role,
                tenant_id=tenant_of(request), **body.model_dump(),
            )
        except GraphError as exc:
            fail(exc)

    @router.get("/impact/{ci_id}")
    def impact(ci_id: str, request: Request, direction: str = "downstream",
               depth: int = Query(default=3, ge=1, le=8),
               _role: str = Depends(require("viewer"))) -> dict:
        try:
            return graph.impact(
                ci_id, direction=direction, depth=depth,
                tenant_id=tenant_of(request),
            )
        except GraphError as exc:
            raise HTTPException(404, str(exc))

    @router.get("/changes")
    def list_changes(request: Request, service: str = "", status: str = "",
                     limit: int = Query(default=50, ge=1, le=200),
                     _role: str = Depends(require("viewer"))) -> dict:
        return {"changes": graph.list_changes(
            service or None, status or None, limit, tenant_id=tenant_of(request)
        )}

    @router.get("/changes/{change_id}")
    def get_change(change_id: str, request: Request,
                   _role: str = Depends(require("viewer"))) -> dict:
        change = graph.get_change(change_id, tenant_of(request))
        if not change:
            raise HTTPException(404, f"change {change_id} not found")
        return change

    @router.post("/changes", status_code=201)
    def create_change(body: ChangeIn, request: Request,
                      role: str = Depends(require("editor"))) -> dict:
        try:
            return graph.create_change(
                actor=actor(request, role), role=role,
                tenant_id=tenant_of(request), **body.model_dump()
            )
        except GraphError as exc:
            fail(exc)

    @router.post("/changes/{change_id}/submit")
    def submit_change(change_id: str, request: Request,
                      role: str = Depends(require("editor"))) -> dict:
        try:
            return graph.submit_change(
                change_id, actor(request, role), role, tenant_of(request)
            )
        except GraphError as exc:
            fail(exc)

    @router.post("/changes/{change_id}/approve")
    def approve_change(change_id: str, body: DecisionIn, request: Request,
                       role: str = Depends(require("approver"))) -> dict:
        try:
            return graph.approve_change(
                change_id, actor(request, role), body.decision, body.comment,
                role, tenant_of(request),
            )
        except GraphError as exc:
            fail(exc)

    @router.post("/changes/{change_id}/execute")
    def execute_change(change_id: str, body: ExecuteIn, request: Request,
                       role: str = Depends(require("editor"))) -> dict:
        try:
            return graph.execute_change(
                change_id, actor(request, role), body.success, role,
                tenant_of(request),
            )
        except GraphError as exc:
            fail(exc)

    @router.post("/changes/{change_id}/rollback")
    def rollback_change(change_id: str, request: Request,
                        role: str = Depends(require("editor"))) -> dict:
        try:
            return graph.rollback_change(
                change_id, actor(request, role), role, tenant_of(request)
            )
        except GraphError as exc:
            fail(exc)

    @router.get("/quality")
    def quality(request: Request,
                _role: str = Depends(require("viewer"))) -> dict:
        return graph.quality_report(tenant_of(request))

    @router.get("/audit")
    def audit(limit: int = Query(default=100, ge=1, le=500),
              _role: str = Depends(require("approver"))) -> dict:
        return {"audit": graph.audit_log(limit)}

    return router
