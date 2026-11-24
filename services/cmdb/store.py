"""Lightweight, self-hosted CMDB + change registry.

Why a separate service instead of reusing the catalog tables:

* The catalog says "where to query telemetry for a service"; the CMDB says
  "what the service is made of and what changed". Those are different lifecycles
  and different owners.
* The MCP servers already speak a simple HTTP contract
  (``/api/v1/topology/{service}``, ``/api/v1/changes``); keeping that contract
  means switching from devstack to real data requires only a catalog binding.

Data is persisted in MySQL (production) or SQLite (tests/local), and is written
through a real API — not a fixture loaded into the MCP process. The service
binds loopback only; release registration is an internal operation.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from fastapi import FastAPI, HTTPException  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from app.db import Database  # noqa: E402

DDL = """
CREATE TABLE IF NOT EXISTS cmdb_service (
    name VARCHAR(128) NOT NULL,
    display_name VARCHAR(255) NULL,
    owner VARCHAR(128) NULL,
    tier VARCHAR(16) NULL,
    description VARCHAR(1024) NULL,
    upstream MEDIUMTEXT NULL,
    downstream MEDIUMTEXT NULL,
    instances INT NOT NULL DEFAULT 1,
    healthy_instances INT NOT NULL DEFAULT 1,
    updated_at DOUBLE NOT NULL,
    PRIMARY KEY (name)
);

