"""Enterprise-grade CI graph + change workflow for the self-hosted CMDB.

This module is intentionally separate from the v1 "service + JSON arrays"
registry. It adds the pieces a real CMDB needs:

* typed configuration items (service / host / middleware / database / storage),
* a relation table with direction, criticality and provenance,
* history snapshots on every CI/relation mutation (temporal audit),
* multi-hop impact traversal,
* change requests with an approval state machine,
* an append-only audit log for every write.

The v1 REST contract (`/api/v1/topology`, `/api/v1/changes`) remains as a
compatibility facade for the existing MCP servers. v2 is additive, so migrating
does not break the analysis chain.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from app.db import Database  # noqa: E402

CI_TYPES = ("service", "host", "middleware", "database", "storage", "external")
RELATION_TYPES = ("depends_on", "routes_to", "calls", "stores_in", "runs_on",
                  "realized_by")
CHANGE_STATES = (
    "draft", "pending_approval", "approved", "rejected",
    "executing", "succeeded", "failed", "rolled_back",
)
RISKS = ("low", "medium", "high", "critical")

DDL = """
CREATE TABLE IF NOT EXISTS ci (
    ci_id VARCHAR(128) NOT NULL,
    name VARCHAR(128) NOT NULL,
    ci_type VARCHAR(32) NOT NULL,
    owner VARCHAR(128) NULL,
    tier VARCHAR(16) NULL,
    env VARCHAR(32) NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    attributes MEDIUMTEXT NULL,
    source VARCHAR(64) NOT NULL DEFAULT 'manual',
    version INT NOT NULL DEFAULT 1,
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'team-core',
    created_at DOUBLE NOT NULL,
    updated_at DOUBLE NOT NULL,
    updated_by VARCHAR(128) NOT NULL DEFAULT 'system',
    PRIMARY KEY (ci_id)
);

CREATE TABLE IF NOT EXISTS ci_history (
    history_id VARCHAR(64) NOT NULL,
    ci_id VARCHAR(128) NOT NULL,
    version INT NOT NULL,
    snapshot MEDIUMTEXT NOT NULL,
    valid_from DOUBLE NOT NULL,
    valid_to DOUBLE NOT NULL,
    actor VARCHAR(128) NOT NULL,
    PRIMARY KEY (history_id)
);

CREATE TABLE IF NOT EXISTS ci_relation (
    relation_id VARCHAR(64) NOT NULL,
    from_ci VARCHAR(128) NOT NULL,
    to_ci VARCHAR(128) NOT NULL,
    relation_type VARCHAR(32) NOT NULL,
    criticality VARCHAR(16) NOT NULL DEFAULT 'medium',
    source VARCHAR(64) NOT NULL DEFAULT 'manual',
    attributes MEDIUMTEXT NULL,
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'team-core',
    created_at DOUBLE NOT NULL,
    updated_at DOUBLE NOT NULL,
    updated_by VARCHAR(128) NOT NULL DEFAULT 'system',
    PRIMARY KEY (relation_id)
);

CREATE TABLE IF NOT EXISTS ci_relation_history (
    history_id VARCHAR(64) NOT NULL,
    relation_id VARCHAR(64) NOT NULL,
    snapshot MEDIUMTEXT NOT NULL,
    valid_from DOUBLE NOT NULL,
    valid_to DOUBLE NOT NULL,
    actor VARCHAR(128) NOT NULL,
    PRIMARY KEY (history_id)
);

CREATE TABLE IF NOT EXISTS change_request (
    change_id VARCHAR(64) NOT NULL,
    service_ci VARCHAR(128) NOT NULL,
    title VARCHAR(255) NOT NULL,
    description MEDIUMTEXT NULL,
    risk VARCHAR(16) NOT NULL DEFAULT 'medium',
    status VARCHAR(32) NOT NULL DEFAULT 'draft',
    planned_at VARCHAR(40) NULL,
    executed_at VARCHAR(40) NULL,
    operator VARCHAR(128) NOT NULL DEFAULT 'unknown',
    rollback_of VARCHAR(64) NULL,
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'team-core',
    created_at DOUBLE NOT NULL,
    updated_at DOUBLE NOT NULL,
    updated_by VARCHAR(128) NOT NULL DEFAULT 'system',
    PRIMARY KEY (change_id)
);

