"""HTTP layer: thin adapters over TaskService.

Deliberately thin — all invariants live in the domain service so they hold no
matter which transport calls in (REST today, gRPC/MQ consumer later).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import incidents, memory, render
from .archive import ArchiveStore, OssArchiver
from .catalog import CatalogStore
from .catalog_api import build_router
from .config import load_settings, oss_config
from .db import from_settings as db_from_settings
from .docx_export import markdown_to_docx
from .lease import LeaseManager, StaleLease
from .llm import ModelClient
from .orchestrator import LoopDetected, Orchestrator
from .reaper import LeaseReaper
from .report_store import ReportStore
from .saga import SagaRegistry
from .step_store import StepOutputStore
from .service import (
    IllegalTransition,
    NeedsHumanDecision,
    StepStatus,
    TaskService,
    TaskStatus,
)
from .store import EventStore
from .trace import build_trace, export_otlp
from .security import SecurityStore
from .auth_api import build_router as build_auth_router, resolve_principal

try:  # metrics are optional so the domain layer can run dependency-free
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

    TASKS_CREATED = Counter("copilot_tasks_created_total", "Tasks created")
    STEPS_LEASED = Counter("copilot_steps_leased_total", "Steps leased")
    HTTP_REQUESTS = Counter(
        "copilot_http_requests_total", "HTTP requests", ["method", "path", "status"]
    )
    HTTP_LATENCY = Histogram(
        "copilot_http_request_duration_seconds", "HTTP request latency",
        ["method", "path"],
    )
    ANALYSIS_TOTAL = Counter(
        "copilot_analysis_total", "Analysis runs", ["service", "status"]
    )
    ANALYSIS_DURATION = Histogram(
        "copilot_analysis_duration_seconds", "End-to-end analysis latency", ["service"]
    )
    HAS_METRICS = True
except ImportError:  # pragma: no cover - container installs the dependency
    HAS_METRICS = False

DB_PATH = os.getenv("CORE_DB_PATH", "core.db")

# The backend is chosen by DB_BACKEND (sqlite | mysql). Every auxiliary store
# shares this one handle, so its dialect is translated exactly once.
log = logging.getLogger(__name__)

_settings = load_settings()
_database = db_from_settings(_settings)
_deduper = incidents.Deduper(_settings.alert_dedup_window_s)
store = EventStore(_settings.db_path or DB_PATH, db=_database)
leases = LeaseManager(store._conn, store._lock)
saga = SagaRegistry(store._conn, store._lock)
reports = ReportStore(store._conn, store._lock)
archives = ArchiveStore(store._conn, store._lock)
step_outputs = StepOutputStore(store._conn, store._lock)
service = TaskService(store, leases, saga)
reaper = LeaseReaper(service, leases)
# The catalog seeds two services on an empty DB; on a populated one it never
# overwrites operator edits.
catalog = CatalogStore(_database, _settings, seed=_settings.catalog_seed)
security = SecurityStore(_database)
security.bootstrap(_settings.bootstrap_admin_password or _settings.admin_password)

app = FastAPI(title="Postmortem Copilot Core", version="0.2.0")
app.include_router(build_router(catalog, _settings))
from .cmdb_admin import build_router as build_cmdb_admin_router  # noqa: E402

app.include_router(build_cmdb_admin_router(_settings))
app.include_router(build_auth_router(security, _settings))
app.state.catalog = catalog
app.state.security = security


@app.middleware("http")
async def _http_metrics(request: Request, call_next):  # noqa: ANN001, ANN201
    """Request count/latency with the *route template* as the path label.

    Using the raw path would create one time series per task UUID and blow up
    Prometheus cardinality; the route template (`/tasks/{task_id}`) is bounded.
    """
    started = time.perf_counter()
    status = "500"
    try:
        response = await call_next(request)
        status = str(response.status_code)
        return response
    except Exception:  # noqa: BLE001 - record then let FastAPI handle it
        raise
    finally:
        if HAS_METRICS:
            route = request.scope.get("route")
            path = getattr(route, "path", None) or "unmatched"
            if path != "/metrics":
                HTTP_REQUESTS.labels(request.method, path, status).inc()
                HTTP_LATENCY.labels(request.method, path).observe(
                    max(0.0, time.perf_counter() - started)
                )


_TASK_PATH_RE = re.compile(r"/(?:api/v1/)?tasks/([0-9a-fA-F-]{36})")
_PUBLIC_PATHS = {
    "/healthz", "/metrics", "/login", "/api/v1/auth/login", "/favicon.ico",
    "/api/v1/auth/oidc/login", "/api/v1/auth/oidc/callback",
}


@app.middleware("http")
async def _auth_and_tenant(request: Request, call_next):  # noqa: ANN001, ANN201
    if not _settings.auth_enabled:
        return await call_next(request)
    path = request.url.path
    if path in _PUBLIC_PATHS:
        return await call_next(request)
    principal = resolve_principal(request, security, _settings)
    if principal is None:
        if path.startswith("/api/"):
            return UTF8JSONResponse(
                {"detail": "authentication required"}, status_code=401
            )
        return RedirectResponse(f"/login?next={path}", status_code=303)

    identity = f"{principal.kind}:{principal.user_id}"
    if not security.check_rate(identity, _settings.rate_limit_per_min):
        return UTF8JSONResponse(
            {"detail": "rate limit exceeded"}, status_code=429
        )

    # Coarse role policy; endpoint-level policies can tighten further. Viewers
    # are read-only; tenant admins are required for the control-plane admin UI.
    if principal.role == "viewer" and request.method not in ("GET", "HEAD", "OPTIONS"):
        return UTF8JSONResponse({"detail": "viewer is read-only"}, status_code=403)
    if path.startswith("/admin") and principal.role not in (
        "tenant_admin", "platform_admin"
    ):
        return UTF8JSONResponse({"detail": "admin role required"}, status_code=403)
    if path.endswith("/approval") and principal.role not in (
        "tenant_admin", "platform_admin"
    ):
        return UTF8JSONResponse({"detail": "approver role required"}, status_code=403)

    # A platform admin may choose the tenant when creating; reads stay global.
    override = request.headers.get("x-tenant-id", "").strip()
    if override and principal.role == "platform_admin":
        principal.tenant_id = override

    # Session-cookie form posts need same-origin checks. Basic auth is a
    # separate bootstrap channel and is not a browser session.
    if (
        request.method == "POST"
        and path.startswith("/admin")
        and principal.kind == "session"
    ):
        origin = request.headers.get("origin", "")
        if origin:
            host = request.headers.get("host", "")
            if host and host not in origin:
                return JSONResponse(
                    {"detail": "cross-origin admin POST rejected"}, status_code=403
                )

    request.state.principal = principal

    match = _TASK_PATH_RE.search(path)
    if match:
        try:
            view = service.view(match.group(1))
        except Exception:  # noqa: BLE001 - route will return 404
            view = None
        if view is not None and principal.role != "platform_admin":
            if view.tenant_id != principal.tenant_id:
                if path.startswith("/api/"):
                    return UTF8JSONResponse(
                        {"detail": "task not found in this tenant"}, status_code=404
                    )
                return JSONResponse({"detail": "forbidden"}, status_code=404)

    return await call_next(request)


class UTF8JSONResponse(JSONResponse):
    """Declare charset explicitly: PowerShell 5.1 otherwise decodes UTF-8 JSON
    as Latin-1 and every Chinese string turns into mojibake."""

    media_type = "application/json; charset=utf-8"


app.router.default_response_class = UTF8JSONResponse


# ---------------------------------------------------------------- schemas
class CreateTaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    actor: str = "api"


class StepIn(BaseModel):
    step_id: str
    role: str
    capability_tag: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    side_effect: bool = False
    max_attempts: int = 1


class PlanIn(BaseModel):
    steps: list[StepIn]
    actor: str = "orchestrator"


class LeaseIn(BaseModel):
    owner: str
    ttl_ms: int = 30_000


class ApprovalIn(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    comment: str | None = None
    approver: str


class BeginIn(BaseModel):
    owner: str
    fence: int
    attempt: int = 1


class RunIn(BaseModel):
    service_name: str = Field(min_length=1, max_length=128)
    window: str = "30m"


class AdjudicateIn(BaseModel):
    decision: str = Field(pattern="^(confirm_success|confirm_failure)$")
    actor: str = Field(min_length=1, max_length=64)
    note: str = ""


# ---------------------------------------------------------------- endpoints
@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/metrics")
def metrics() -> Response:
    if not HAS_METRICS:
        raise HTTPException(501, "prometheus_client not installed")
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ------------------------------------------------------------------ web pages
def _task_summaries(limit: int = 20, tenant_id: str | None = None,
                    platform: bool = False) -> list[dict]:
    out: list[dict] = []
    for task_id in store.list_tasks_recent(limit=max(limit * 4, limit)):
        try:
            view = service.view(task_id)
        except Exception:  # noqa: BLE001 - a broken journal must not break the index
            continue
        if not platform and tenant_id and view.tenant_id != tenant_id:
            continue
        total = len(view.steps)
        done = sum(1 for s in view.steps.values() if s["status"] == "SUCCEEDED")
        out.append(
            {
                "task_id": task_id,
                "title": view.title,
                "status": view.status.value,
                "step_summary": f"{done}/{total} 成功" if total else "未规划",
            }
        )
    return out[:limit]


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> str:
    mode = "mock（未配置真实模型）" if ModelClient().is_mock else "real"
    principal = getattr(request.state, "principal", None)
    if principal is None:
        tasks = _task_summaries()
    else:
        tasks = _task_summaries(
            tenant_id=principal.tenant_id,
            platform=principal.role == "platform_admin",
        )
    return render.landing(tasks, mode)


def _archiver() -> OssArchiver:
    return OssArchiver(oss_config(load_settings()), archives)


def _memory():
    """Long-term memory client (null object when ContextDB OAuth is unset)."""
    s = load_settings()
    return memory.build(
        s.contextdb_oauth_base,
        s.contextdb_oauth_access_token,
        s.contextdb_oauth_refresh_token,
        s.contextdb_user_id,
        s.contextdb_agent_id,
    )


def _recall_history(service_name: str) -> list:
    """Best-effort semantic recall of similar past incidents."""
    try:
        return _memory().recall(f"{service_name} 故障根因与处置", top_k=3)
    except Exception:  # noqa: BLE001 - memory must never break an analysis
        return []


def _remember_incident(task_id: str, service_name: str, report_md: str,
                       evidence: dict | None = None) -> None:
    """Persist a report to long-term memory, but never persist demo data.

    Long-term memory is the highest-leverage place to contaminate: one mock
    conclusion stored today is retrieved as "similar history" for every future
    real incident. Evidence carries its own trust label, so the gate is exact
    rather than a keyword scan over the rendered Markdown.
    """
    if evidence and any(e.data_mode != "real" for e in evidence.values()):
        log.info("skip memory write for %s: evidence contains mock/fixture", task_id)
        return
    try:
        _memory().remember(task_id=task_id, service=service_name, report_md=report_md,
                           extra={"来源": "postmortem-copilot 自动归档"})
    except Exception:  # noqa: BLE001
        pass


def _task_tenant(task_id: str) -> str:
    try:
        return service.view(task_id).tenant_id
    except Exception:  # noqa: BLE001 - legacy tasks may not project a tenant
        return ""


def _archive_task(task_id: str, markdown_text: str, evidence: dict,
                  base_url: str, tenant_id: str = "") -> dict:
    """Best-effort archival; never let storage failure break the request."""
    archiver = _archiver()
    if not archiver.enabled:
        return {}
    base = (base_url or "").rstrip("/")
    try:
        docx_bytes = markdown_to_docx(
            markdown_text, task_id=task_id, base_url=f"{base}/tasks/{task_id}/evidence"
        )
    except Exception:  # noqa: BLE001 - docx is optional for archival
        docx_bytes = b""
    evidence_json = {
        k: v.model_dump(mode="json") for k, v in evidence.items()
    } if evidence else {}
    return archiver.archive_task(
        task_id, markdown_text, docx_bytes, evidence_json, tenant_id=tenant_id
    )


def _notifier() -> incidents.Notifier:
    s = load_settings()
    urls = [u.strip() for u in (s.notify_webhook_urls or "").split(",") if u.strip()]
    return incidents.Notifier(urls)


def _public_base(request_base: str = "") -> str:
    s = load_settings()
    return (s.public_base_url or request_base or "http://127.0.0.1:8000").rstrip("/")


def _record_analysis(service_name: str, status: str, started: float) -> None:
    if not HAS_METRICS:
        return
    try:
        ANALYSIS_TOTAL.labels(service=service_name, status=status).inc()
        ANALYSIS_DURATION.labels(service=service_name).observe(
            max(0.0, time.perf_counter() - started)
        )
    except Exception:  # noqa: BLE001 - metrics must never break a request
        pass


def _run_incident(task_id: str, alert) -> None:  # noqa: ANN001
    """Background analysis for one ingested alert. Never raises into the server."""
    base = _public_base()
    service_name = alert.service
    started = time.perf_counter()
    analysis_status = "failed"
    try:
        history = _recall_history(service_name)
        report = Orchestrator(service, catalog=catalog).run(task_id, service_name, "30m")
        report_md = report.report_md + memory.history_section(history)
        reports.save(task_id, report_md)
        step_outputs.save_evidence(task_id, report.evidence)
        _archive_task(task_id, report_md, report.evidence, base,
                      tenant_id=_task_tenant(task_id))
        _remember_incident(task_id, service_name, report.report_md, report.evidence)
        result = incidents.IncidentResult(
            task_id=task_id, service=service_name, status="ok",
            report_url=f"{base}/tasks/{task_id}/report",
            claims=len(report.claims),
        )
        analysis_status = "ok"
    except Exception as exc:  # noqa: BLE001
        log.exception("自动复盘失败 task=%s: %s", task_id, exc)
        result = incidents.IncidentResult(task_id=task_id, service=service_name,
                                          status="failed")
    finally:
        _record_analysis(service_name, analysis_status, started)
    title, body = incidents.report_message(result)
    _notifier().send(title, body)


@app.post("/api/v1/incidents/webhook", status_code=202)
def alert_webhook(payload: dict, background: BackgroundTasks,
                  x_alert_token: str = Header(default="")) -> dict:
    """Ingest an alert and start the analysis asynchronously.

    Returns 202 immediately: a 16-second analysis inside the webhook call would
    make Alertmanager (or any caller) time out and retry, creating duplicates.
    """
    s = load_settings()
    if s.alert_token and x_alert_token != s.alert_token:
        raise HTTPException(401, "invalid alert token")

    alerts = incidents.parse(payload)
    if not alerts:
        raise HTTPException(422, "no service label found in alerts")

    accepted: list[dict] = []
    deduplicated: list[str] = []
    for alert in alerts:
        if not _deduper.accept(alert.service):
            deduplicated.append(alert.service)
            continue
        task_id = service.create_task(f"[自动] {alert.service} · {alert.name}",
                                      actor=f"alert:{alert.source}")
        background.add_task(_run_incident, task_id, alert)
        accepted.append({"service": alert.service, "task_id": task_id,
                         "severity": alert.severity})
    return {"accepted": len(accepted), "deduplicated": deduplicated, "tasks": accepted}


@app.get("/api/v1/incidents/config")
def incident_config() -> dict:
    """What the alerting pipeline can rely on (useful for wiring it up)."""
    s = load_settings()
    return {
        "webhook_path": "/api/v1/incidents/webhook",
        "auth_required": bool(s.alert_token),
        "dedup_window_s": s.alert_dedup_window_s,
        "notify_channels": len([u for u in (s.notify_webhook_urls or "").split(",") if u.strip()]),
        "accepted_formats": ["alertmanager", "generic"],
        "generic_example": {"service": "order-service", "alert": "HighLatency",
                            "severity": "critical", "summary": "P99 > 阈值"},
    }


@app.get("/ui/run", response_class=HTMLResponse)
def ui_run(
    request: Request,
    service_name: str = Query(alias="service", min_length=1, max_length=128),
    window: str = "30m",
    title: str = "",
) -> RedirectResponse:
    """Browser entry point: run the analysis and land on the task page."""
    task_id = service.create_task(title or f"{service_name} 故障复盘", actor="web")
    orchestrator = Orchestrator(service, catalog=catalog)
    history = _recall_history(service_name)
    started = time.perf_counter()
    analysis_status = "failed"
    try:
        report = orchestrator.run(task_id, service_name, window)
        analysis_status = report.status
        report_md = report.report_md + memory.history_section(history)
        reports.save(task_id, report_md)
        step_outputs.save_evidence(task_id, report.evidence)
        _archive_task(task_id, report_md, report.evidence, str(request.base_url),
                      tenant_id=_task_tenant(task_id))
        # Remember the ORIGINAL report, never the one with the recall appendix:
        # storing the appendix would make every memory contain its own history
        # section, and each recall would compound the previous one.
        _remember_incident(task_id, service_name, report.report_md, report.evidence)
    except LoopDetected as exc:
        raise HTTPException(409, f"loop detected: {exc}")
    finally:
        _record_analysis(service_name, analysis_status, started)
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


@app.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_page(task_id: str) -> str:
    try:
        view = service.view(task_id)
    except Exception:  # noqa: BLE001
        raise HTTPException(404, "task not found")
    task = {
        "task_id": view.task_id,
        "title": view.title,
        "status": view.status.value,
        "plan_version": view.plan_version,
        "last_seq": view.last_seq,
        "steps": view.steps,
    }
    return render.task_detail(task, reports.exists(task_id))


@app.get("/tasks/{task_id}/report", response_class=HTMLResponse)
def report_page(task_id: str) -> str:
    markdown_text = reports.load(task_id)
    if markdown_text is None:
        raise HTTPException(404, "报告尚未生成，请先运行分析")
    view = service.view(task_id)
    return render.report_page(
        {"task_id": view.task_id, "title": view.title, "status": view.status.value},
        markdown_text,
        archives.for_task(task_id),
    )


@app.get("/tasks/{task_id}/report.md")
def report_download(task_id: str) -> Response:
    """Download the raw Markdown, for pasting into a wiki or a ticket."""
    markdown_text = reports.load(task_id)
    if markdown_text is None:
        raise HTTPException(404, "报告尚未生成")
    return Response(
        content=markdown_text,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="postmortem-{task_id[:8]}.md"'},
    )


@app.get("/tasks/{task_id}/report.docx")
def report_docx(task_id: str, request: Request) -> Response:
    """Word export: tables, headings and clickable evidence links preserved."""
    markdown_text = reports.load(task_id)
    if markdown_text is None:
        raise HTTPException(404, "报告尚未生成")
    base = str(request.base_url).rstrip("/")
    content = markdown_to_docx(
        markdown_text,
        task_id=task_id,
        base_url=f"{base}/tasks/{task_id}/evidence",
    )
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="postmortem-{task_id[:8]}.docx"'},
    )


@app.get("/tasks/{task_id}/archive/{kind}")
def archive_download(task_id: str, kind: str) -> Response:
    """Stream an archived artifact through the API.

    Not a redirect to a signed OSS URL: the bucket is only reachable over the
    internal endpoint from inside the VPC, so a signed URL would be unusable in
    a browser. Proxying keeps the bucket private and the object public-free.
    """
    archiver = _archiver()
    if not archiver.enabled:
        raise HTTPException(503, "对象存储未配置")
    entry = next((a for a in archives.for_task(task_id) if a["kind"] == kind), None)
    if entry is None:
        raise HTTPException(404, f"未找到归档文件 {kind}")

    data = archiver.read(entry["object_key"])
    if data is None:
        raise HTTPException(502, f"从对象存储读取失败: {archiver.error or '未知错误'}")

    media = {
        "report.md": "text/markdown; charset=utf-8",
        "report.docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "evidence.json": "application/json; charset=utf-8",
    }.get(kind, "application/octet-stream")
    return Response(
        content=data,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{kind}"'},
    )


@app.get("/tasks/{task_id}/evidence/{evidence_id}", response_class=HTMLResponse)
def evidence_page(task_id: str, evidence_id: str) -> str:
    view = service.view(task_id)
    evidence = step_outputs.load_evidence(task_id)
    item = evidence.get(evidence_id)
    if item is None:
        raise HTTPException(404, f"evidence {evidence_id} not found for this task")

    cited_by: list[str] = []
    markdown_text = reports.load(task_id) or ""
    for line in markdown_text.splitlines():
        if evidence_id in line and line.strip() and not line.strip().startswith("|"):
            cited_by.append(line.strip()[:200])

    return render.evidence_page(
        {"task_id": view.task_id, "title": view.title},
        item.model_dump(mode="json"),
        cited_by,
    )


@app.post("/api/v1/tasks", status_code=201)
def create_task(body: CreateTaskIn, request: Request) -> dict:
    # Idempotent create when the caller supplies a key.
    key = request.headers.get("Idempotency-Key")
    principal = getattr(request.state, "principal", None)
    tenant_id = principal.tenant_id if principal else "default"
    task_id = service.create_task_idempotent(
        key or "", body.title, body.actor, tenant_id=tenant_id
    )
    if HAS_METRICS:
        TASKS_CREATED.inc()
    return {"task_id": task_id, "idempotency_key": key}


@app.get("/api/v1/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    try:
        view = service.view(task_id)
    except Exception:
        raise HTTPException(404, f"task {task_id} not found")
    return {
        "task_id": view.task_id,
        "title": view.title,
        "status": view.status.value,
        "plan_version": view.plan_version,
        "steps": view.steps,
        "last_seq": view.last_seq,
    }


@app.get("/api/v1/tasks/{task_id}/events")
def get_events(task_id: str) -> list[dict]:
    return [
        {"seq": e.seq, "type": e.type.value, "payload": e.payload,
         "actor": e.actor, "at": e.created_at}
        for e in store.load(task_id)
    ]


@app.post("/api/v1/tasks/{task_id}/plan")
def plan_task(task_id: str, body: PlanIn) -> dict:
    try:
        view = service.plan(
            task_id, [s.model_dump(exclude_none=True) for s in body.steps], body.actor
        )
    except IllegalTransition as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:  # noqa: BLE001 - PlanError/validation -> 400
        from .dag import PlanError

        if isinstance(exc, PlanError):
            raise HTTPException(400, str(exc))
        raise
    return {"task_id": task_id, "plan_version": view.plan_version, "steps": len(view.steps)}


@app.post("/api/v1/tasks/{task_id}/start")
def start_task(task_id: str) -> dict:
    try:
        service.start(task_id)
    except IllegalTransition as exc:
        raise HTTPException(409, str(exc))
    return {"task_id": task_id, "status": service.view(task_id).status.value}


@app.post("/api/v1/tasks/{task_id}/steps/{step_id}/lease")
def lease_step(task_id: str, step_id: str, body: LeaseIn) -> dict:
    try:
        fence = service.lease_step(task_id, step_id, body.owner, body.ttl_ms)
    except IllegalTransition as exc:
        raise HTTPException(409, str(exc))
    if fence is None:
        raise HTTPException(423, "lease is held by another worker")
    return {"task_id": task_id, "step_id": step_id, "fence": fence}


@app.post("/api/v1/tasks/{task_id}/steps/{step_id}/begin")
def begin_step(task_id: str, step_id: str, body: BeginIn) -> dict:
    try:
        service.begin_step(task_id, step_id, body.owner, body.fence, body.attempt)
    except StaleLease as exc:
        raise HTTPException(409, str(exc))
    except IllegalTransition as exc:
        raise HTTPException(409, str(exc))
    return {"task_id": task_id, "step_id": step_id, "status": "RUNNING"}


@app.post("/api/v1/tasks/{task_id}/steps/{step_id}/succeed")
def succeed_step(task_id: str, step_id: str, body: BeginIn) -> dict:
    try:
        service.succeed(task_id, step_id, body.fence, {"ok": True}, body.owner)
    except (StaleLease, IllegalTransition) as exc:
        raise HTTPException(409, str(exc))
    return {"task_id": task_id, "step_id": step_id, "status": "SUCCEEDED"}


@app.post("/api/v1/tasks/{task_id}/recover")
def recover_task(task_id: str) -> dict:
    unknown = service.recover(task_id)
    if unknown:
        service.request_human(task_id, f"unknown outcome on steps {unknown}")
    return {"task_id": task_id, "unknown_steps": unknown}


@app.post("/api/v1/tasks/{task_id}/approval")
def approval(task_id: str, body: ApprovalIn) -> dict:
    try:
        if body.decision == "approve":
            service.approve_delivery(task_id, body.approver)
        else:
            service.resume(task_id, "reject", body.approver)
    except IllegalTransition as exc:
        raise HTTPException(409, str(exc))
    return {"task_id": task_id, "status": service.view(task_id).status.value}


@app.post("/api/v1/tasks/{task_id}/run")
def run_task(task_id: str, body: RunIn, request: Request) -> dict:
    """Execute the full analysis chain (M2). Uses the mock model when no
    LLM_* env vars are configured."""
    orchestrator = Orchestrator(service, catalog=catalog)
    history = _recall_history(body.service_name)
    started = time.perf_counter()
    analysis_status = "failed"
    try:
        report = orchestrator.run(task_id, body.service_name, body.window)
        analysis_status = report.status
    except LoopDetected as exc:
        raise HTTPException(409, f"loop detected: {exc}")
    except IllegalTransition as exc:
        raise HTTPException(409, str(exc))
    finally:
        _record_analysis(body.service_name, analysis_status, started)

    report_md = report.report_md + memory.history_section(history)
    reports.save(task_id, report_md)
    step_outputs.save_evidence(task_id, report.evidence)
    _archive_task(task_id, report_md, report.evidence, str(request.base_url),
                  tenant_id=_task_tenant(task_id))
    # Store the original report only — see the note in ui_run().
    _remember_incident(task_id, body.service_name, report.report_md, report.evidence)
    return {
        "task_id": report.task_id,
        "status": report.status,
        "needs_human": report.needs_human,
        "claims": [c.model_dump(mode="json") for c in report.claims],
        "claim_scores": report.claim_scores,
        "impact": report.impact,
        "usage": report.usage,
        "step_usage": report.step_usage,
        "cost": report.cost,
        "failed_steps": report.failed_steps,
        "skipped_steps": report.skipped_steps,
        "timeline": [e.model_dump(mode="json") for e in report.timeline],
        "evidence": {k: v.model_dump(mode="json") for k, v in report.evidence.items()},
        "rejections": report.rejections,
        "tool_calls": report.tool_calls,
        "report_md": report.report_md,
        "model_mode": "mock" if orchestrator.llm.is_mock else "real",
    }


@app.post("/api/v1/tasks/{task_id}/steps/{step_id}/adjudicate")
def adjudicate(task_id: str, step_id: str, body: AdjudicateIn) -> dict:
    """Human decision for a step stuck in UNKNOWN (side effect indeterminate)."""
    try:
        service.adjudicate(task_id, step_id, body.decision, body.actor, body.note)
    except IllegalTransition as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "task_id": task_id,
        "step_id": step_id,
        "status": service.view(task_id).step_status(step_id).value,
    }


@app.post("/admin/reaper/run")
def run_reaper() -> dict:
    """Sweep expired leases: requeue what is safe, escalate what is not."""
    results = reaper.reap()
    return {
        "reaped": len(results),
        "details": [
            {"task_id": r.task_id, "step_id": r.step_id, "action": r.action, "detail": r.detail}
            for r in results
        ],
    }


@app.get("/ui/approve")
def ui_approve(task_id: str, approver: str = "operator") -> RedirectResponse:
    """One-click human approval from the browser.

    The API-only approval path made the demo awkward: every walkthrough ended at
    「REVIEWING」 with no way to finish without curl. This closes the loop.
    """
    try:
        service.approve_delivery(task_id, approver=approver or "operator")
    except IllegalTransition as exc:
        raise HTTPException(409, str(exc))
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


@app.get("/api/v1/tasks/{task_id}/trace")
def task_trace(task_id: str) -> dict:
    """OTel-shaped spans projected from the event journal (no side store)."""
    try:
        events = store.load(task_id)
    except Exception:  # noqa: BLE001
        raise HTTPException(404, "task not found")
    if not events:
        raise HTTPException(404, "task not found")
    return build_trace(events, task_id)


@app.post("/api/v1/tasks/{task_id}/trace/export")
def export_task_trace(task_id: str) -> dict:
    """Best-effort OTLP/HTTP export of the projected trace."""
    try:
        events = store.load(task_id)
    except Exception:  # noqa: BLE001
        raise HTTPException(404, "task not found") from None
    trace = build_trace(events, task_id)
    settings = load_settings()
    return export_otlp(
        trace, settings.otel_exporter_endpoint,
        service_name=settings.otel_service_name,
    )


@app.get("/api/v1/tasks/{task_id}/compensations")
def list_compensations(task_id: str) -> dict:
    """Outstanding side effects that still need to be undone."""
    comps = saga.pending(task_id)
    return {
        "task_id": task_id,
        "pending": len(comps),
        "items": [
            {"id": c.id, "step_id": c.step_id, "action": c.action,
             "status": c.status.value, "attempts": c.attempts, "error": c.last_error}
            for c in comps
        ],
    }


@app.get("/api/v1/tasks/{task_id}/stream")
async def stream(task_id: str) -> StreamingResponse:
    """Server-sent events: replays the journal then tails it."""
    async def gen():
        seen = 0
        for _ in range(600):  # ~5 min tail
            for event in store.load_after(task_id, seen):
                seen = event.seq
                payload: dict[str, Any] = {
                    "seq": event.seq, "type": event.type.value,
                    "payload": event.payload, "at": event.created_at,
                }
                yield f"event: {event.type.value.lower()}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.on_event("startup")
def _warm_shared_mcp_gateway() -> None:
    """Pay the MCP interpreter startup once, not on the first incident.

    Running in the app startup path also means a broken server is visible in
    journald before a user asks for an analysis. A server that fails stays
    degraded; the health monitor retries it in the background.
    """
    if _settings.tool_backend != "mcp" or _settings.mcp_gateway_mode != "shared":
        return
    from .mcp_gateway import shared_gateway

    log.info("warming shared MCP gateway")
    shared_gateway(_settings)


@app.on_event("shutdown")
def _close_shared_mcp_gateway() -> None:
    from .mcp_gateway import close_shared_gateway

    close_shared_gateway()


__all__ = ["app", "service", "store", "leases", "NeedsHumanDecision", "TaskStatus", "StepStatus"]