CREATE TABLE IF NOT EXISTS cmdb_change (
    change_id VARCHAR(64) NOT NULL,
    service_name VARCHAR(128) NOT NULL,
    at VARCHAR(40) NOT NULL,
    at_epoch DOUBLE NOT NULL,
    kind VARCHAR(32) NOT NULL,
    summary VARCHAR(512) NOT NULL,
    operator VARCHAR(128) NOT NULL,
    created_at DOUBLE NOT NULL,
    PRIMARY KEY (change_id)
);
"""


class CmdbError(ValueError):
    pass


def _parse_window(window: str) -> int:
    text = (window or "2h").strip().lower()
    units = {"m": 60, "h": 3600, "d": 86400}
    if len(text) < 2 or text[-1] not in units:
        return 7200
    try:
        return max(60, int(float(text[:-1])) * units[text[-1]])
    except ValueError:
        return 7200


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


class CmdbStore:
    def __init__(self, db: Database, *, seed: bool = True) -> None:
        self.db = db
        self.db.executescript(DDL)
        if seed:
            self.seed_defaults()

    # ------------------------------------------------------------------ helpers
    def _rows(self, statement: str, params: list[Any] | tuple[Any, ...] = ()) -> list[dict]:
        with self.db.lock:
            cur = self.db.conn.cursor()
            cur.execute(self.db.sql(statement), tuple(params))
            columns = [d[0] for d in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    def _one(self, statement: str, params: list[Any] | tuple[Any, ...] = ()) -> dict | None:
        rows = self._rows(statement, params)
        return rows[0] if rows else None

    def _write(self, statement: str, params: list[Any] | tuple[Any, ...]) -> None:
        with self.db.lock:
            cur = self.db.conn.cursor()
            cur.execute(self.db.sql(statement), tuple(params))
            self.db.commit()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value or [], ensure_ascii=False)

    @staticmethod
    def _loads(value: Any) -> list[str]:
        try:
            parsed = json.loads(value) if value else []
            return [str(item) for item in parsed] if isinstance(parsed, list) else []
        except (TypeError, json.JSONDecodeError):
            return []

    # ---------------------------------------------------------------- services
    def list_services(self) -> list[dict]:
        rows = self._rows(
            "SELECT name, display_name, owner, tier, description, upstream, downstream, "
            "instances, healthy_instances, updated_at FROM cmdb_service ORDER BY name"
        )
        return [self._service_out(row) for row in rows]

    def get_service(self, name: str) -> dict | None:
        row = self._one(
            "SELECT name, display_name, owner, tier, description, upstream, downstream, "
            "instances, healthy_instances, updated_at FROM cmdb_service WHERE name = ?",
            [name],
        )
        return self._service_out(row) if row else None

    @staticmethod
    def _service_out(row: dict) -> dict:
        out = dict(row)
        out["upstream"] = CmdbStore._loads(out.get("upstream"))
        out["downstream"] = CmdbStore._loads(out.get("downstream"))
        return out

    def upsert_service(
        self,
        name: str,
        *,
        display_name: str = "",
        owner: str = "",
        tier: str = "P2",
        description: str = "",
        upstream: list[str] | None = None,
        downstream: list[str] | None = None,
        instances: int = 1,
        healthy_instances: int = 1,
    ) -> dict:
        name = (name or "").strip()
        if not name or len(name) > 128:
            raise CmdbError("service name is required (<=128)")
        columns = ("name", "display_name", "owner", "tier", "description",
                   "upstream", "downstream", "instances", "healthy_instances",
                   "updated_at")
        self._write(
            self.db.upsert_sql("cmdb_service", columns),
            [name, display_name[:255], owner[:128], (tier or "P2").upper()[:16],
             description[:1024], self._json(upstream), self._json(downstream),
             max(0, int(instances)), max(0, int(healthy_instances)),
             round(datetime.now(timezone.utc).timestamp(), 3)],
        )
        return self.get_service(name) or {}

    def topology(self, name: str) -> dict | None:
        svc = self.get_service(name)
        if not svc:
            return None
        return {
            "service": name,
            "upstream": svc["upstream"],
            "downstream": svc["downstream"],
            "instances": svc["instances"],
            "healthy_instances": svc["healthy_instances"],
            "source": "cmdb",
            "owner": svc.get("owner"),
            "tier": svc.get("tier"),
        }

    # ----------------------------------------------------------------- changes
    def add_change(
        self,
        service: str,
        summary: str,
        *,
        kind: str = "deploy",
        operator: str = "deploy-bot",
        change_id: str = "",
        at: str = "",
    ) -> dict:
        if not self.get_service(service):
            raise CmdbError(f"service {service} is not registered")
        at = (at or "").strip() or _iso(datetime.now(timezone.utc).timestamp())
        try:
            epoch = datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()
        except ValueError as exc:
            raise CmdbError(f"invalid at timestamp: {at}") from exc
        change_id = (change_id or "").strip()[:64] or (
            f"CHG-{service}-{int(epoch)}"
        )
        columns = ("change_id", "service_name", "at", "at_epoch", "kind", "summary",
                   "operator", "created_at")
        self._write(
            self.db.upsert_sql("cmdb_change", columns),
            [change_id, service, _iso(epoch), epoch, (kind or "deploy")[:32],
             (summary or "")[:512], (operator or "unknown")[:128],
             datetime.now(timezone.utc).timestamp()],
        )
        return {
            "change_id": change_id,
            "service": service,
            "at": _iso(epoch),
            "kind": kind,
            "summary": summary,
            "operator": operator,
        }

    def delete_change(self, change_id: str) -> bool:
        """Remove a mistakenly-scoped change record.

        Used for account-level ActionTrail bootstrap events that were imported
        before the resource-scoping filter existed. Not exposed on the public
        API; it is an operator cleanup helper.
        """
        exists = self._one(
            "SELECT change_id FROM cmdb_change WHERE change_id = ?", [change_id]
        )
        if not exists:
            return False
        self._write("DELETE FROM cmdb_change WHERE change_id = ?", [change_id])
        return True

    def changes(self, service: str, window: str = "2h") -> list[dict]:
        seconds = _parse_window(window)
        since = datetime.now(timezone.utc).timestamp() - seconds
        rows = self._rows(
            "SELECT change_id, service_name, at, kind, summary, operator "
            "FROM cmdb_change WHERE service_name = ? AND at_epoch >= ? "
            "ORDER BY at_epoch DESC",
            [service, since],
        )
        return [
            {
                "change_id": row["change_id"],
                "service": row["service_name"],
                "at": row["at"],
                "kind": row["kind"],
                "summary": row["summary"],
                "operator": row["operator"],
            }
            for row in rows
        ]

    # -------------------------------------------------------------------- seed
    def seed_defaults(self) -> None:
        existing = self._one("SELECT name FROM cmdb_service LIMIT 1")
        if existing:
            return
        dependencies = {
            "prometheus": ("Prometheus", "SRE", "P1", "指标存储"),
            "loki": ("Loki", "SRE", "P1", "日志存储"),
            "rds-mysql": ("RDS MySQL", "DBA", "P0", "业务/事件库"),
            "oss": ("OSS", "SRE", "P1", "报告归档"),
            "contextdb": ("ContextDB", "SRE", "P1", "长期记忆"),
        }
        for name, (display, owner, tier, desc) in dependencies.items():
            self.upsert_service(
                name, display_name=display, owner=owner, tier=tier,
                description=desc, instances=1, healthy_instances=1,
            )
        self.upsert_service(
            "ecs-copilot",
            display_name="Copilot ECS（自监控）",
            owner="SRE",
            tier="P0",
            description="运行 Postmortem Copilot 的阿里云 ECS",
            downstream=list(dependencies),
            instances=1,
            healthy_instances=1,
        )
        now = datetime.now(timezone.utc).timestamp()
        self.add_change(
            "ecs-copilot",
            "M9 共享 MCP 网关 + 真实 Loki + 影响面上线",
            change_id="CHG-M9-001",
            at=_iso(now - 3600),
            operator="deploy-bot",
        )
        self.add_change(
            "ecs-copilot",
            "M10.1 指标包 / counter / 磁盘 IO 指标上线",
            change_id="CHG-M10-001",
            at=_iso(now - 1800),
            operator="deploy-bot",
        )
        self.add_change(
            "ecs-copilot",
            "M10.2 自建 CMDB + 发布变更服务上线",
            change_id="CHG-M10-002",
            at=_iso(now - 60),
            operator="deploy-bot",
        )


class ServiceIn(BaseModel):
    display_name: str = ""
    owner: str = ""
    tier: str = "P2"
    description: str = ""
    upstream: list[str] = Field(default_factory=list)
    downstream: list[str] = Field(default_factory=list)
    instances: int = 1
    healthy_instances: int = 1


class ChangeIn(BaseModel):
    service: str
    summary: str = Field(min_length=1, max_length=512)
    kind: str = "deploy"
    operator: str = "deploy-bot"
    change_id: str = ""
    at: str = ""


def create_app(store: CmdbStore, graph: Any | None = None,
               tokens: dict[str, str] | None = None,
               tenant: str = "team-core",
               platform_token: str = "") -> FastAPI:
    app = FastAPI(title="Postmortem CMDB", version="0.2.0")

    @app.get("/healthz")
    def healthz() -> dict:
        result = {"status": "ok", "services": len(store.list_services())}
        if graph is not None:
            result["cis"] = len(graph.list_cis())
        return result

    @app.get("/api/v1/services")
    def services() -> dict:
        return {"services": store.list_services()}

    @app.get("/api/v1/services/{name}")
    def service(name: str) -> dict:
        found = store.get_service(name)
        if not found:
            raise HTTPException(404, f"service {name} not found")
        return found

    @app.put("/api/v1/services/{name}")
    def upsert_service(name: str, body: ServiceIn) -> dict:
        try:
            return store.upsert_service(name, **body.model_dump())
        except CmdbError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/v1/topology/{name}")
    def topology(name: str) -> dict:
        # The v1 contract stays stable, but the enterprise graph is the source
        # of truth once a CI exists. Legacy arrays are only a fallback.
        if graph is not None:
            found = graph.topology(name)
            if found:
                return found
        found = store.topology(name)
        if not found:
            raise HTTPException(404, f"service {name} not found")
        return found

    @app.get("/api/v1/changes")
    def changes(service: str, window: str = "2h") -> dict:
        # Merge the legacy deployment feed with executed v2 change requests.
        merged = list(store.changes(service, window))
        if graph is not None:
            seen = {item["change_id"] for item in merged}
            for change in graph.list_changes(service, "succeeded", limit=100):
                if change["change_id"] in seen:
                    continue
                merged.append({
                    "change_id": change["change_id"],
                    "service": change["service_ci"],
                    "at": change.get("executed_at") or change.get("planned_at")
                    or _iso(datetime.now(timezone.utc).timestamp()),
                    "kind": "deploy",
                    "summary": change["title"],
                    "operator": change.get("operator") or "unknown",
                })
            merged.sort(key=lambda item: item.get("at") or "", reverse=True)
        return {
            "service": service,
            "window": window,
            "changes": merged,
            "source": "deploy",
        }

    @app.post("/api/v1/changes", status_code=201)
    def add_change(body: ChangeIn) -> dict:
        try:
            return store.add_change(body.service, body.summary, kind=body.kind,
                                    operator=body.operator, change_id=body.change_id,
                                    at=body.at)
        except CmdbError as exc:
            raise HTTPException(400, str(exc))

    if graph is not None:
        from .v2 import build_router

        app.include_router(build_router(
            graph, tokens or {}, tenant=tenant, platform_token=platform_token
        ))

    return app


__all__ = ["CmdbStore", "CmdbError", "create_app"]