CREATE TABLE IF NOT EXISTS change_approval (
    approval_id VARCHAR(64) NOT NULL,
    change_id VARCHAR(64) NOT NULL,
    approver VARCHAR(128) NOT NULL,
    decision VARCHAR(16) NOT NULL,
    comment VARCHAR(512) NULL,
    at DOUBLE NOT NULL,
    PRIMARY KEY (approval_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id VARCHAR(64) NOT NULL,
    actor VARCHAR(128) NOT NULL,
    role VARCHAR(32) NOT NULL,
    action VARCHAR(64) NOT NULL,
    object_type VARCHAR(32) NOT NULL,
    object_id VARCHAR(128) NOT NULL,
    before_json MEDIUMTEXT NULL,
    after_json MEDIUMTEXT NULL,
    at DOUBLE NOT NULL,
    PRIMARY KEY (audit_id)
);
"""


class GraphError(ValueError):
    pass


def _now() -> float:
    return round(datetime.now(timezone.utc).timestamp(), 3)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def _sid(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]


class GraphStore:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.db.executescript(DDL)
        self._migrate_tenant_columns()

    def _migrate_tenant_columns(self) -> None:
        """Additive, backward-compatible tenant columns for old databases."""
        for table in ("ci", "ci_relation", "change_request"):
            try:
                with self.db.lock:
                    cur = self.db.conn.cursor()
                    cur.execute(
                        f"ALTER TABLE {table} ADD COLUMN tenant_id "
                        "VARCHAR(64) NOT NULL DEFAULT 'team-core'"
                    )
                    self.db.commit()
            except Exception:  # noqa: BLE001 - column exists on fresh DBs
                pass

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
        return json.dumps(value or {}, ensure_ascii=False)

    @staticmethod
    def _loads(value: Any) -> dict:
        try:
            parsed = json.loads(value) if value else {}
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, json.JSONDecodeError):
            return {}

    def _audit(self, actor: str, role: str, action: str, object_type: str,
               object_id: str, before: Any = None, after: Any = None) -> None:
        self._write(
            self.db.upsert_sql("audit_log", (
                "audit_id", "actor", "role", "action", "object_type", "object_id",
                "before_json", "after_json", "at",
            )),
            [_sid("audit", object_id, str(_now()), action),
             actor[:128], role[:32], action[:64], object_type[:32],
             object_id[:128], self._json(before) if before else None,
             self._json(after) if after else None, _now()],
        )

    def _ci_out(self, row: dict) -> dict:
        out = dict(row)
        out["attributes"] = self._loads(out.get("attributes"))
        return out

    # --------------------------------------------------------------------- CIs
    def get_ci(self, ci_id: str, tenant_id: str = "") -> dict | None:
        sql = (
            "SELECT ci_id, name, ci_type, owner, tier, env, status, attributes, "
            "source, version, tenant_id, created_at, updated_at, updated_by "
            "FROM ci WHERE ci_id = ?"
        )
        params: list[Any] = [ci_id]
        if tenant_id:
            sql += " AND tenant_id = ?"
            params.append(tenant_id)
        row = self._one(sql, params)
        return self._ci_out(row) if row else None

    def list_cis(self, ci_type: str | None = None, env: str | None = None,
                 tenant_id: str = "") -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if ci_type:
            clauses.append("ci_type = ?")
            params.append(ci_type)
        if env:
            clauses.append("env = ?")
            params.append(env)
        if tenant_id:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._rows(
            "SELECT ci_id, name, ci_type, owner, tier, env, status, attributes, "
            "source, version, tenant_id, created_at, updated_at, updated_by FROM ci "
            f"{where} ORDER BY ci_type, name",
            params,
        )
        return [self._ci_out(row) for row in rows]

    def upsert_ci(self, ci_id: str, *, name: str, ci_type: str = "service",
                  owner: str = "", tier: str = "P2", env: str = "prod",
                  status: str = "active", attributes: dict | None = None,
                  source: str = "manual", actor: str = "system",
                  role: str = "editor", tenant_id: str = "team-core") -> dict:
        ci_id = (ci_id or "").strip().lower()
        if not ci_id or len(ci_id) > 128:
            raise GraphError("ci_id is required (<=128)")
        if ci_type not in CI_TYPES:
            raise GraphError(f"ci_type must be one of {CI_TYPES}")
        old = self.get_ci(ci_id)
        if old and tenant_id and old.get("tenant_id") not in ("", tenant_id):
            raise GraphError(f"CI {ci_id} belongs to another tenant")
        now = _now()
        version = int(old["version"]) + 1 if old else 1
        if old:
            self._write(
                self.db.upsert_sql("ci_history", (
                    "history_id", "ci_id", "version", "snapshot",
                    "valid_from", "valid_to", "actor",
                )),
                [_sid("cih", ci_id, str(now)), ci_id, old["version"],
                 self._json(old), float(old["updated_at"]), now, actor[:128]],
            )
        columns = ("ci_id", "name", "ci_type", "owner", "tier", "env", "status",
                   "attributes", "source", "version", "tenant_id", "created_at",
                   "updated_at", "updated_by")
        self._write(
            self.db.upsert_sql("ci", columns),
            [ci_id, name[:128], ci_type, owner[:128], (tier or "P2").upper()[:16],
             env[:32], status[:32], self._json(attributes), source[:64], version,
             tenant_id[:64] or "team-core",
             float(old["created_at"]) if old else now, now, actor[:128]],
        )
        after = self.get_ci(ci_id)
        self._audit(actor, role, "ci.upsert", "ci", ci_id, old, after)
        return after or {}

    # --------------------------------------------------------------- relations
    def relations(self, ci_id: str, direction: str = "both",
                  tenant_id: str = "") -> list[dict]:
        base = (
            "SELECT relation_id, from_ci, to_ci, relation_type, criticality, "
            "source, attributes, tenant_id, updated_at, updated_by FROM ci_relation "
        )
        params: list[Any] = [ci_id]
        tenant_sql = ""
        if tenant_id:
            tenant_sql = " AND tenant_id = ?"
        if direction == "downstream":
            sql = base + "WHERE from_ci = ?" + tenant_sql + " ORDER BY to_ci"
        elif direction == "upstream":
            sql = base + "WHERE to_ci = ?" + tenant_sql + " ORDER BY from_ci"
        else:
            sql = base + "WHERE (from_ci = ? OR to_ci = ?)" + tenant_sql + " ORDER BY to_ci"
            params = [ci_id, ci_id]
        if tenant_id:
            params.append(tenant_id)
        rows = self._rows(sql, params)
        return [self._ci_out(row) for row in rows]

    def upsert_relation(self, from_ci: str, to_ci: str, *, relation_type: str,
                        criticality: str = "medium", source: str = "manual",
                        attributes: dict | None = None,
                        actor: str = "system", role: str = "editor",
                        tenant_id: str = "team-core") -> dict:
        if relation_type not in RELATION_TYPES:
            raise GraphError(f"relation_type must be one of {RELATION_TYPES}")
        if not self.get_ci(from_ci, tenant_id) or not self.get_ci(to_ci, tenant_id):
            raise GraphError("both CIs must exist in this tenant before creating a relation")
        relation_id = _sid("rel", from_ci, to_ci, relation_type)
        old = self._one(
            "SELECT relation_id, from_ci, to_ci, relation_type, criticality, source, "
            "attributes, created_at, updated_at, updated_by FROM ci_relation "
            "WHERE relation_id = ?", [relation_id],
        )
        now = _now()
        if old:
            self._write(
                self.db.upsert_sql("ci_relation_history", (
                    "history_id", "relation_id", "snapshot", "valid_from",
                    "valid_to", "actor",
                )),
                [_sid("relh", relation_id, str(now)), relation_id,
                 self._json(old), float(old["updated_at"]), now, actor[:128]],
            )
        columns = ("relation_id", "from_ci", "to_ci", "relation_type", "criticality",
                   "source", "attributes", "tenant_id", "created_at", "updated_at",
                   "updated_by")
        self._write(
            self.db.upsert_sql("ci_relation", columns),
            [relation_id, from_ci, to_ci, relation_type, criticality[:16],
             source[:64], self._json(attributes), tenant_id[:64] or "team-core",
             float(old["created_at"]) if old else now, now, actor[:128]],
        )
        after = self._one(
            "SELECT relation_id, from_ci, to_ci, relation_type, criticality, source, "
            "attributes, updated_at, updated_by FROM ci_relation WHERE relation_id = ?",
            [relation_id],
        )
        self._audit(actor, role, "relation.upsert", "relation", relation_id, old, after)
        return self._ci_out(after) if after else {}

    def delete_relation(self, from_ci: str, to_ci: str, relation_type: str,
                        actor: str = "system", role: str = "editor") -> bool:
        relation_id = _sid("rel", from_ci, to_ci, relation_type)
        old = self._one(
            "SELECT relation_id, from_ci, to_ci, relation_type, criticality, source, "
            "attributes, created_at, updated_at, updated_by FROM ci_relation "
            "WHERE relation_id = ?", [relation_id],
        )
        if not old:
            return False
        now = _now()
        self._write(
            self.db.upsert_sql("ci_relation_history", (
                "history_id", "relation_id", "snapshot", "valid_from",
                "valid_to", "actor",
            )),
            [_sid("relh", relation_id, str(now)), relation_id,
             self._json(old), float(old["updated_at"]), now, actor[:128]],
        )
        self._write("DELETE FROM ci_relation WHERE relation_id = ?", [relation_id])
        self._audit(actor, role, "relation.delete", "relation", relation_id, old, None)
        return True

    def topology(self, ci_id: str, tenant_id: str = "") -> dict | None:
        ci = self.get_ci(ci_id, tenant_id)
        if not ci:
            return None
        downstream = [r["to_ci"] for r in self.relations(ci_id, "downstream", tenant_id)]
        upstream = [r["from_ci"] for r in self.relations(ci_id, "upstream", tenant_id)]
        return {
            "service": ci_id,
            "upstream": upstream,
            "downstream": downstream,
            "instances": 1,
            "healthy_instances": 1 if ci.get("status") == "active" else 0,
            "source": "cmdb",
            "owner": ci.get("owner"),
            "tier": ci.get("tier"),
            "ci_type": ci.get("ci_type"),
        }

    def impact(self, ci_id: str, direction: str = "downstream", depth: int = 3,
               tenant_id: str = "") -> dict:
        if not self.get_ci(ci_id, tenant_id):
            raise GraphError(f"CI {ci_id} not found")
        depth = max(1, min(int(depth), 8))
        visited = {ci_id}
        frontier = [ci_id]
        edges: list[dict] = []
        levels: dict[str, int] = {ci_id: 0}
        for level in range(1, depth + 1):
            next_frontier: list[str] = []
            for node in frontier:
                rels = self.relations(node, direction, tenant_id)
                for rel in rels:
                    neighbour = rel["to_ci"] if direction == "downstream" else rel["from_ci"]
                    edges.append({
                        "from": rel["from_ci"], "to": rel["to_ci"],
                        "type": rel["relation_type"], "criticality": rel["criticality"],
                        "depth": level,
                    })
                    if neighbour not in visited:
                        visited.add(neighbour)
                        levels[neighbour] = level
                        next_frontier.append(neighbour)
            frontier = next_frontier
            if not frontier:
                break
        return {
            "ci_id": ci_id,
            "direction": direction,
            "max_depth": depth,
            "affected": [{"ci_id": cid, "depth": levels[cid]}
                         for cid in visited if cid != ci_id],
            "edges": edges,
        }

    # ----------------------------------------------------------------- changes
    def get_change(self, change_id: str, tenant_id: str = "") -> dict | None:
        sql = (
            "SELECT change_id, service_ci, title, description, risk, status, "
            "planned_at, executed_at, operator, rollback_of, tenant_id, created_at, "
            "updated_at, updated_by FROM change_request WHERE change_id = ?"
        )
        params: list[Any] = [change_id]
        if tenant_id:
            sql += " AND tenant_id = ?"
            params.append(tenant_id)
        row = self._one(sql, params)
        if not row:
            return None
        row["approvals"] = self._rows(
            "SELECT approval_id, approver, decision, comment, at FROM change_approval "
            "WHERE change_id = ? ORDER BY at", [change_id],
        )
        return row

    def list_changes(self, service: str | None = None, status: str | None = None,
                     limit: int = 50, tenant_id: str = "") -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if service:
            clauses.append("service_ci = ?")
            params.append(service)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if tenant_id:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._rows(
            "SELECT change_id, service_ci, title, description, risk, status, "
            "planned_at, executed_at, operator, rollback_of, tenant_id, created_at, "
            "updated_at, updated_by FROM change_request "
            f"{where} ORDER BY created_at DESC LIMIT {max(1, min(int(limit), 200))}",
            params,
        )
        return rows

    def create_change(self, *, service_ci: str, title: str, description: str = "",
                      risk: str = "medium", actor: str = "system",
                      role: str = "editor", planned_at: str = "",
                      rollback_of: str = "", tenant_id: str = "team-core") -> dict:
        if not self.get_ci(service_ci, tenant_id):
            raise GraphError(f"service CI {service_ci} not found in tenant")
        if risk not in RISKS:
            raise GraphError(f"risk must be one of {RISKS}")
        change_id = _sid("chg", service_ci, title, str(_now()))
        now = _now()
        columns = ("change_id", "service_ci", "title", "description", "risk",
                   "status", "planned_at", "executed_at", "operator", "rollback_of",
                   "tenant_id", "created_at", "updated_at", "updated_by")
        self._write(
            self.db.upsert_sql("change_request", columns),
            [change_id, service_ci, title[:255], description, risk, "draft",
             planned_at[:40] or None, None, actor[:128], rollback_of[:64] or None,
             tenant_id[:64] or "team-core", now, now, actor[:128]],
        )
        after = self.get_change(change_id)
        self._audit(actor, role, "change.create", "change", change_id, None, after)
        return after or {}

    def _transition(self, change_id: str, allowed: tuple[str, ...], target: str,
                    actor: str, role: str, action: str,
                    tenant_id: str = "") -> dict:
        change = self.get_change(change_id, tenant_id)
        if not change:
            raise GraphError(f"change {change_id} not found")
        if change["status"] not in allowed:
            raise GraphError(
                f"illegal change transition {change['status']} -> {target}"
            )
        now = _now()
        self._write(
            "UPDATE change_request SET status = ?, updated_at = ?, updated_by = ?, "
            "executed_at = CASE WHEN ? IN ('succeeded','failed','rolled_back') "
            "THEN ? ELSE executed_at END WHERE change_id = ?",
            [target, now, actor[:128], target, _iso(now), change_id],
        )
        after = self.get_change(change_id, tenant_id)
        self._audit(actor, role, action, "change", change_id, change, after)
        return after or {}

    def submit_change(self, change_id: str, actor: str, role: str = "editor",
                      tenant_id: str = "") -> dict:
        return self._transition(change_id, ("draft",), "pending_approval",
                                actor, role, "change.submit", tenant_id)

    def approve_change(self, change_id: str, approver: str, decision: str = "approve",
                       comment: str = "", role: str = "approver",
                       tenant_id: str = "") -> dict:
        if role != "approver":
            raise GraphError("change approval requires approver role")
        target = "approved" if decision == "approve" else "rejected"
        change = self._transition(change_id, ("pending_approval",), target,
                                  approver, role, f"change.{decision}", tenant_id)
        self._write(
            self.db.upsert_sql("change_approval", (
                "approval_id", "change_id", "approver", "decision", "comment", "at",
            )),
            [_sid("appr", change_id, approver, str(_now())), change_id,
             approver[:128], target, comment[:512], _now()],
        )
        return self.get_change(change_id, tenant_id) or change

    def execute_change(self, change_id: str, actor: str, success: bool = True,
                       role: str = "editor", tenant_id: str = "") -> dict:
        target = "succeeded" if success else "failed"
        return self._transition(change_id, ("approved", "executing"), target,
                                actor, role, "change.execute", tenant_id)

    def rollback_change(self, change_id: str, actor: str, role: str = "editor",
                        tenant_id: str = "") -> dict:
        original = self._transition(change_id, ("succeeded", "failed"), "rolled_back",
                                    actor, role, "change.rollback", tenant_id)
        rollback = self.create_change(
            service_ci=original["service_ci"],
            title=f"rollback of {change_id[:12]}",
            description=f"rollback requested by {actor}",
            risk="high", actor=actor, role=role, rollback_of=change_id,
            tenant_id=tenant_id or original.get("tenant_id") or "team-core",
        )
        return {"original": original, "rollback_change": rollback}

    def quality_report(self, tenant_id: str = "") -> dict:
        """Expose data-quality debt instead of hiding discovery gaps."""
        cis = self.list_cis(tenant_id=tenant_id)
        unowned = [
            c["ci_id"] for c in cis
            if (c.get("attributes") or {}).get("unowned")
            or not c.get("owner") or c.get("owner") == "unowned"
        ]
        unattached = [
            c["ci_id"] for c in cis
            if (c.get("attributes") or {}).get("unattached")
        ]
        missing_tier = [c["ci_id"] for c in cis if not c.get("tier")]
        relation_sql = "SELECT relation_id, from_ci, to_ci FROM ci_relation"
        relation_params: list[Any] = []
        if tenant_id:
            relation_sql += " WHERE tenant_id = ?"
            relation_params.append(tenant_id)
        relations = self._rows(relation_sql, relation_params)
        return {
            "cis": len(cis),
            "relations": len(relations),
            "unowned": len(unowned),
            "unowned_sample": unowned[:20],
            # Inventory-only resources are allowed (e.g. an audit bucket);
            # they are reported separately from a missing owner.
            "unattached": len(unattached),
            "unattached_sample": unattached[:20],
            "missing_tier": len(missing_tier),
            "missing_tier_sample": missing_tier[:20],
            "ok": not unowned and not missing_tier,
        }

    def audit_log(self, limit: int = 100) -> list[dict]:
        # Audit is intentionally platform-wide: platform_admin reviews cross-
        # tenant writes. Tenant filtering is applied to CI/change reads.
        return self._rows(
            "SELECT audit_id, actor, role, action, object_type, object_id, "
            "before_json, after_json, at FROM audit_log ORDER BY at DESC "
            f"LIMIT {max(1, min(int(limit), 500))}"
        )

    # -------------------------------------------------------------------- seed
    def seed_from_legacy(self, legacy: Any) -> None:
        if self._one("SELECT ci_id FROM ci LIMIT 1"):
            return
        type_map = {
            "prometheus": "middleware", "loki": "middleware",
            "rds-mysql": "database", "oss": "storage", "contextdb": "middleware",
        }
        for service in legacy.list_services():
            self.upsert_ci(
                service["name"], name=service.get("display_name") or service["name"],
                ci_type=type_map.get(service["name"], "service"),
                owner=service.get("owner") or "", tier=service.get("tier") or "P2",
                env="prod", status="active",
                attributes={"description": service.get("description") or "",
                            "legacy": True},
                source="legacy-migration", actor="system:seed", role="editor",
            )
        for service in legacy.list_services():
            topo = legacy.topology(service["name"]) or {}
            for target in topo.get("downstream") or []:
                self.upsert_relation(
                    service["name"], target, relation_type="depends_on",
                    criticality="high" if target in ("rds-mysql", "prometheus") else "medium",
                    source="legacy-migration", actor="system:seed", role="editor",
                )
