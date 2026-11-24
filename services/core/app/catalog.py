"""Service catalog: which datasource serves which service, and how honest the
data is.

Why this exists
---------------
Before the catalog, every tool call hardcoded ``service="<name>"`` against one
globally configured upstream. That works for a demo with one Prometheus, but it
breaks the moment a platform has:

* several Prometheus / Loki tenants (prod, pre, per-BU),
* services whose telemetry simply is not wired yet,
* and mock fixtures used for demos, which must never look like production data.

The catalog is the single source of truth for those three questions. The agent
runtime asks it "where do I query ``order-service`` metrics?" and gets back a
datasource URL, a selector, a metric and an explicit **mode** (``real`` or
``mock``). A binding whose mode contradicts its datasource is rejected at write
time, so a mock result cannot silently be labelled real later.

Design constraints
------------------
* One DDL for SQLite and MySQL (the types are MySQL-compatible and SQLite's
  affinity accepts them). Two schemas would drift.
* No secrets in the catalog: only base URLs. Credentials stay in env vars of
  the MCP server processes.
* Every write is validated here, not in the HTTP layer, so the CLI/tests get
  the same guarantees as the admin page.
* ``resolve()`` is read on every tool call. A catalog edit applies to the very
  next analysis without a restart; the row count is tiny and the query is a
  three-table join on primary keys.
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import urlparse
from typing import Any

from .config import Settings, load_settings
from .db import CATALOG_DDL, Database
from .tools import SSRFBlocked, assert_url_allowed

KINDS: tuple[str, ...] = ("prometheus", "loki", "cmdb", "deploy", "kb")
DATASOURCE_KINDS: tuple[str, ...] = KINDS + ("multi",)
MODES: tuple[str, ...] = ("real", "mock")
METRIC_TYPES: tuple[str, ...] = ("gauge", "counter")
KIND_LABEL = {
    "prometheus": "指标 Prometheus",
    "loki": "日志 Loki",
    "cmdb": "拓扑 CMDB",
    "deploy": "变更发布",
    "kb": "知识库 KB",
}
MODE_LABEL = {"real": "真实", "mock": "Mock"}

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class CatalogError(ValueError):
    """A catalog write that violates a business invariant."""


def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise CatalogError(
            "服务名/数据源名只能包含小写字母、数字、点、下划线、连字符，长度 1–64"
        )
    return name


def _validate_url(url: str, settings: Settings) -> str:
    url = (url or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise CatalogError("数据源地址必须是 http(s)://host[:port]")
    try:
        assert_url_allowed(url, settings.tool_url_allowlist)
    except SSRFBlocked as exc:
        raise CatalogError(
            f"数据源 host 不在白名单 {settings.tool_url_allowlist}: {exc}"
        ) from exc
    return url


def _clean_selector(selector: str) -> str:
    selector = (selector or "").strip()
    if "\n" in selector or "\r" in selector or "\x00" in selector:
        raise CatalogError("selector 不能包含换行或空字符")
    if len(selector) > 1000:
        raise CatalogError("selector 过长（上限 1000 字符）")
    return selector


def _clean_metric_type(value: str) -> str:
    value = (value or "gauge").strip().lower()
    if value not in METRIC_TYPES:
        raise CatalogError(f"metric_type 只能是 {METRIC_TYPES}")
    return value


def _clean_extra_metrics(value: Any) -> str:
    """Normalise a metric pack to a JSON string.

    Accepts a JSON string (admin form) or a list (JSON API). Keeping the list
    small is deliberate: an unbounded pack would turn one analysis into dozens
    of Prometheus queries.
    """
    if value in (None, "", []):
        return "[]"
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise CatalogError(f"extra_metrics 不是合法 JSON: {exc}") from exc
    if not isinstance(value, list):
        raise CatalogError("extra_metrics 必须是数组")
    if len(value) > 8:
        raise CatalogError("extra_metrics 最多 8 条")
    cleaned: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise CatalogError("extra_metrics 每项必须是对象")
        metric = str(item.get("metric") or "").strip()
        if not metric:
            raise CatalogError("extra_metrics.metric 不能为空")
        metric_type = _clean_metric_type(str(item.get("type") or "gauge"))
        cleaned.append({
            "metric": metric[:128],
            "type": metric_type,
            "label": str(item.get("label") or metric)[:64],
        })
    return json.dumps(cleaned, ensure_ascii=False)


def _clean_impact_policy(value: Any) -> str:
    """Normalise business impact assumptions.

    The numbers are explicitly *configured assumptions*, not measured facts.
    Keeping them in the catalog lets finance/owners change them without a code
    deploy, while the report still labels them as estimates.
    """
    if value in (None, "", {}):
        return "{}"
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise CatalogError(f"impact_policy 不是合法 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise CatalogError("impact_policy 必须是对象")
    allowed = {"users_per_request", "cost_per_minute", "currency", "note"}
    unknown = set(value) - allowed
    if unknown:
        raise CatalogError(f"impact_policy 未知字段: {sorted(unknown)}")
    cleaned: dict[str, Any] = {}
    for key in ("users_per_request", "cost_per_minute"):
        if key in value:
            try:
                number = float(value[key])
            except (TypeError, ValueError) as exc:
                raise CatalogError(f"impact_policy.{key} 必须是数字") from exc
            if number < 0:
                raise CatalogError(f"impact_policy.{key} 不能为负")
            cleaned[key] = number
    if "currency" in value:
        cleaned["currency"] = str(value["currency"])[:8] or "CNY"
    if "note" in value:
        cleaned["note"] = str(value["note"])[:200]
    return json.dumps(cleaned, ensure_ascii=False)


class CatalogStore:
    """CRUD + resolution over ``datasource`` / ``service_catalog`` /
    ``service_binding``. Thread-safe through the shared :class:`Database` lock.
    """

    def __init__(
        self,
        db: Database,
        settings: Settings | None = None,
        *,
        seed: bool = True,
    ) -> None:
        self.db = db
        self.settings = settings or load_settings()
        self.db.executescript(CATALOG_DDL)
        self._migrate_columns()
        if seed:
            self.seed_defaults()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _now() -> float:
        return round(time.time(), 3)

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

    def _migrate_columns(self) -> None:
        """Add M10 columns to an existing ``service_binding`` table.

        ``CREATE TABLE IF NOT EXISTS`` does not alter an existing table, and
        SQLite/MySQL both lack a portable ``ADD COLUMN IF NOT EXISTS``. Reading
        the column list first is the only version that works on both.
        """
        if self.db.backend == "mysql":
            rows = self._rows("SHOW COLUMNS FROM service_binding")
            existing = {row.get("Field") for row in rows}
        else:
            rows = self._rows("PRAGMA table_info(service_binding)")
            existing = {row.get("name") for row in rows}
        additions = [
            ("metric_type", "VARCHAR(16) NOT NULL DEFAULT 'gauge'"),
            ("secondary_metric_type", "VARCHAR(16) NOT NULL DEFAULT 'gauge'"),
            ("extra_metrics", "MEDIUMTEXT NULL"),
            ("impact_policy", "MEDIUMTEXT NULL"),
        ]
        for name, ddl in additions:
            if name not in existing:
                self._write(f"ALTER TABLE service_binding ADD COLUMN {name} {ddl}", [])

    # -------------------------------------------------------------- datasources
    def list_datasources(self) -> list[dict]:
        rows = self._rows(
            "SELECT name, kind, base_url, mock, description, enabled, updated_at, "
            "updated_by FROM datasource ORDER BY kind, name"
        )
        return [self._ds_out(r) for r in rows]

    def get_datasource(self, name: str) -> dict | None:
        row = self._one(
            "SELECT name, kind, base_url, mock, description, enabled, updated_at, "
            "updated_by FROM datasource WHERE name = ?",
            [name],
        )
        return self._ds_out(row) if row else None

    @staticmethod
    def _ds_out(row: dict) -> dict:
        row = dict(row)
        row["mock"] = bool(row.get("mock"))
        row["enabled"] = bool(row.get("enabled"))
        return row

    def upsert_datasource(
        self,
        name: str,
        *,
        kind: str,
        base_url: str,
        mock: bool = False,
        description: str = "",
        enabled: bool = True,
        actor: str = "admin",
    ) -> dict:
        name = _validate_name(name)
        if kind not in DATASOURCE_KINDS:
            raise CatalogError(f"未知数据源类型 {kind!r}，可选 {DATASOURCE_KINDS}")
        url = _validate_url(base_url, self.settings)
        columns = ("name", "kind", "base_url", "mock", "description", "enabled",
                   "updated_at", "updated_by")
        self._write(
            self.db.upsert_sql("datasource", columns),
            [name, kind, url, int(bool(mock)), description[:512], int(bool(enabled)),
             self._now(), actor[:128]],
        )
        return self.get_datasource(name) or {}

    def delete_datasource(self, name: str) -> None:
        binding = self._one(
            "SELECT service_name, kind FROM service_binding WHERE datasource_name = ? LIMIT 1",
            [name],
        )
        if binding:
            raise CatalogError(
                f"数据源 {name} 仍被 {binding['service_name']}/{binding['kind']} 使用，先改绑定"
            )
        self._write("DELETE FROM datasource WHERE name = ?", [name])

    # ----------------------------------------------------------------- services
    def list_services(self) -> list[dict]:
        rows = self._rows(
            "SELECT name, display_name, owner, tier, description, enabled, updated_at, "
            "updated_by FROM service_catalog ORDER BY name"
        )
        out = [self._svc_out(r) for r in rows]
        counts = self._rows(
            "SELECT service_name, kind, mode FROM service_binding ORDER BY service_name, kind"
        )
        by_service: dict[str, list[dict]] = {}
        for c in counts:
            by_service.setdefault(c["service_name"], []).append(
                {"kind": c["kind"], "mode": c["mode"]}
            )
        for svc in out:
            bindings = by_service.get(svc["name"], [])
            svc["bindings"] = bindings
            svc["binding_count"] = len(bindings)
            svc["mock_count"] = sum(1 for b in bindings if b["mode"] == "mock")
        return out

    @staticmethod
    def _svc_out(row: dict) -> dict:
        row = dict(row)
        row["enabled"] = bool(row.get("enabled"))
        return row

    def get_service(self, name: str) -> dict | None:
        row = self._one(
            "SELECT name, display_name, owner, tier, description, enabled, updated_at, "
            "updated_by FROM service_catalog WHERE name = ?",
            [name],
        )
        if not row:
            return None
        svc = self._svc_out(row)
        svc["bindings"] = self._bindings_for(name)
        return svc

    def upsert_service(
        self,
        name: str,
        *,
        display_name: str = "",
        owner: str = "",
        tier: str = "P2",
        description: str = "",
        enabled: bool = True,
        actor: str = "admin",
    ) -> dict:
        name = _validate_name(name)
        tier = (tier or "P2").strip().upper()
        if tier not in ("P0", "P1", "P2", "P3"):
            raise CatalogError("tier 只能是 P0/P1/P2/P3")
        columns = ("name", "display_name", "owner", "tier", "description", "enabled",
                   "updated_at", "updated_by")
        self._write(
            self.db.upsert_sql("service_catalog", columns),
            [name, display_name[:255], owner[:128], tier, description[:1024],
             int(bool(enabled)), self._now(), actor[:128]],
        )
        return self.get_service(name) or {}

    def delete_service(self, name: str) -> None:
        with self.db.lock:
            cur = self.db.conn.cursor()
            cur.execute(self.db.sql("DELETE FROM service_binding WHERE service_name = ?"),
                        (name,))
            cur.execute(self.db.sql("DELETE FROM service_catalog WHERE name = ?"), (name,))
            self.db.commit()

    # ----------------------------------------------------------------- bindings
    def _bindings_for(self, service: str) -> list[dict]:
        rows = self._rows(
            "SELECT b.service_name, b.kind, b.datasource_name, b.selector, b.metric, "
            "b.secondary_metric, b.metric_type, b.secondary_metric_type, b.extra_metrics, "
            "b.impact_policy, b.mode, b.enabled, b.updated_at, b.updated_by, "
            "d.base_url, d.mock, d.kind AS datasource_kind, d.enabled AS datasource_enabled "
            "FROM service_binding b LEFT JOIN datasource d ON d.name = b.datasource_name "
            "WHERE b.service_name = ? ORDER BY b.kind",
            [service],
        )
        return [self._binding_out(r) for r in rows]

    @staticmethod
    def _binding_out(row: dict) -> dict:
        row = dict(row)
        row["enabled"] = bool(row.get("enabled"))
        row["mock"] = bool(row.get("mock"))
        row["datasource_enabled"] = bool(row.get("datasource_enabled"))
        row["metric_type"] = row.get("metric_type") or "gauge"
        row["secondary_metric_type"] = row.get("secondary_metric_type") or "gauge"
        raw_extra = row.get("extra_metrics")
        try:
            parsed = json.loads(raw_extra) if raw_extra else []
            row["extra_metrics"] = parsed if isinstance(parsed, list) else []
        except (TypeError, json.JSONDecodeError):
            row["extra_metrics"] = []
        raw_policy = row.get("impact_policy")
        try:
            policy = json.loads(raw_policy) if raw_policy else {}
            row["impact_policy"] = policy if isinstance(policy, dict) else {}
        except (TypeError, json.JSONDecodeError):
            row["impact_policy"] = {}
        return row

    def upsert_binding(
        self,
        service: str,
        kind: str,
        *,
        datasource_name: str,
        mode: str = "real",
        selector: str = "",
        metric: str = "",
        secondary_metric: str = "",
        metric_type: str = "gauge",
        secondary_metric_type: str = "gauge",
        extra_metrics: Any = None,
        impact_policy: Any = None,
        enabled: bool = True,
        actor: str = "admin",
    ) -> dict:
        if kind not in KINDS:
            raise CatalogError(f"未知证据类型 {kind!r}，可选 {KINDS}")
        if mode not in MODES:
            raise CatalogError(f"mode 只能是 {MODES}")
        svc = self.get_service(service)
        if not svc:
            raise CatalogError(f"服务 {service} 不存在，先建服务")
        ds = self.get_datasource(datasource_name)
        if not ds:
            raise CatalogError(f"数据源 {datasource_name} 不存在")
        if not ds["enabled"]:
            raise CatalogError(f"数据源 {datasource_name} 已停用")
        if ds["kind"] not in ("multi", kind):
            raise CatalogError(
                f"数据源 {datasource_name} 类型是 {ds['kind']}，不能绑定为 {kind}"
            )
        # The honesty invariant: a mock datasource may only be bound as mock,
        # a real datasource may only be bound as real. This is what makes the
        # ``mode`` column trustworthy for downstream evidence labelling.
        if mode == "mock" and not ds["mock"]:
            raise CatalogError(f"{datasource_name} 是真实数据源，不能标记为 mock")
        if mode == "real" and ds["mock"]:
            raise CatalogError(f"{datasource_name} 是 Mock 数据源，必须标记为 mock")

        if kind == "prometheus" and not (metric or "").strip():
            metric = "latency_p99_ms"
        selector = _clean_selector(selector)
        metric_type = _clean_metric_type(metric_type)
        secondary_metric_type = _clean_metric_type(secondary_metric_type)
        extra_json = _clean_extra_metrics(extra_metrics)
        policy_json = _clean_impact_policy(impact_policy)
        columns = ("service_name", "kind", "datasource_name", "selector", "metric",
                   "secondary_metric", "metric_type", "secondary_metric_type",
                   "extra_metrics", "impact_policy", "mode", "enabled", "updated_at",
                   "updated_by")
        self._write(
            self.db.upsert_sql("service_binding", columns),
            [service, kind, datasource_name, selector[:1024], (metric or "")[:128],
             (secondary_metric or "")[:128], metric_type, secondary_metric_type,
             extra_json, policy_json, mode, int(bool(enabled)), self._now(),
             actor[:128]],
        )
        return next(
            (b for b in self._bindings_for(service) if b["kind"] == kind), {}
        )

    def delete_binding(self, service: str, kind: str) -> None:
        self._write(
            "DELETE FROM service_binding WHERE service_name = ? AND kind = ?",
            [service, kind],
        )

    # ---------------------------------------------------------------- resolve
    def resolve(self, service: str, kind: str) -> dict | None:
        """Resolve one service/kind to a usable datasource, or ``None``.

        Disabled services, disabled bindings and disabled datasources all
        resolve to ``None``: "not configured" and "turned off" must look the
        same to the agent (no evidence), never fall back to another tenant.
        """
        row = self._one(
            "SELECT b.kind, b.datasource_name, b.selector, b.metric, "
            "b.secondary_metric, b.metric_type, b.secondary_metric_type, "
            "b.extra_metrics, b.impact_policy, b.mode, b.enabled, d.base_url, d.mock, "
            "d.enabled AS datasource_enabled, d.kind AS datasource_kind "
            "FROM service_binding b "
            "JOIN service_catalog s ON s.name = b.service_name "
            "JOIN datasource d ON d.name = b.datasource_name "
            "WHERE b.service_name = ? AND b.kind = ? "
            "AND b.enabled = 1 AND s.enabled = 1 AND d.enabled = 1",
            [service, kind],
        )
        if not row:
            return None
        out = self._binding_out(row)
        out["service"] = service
        return out

    # ------------------------------------------------------------------- seed
    def seed_defaults(self) -> None:
        """Create the two services the demo actually has.

        Only runs when the catalog is empty, so an operator's edits are never
        overwritten by a restart.
        """
        existing = self._one("SELECT name FROM service_catalog LIMIT 1")
        if existing:
            return

        actor = "system:seed"
        self.upsert_datasource(
            "ecs-prometheus",
            kind="prometheus",
            base_url=self.settings.prometheus_url,
            mock=False,
            description="ECS 本机 Prometheus 2.53（真实指标，15s 抓取）",
            actor=actor,
        )
        self.upsert_datasource(
            "devstack",
            kind="multi",
            base_url=self.settings.devstack_url,
            mock=True,
            description="Devstack 演练上游（Mock 日志/变更/拓扑，结果必须标黄）",
            actor=actor,
        )
        # Real journald -> Loki. The selector is intentionally narrow: only
        # copilot's own unit, not every system log on the host.
        self.upsert_datasource(
            "ecs-loki",
            kind="loki",
            base_url=self.settings.loki_url,
            mock=False,
            description="ECS journald 日志（Promtail -> Loki 2.9.8，真实日志）",
            actor=actor,
        )
        self.upsert_datasource(
            "ecs-cmdb",
            kind="cmdb",
            base_url=self.settings.cmdb_url,
            mock=False,
            description="平台自带轻量 CMDB（真实服务拓扑，127.0.0.1:9200）",
            actor=actor,
        )
        self.upsert_datasource(
            "ecs-deploy",
            kind="deploy",
            base_url=self.settings.deploy_url,
            mock=False,
            description="平台自带发布/变更登记服务（真实变更记录，127.0.0.1:9200）",
            actor=actor,
        )
        self.upsert_service(
            "ecs-copilot",
            display_name="Copilot ECS（自监控）",
            owner="SRE",
            tier="P0",
            description="运行本平台的阿里云 ECS；node_exporter 真实指标，service=ecs-copilot",
            actor=actor,
        )
        self.upsert_binding(
            "ecs-copilot",
            "prometheus",
            datasource_name="ecs-prometheus",
            mode="real",
            selector='service="ecs-copilot"',
            metric="node_load1",
            metric_type="gauge",
            secondary_metric="node_memory_MemAvailable_bytes",
            secondary_metric_type="gauge",
            extra_metrics=[{
                "metric": "node_disk_io_time_seconds_total",
                "type": "counter",
                "label": "磁盘 IO 时间占比",
            }, {
                "metric": "node_cpu_seconds_total",
                "type": "counter",
                "label": "CPU 利用率",
            }],
            impact_policy={
                "users_per_request": 0,
                "cost_per_minute": 0,
                "currency": "CNY",
                "note": "未配置财务口径，报告只展示请求/Target 级影响面",
            },
            actor=actor,
        )
        self.upsert_binding(
            "ecs-copilot",
            "loki",
            datasource_name="ecs-loki",
            mode="real",
            selector='job="journal",unit="copilot.service"',
            actor=actor,
        )
        self.upsert_binding(
            "ecs-copilot",
            "cmdb",
            datasource_name="ecs-cmdb",
            mode="real",
            actor=actor,
        )
        self.upsert_binding(
            "ecs-copilot",
            "deploy",
            datasource_name="ecs-deploy",
            mode="real",
            actor=actor,
        )
        self.upsert_service(
            "order-service",
            display_name="订单服务（演示）",
            owner="交易平台",
            tier="P1",
            description="演示服务：全部证据来自 devstack，仅用于演练，不得作为真实结论",
            actor=actor,
        )
        demos = {
            "prometheus": {"selector": 'service="order-service"',
                           "metric": "latency_p99_ms", "secondary_metric": "qps"},
            "loki": {"selector": 'service="order-service"'},
            "cmdb": {},
            "deploy": {},
            "kb": {},
        }
        for kind, extra in demos.items():
            self.upsert_binding(
                "order-service",
                kind,
                datasource_name="devstack",
                mode="mock",
                actor=actor,
                **extra,
            )

    def stats(self) -> dict:
        def count(table: str) -> int:
            row = self._one(f"SELECT COUNT(*) AS n FROM {table}")
            return int(row["n"]) if row else 0

        return {
            "services": count("service_catalog"),
            "bindings": count("service_binding"),
            "datasources": count("datasource"),
        }
