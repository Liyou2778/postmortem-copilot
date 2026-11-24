"""Read-only Aliyun discovery for the enterprise CMDB.

Credential model: an ECS instance RAM role (`ecs_ram_role`), so no AccessKey is
stored on disk. The adapter only calls read APIs (Resource Center,
ActionTrail, Config); it never mutates cloud resources.

Discovery alone does not make a service topology. A resource is attached to a
service only when it has a `service` tag (or, for the ECS host running the
platform, the configured default service). Untagged resources are still
registered as inventory with `unowned=true` so data quality is visible instead
of silently inventing an owner.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from app.config import get  # noqa: E402

RESOURCE_TYPES: dict[str, tuple[str, str]] = {
    "ACS::ECS::Instance": ("host", "runs_on"),
    "ACS::RDS::DBInstance": ("database", "depends_on"),
    "ACS::OSS::Bucket": ("storage", "stores_in"),
    "ACS::SLB::LoadBalancer": ("external", "depends_on"),
    "ACS::Redis::DBInstance": ("middleware", "depends_on"),
}

_WRITE_PREFIXES = (
    "Create", "Update", "Modify", "Delete", "Release", "Stop", "Start",
    "Reboot", "Attach", "Detach", "Enable", "Disable", "Reset", "Set",
    "Allocate", "Associate", "Dissociate", "Upgrade", "Downgrade",
    "Tag", "Untag",
)


class AliyunDiscoveryError(RuntimeError):
    pass


@dataclass
class CloudResource:
    resource_id: str
    resource_type: str
    region_id: str
    resource_name: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    account_id: str = ""

    @property
    def service_tag(self) -> str:
        for key in ("service", "Service", "SERVICE"):
            if self.tags.get(key):
                return self.tags[key]
        return ""


class AliyunDiscovery:
    def __init__(self, *, region: str | None = None, role_name: str | None = None,
                 default_service: str | None = None) -> None:
        self.region = region or get("ALIYUN_REGION", "cn-hangzhou")
        self.role_name = role_name or get("CMDB_RAM_ROLE_NAME", "copilot-cmdb-reader")
        self.default_service = default_service or get(
            "CMDB_DEFAULT_SERVICE", "ecs-copilot"
        )
        self._clients: dict[str, Any] = {}

    # ------------------------------------------------------------- credentials
    def _credential(self):
        try:
            from alibabacloud_credentials.client import Client
            from alibabacloud_credentials.models import Config
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise AliyunDiscoveryError(
                "alibabacloud_credentials is not installed"
            ) from exc
        mode = get("CMDB_CREDENTIAL_MODE", "instance_role")
        if mode == "access_key":
            ak = get("CMDB_ACCESS_KEY_ID", "")
            secret = get("CMDB_ACCESS_KEY_SECRET", "")
            if not ak or not secret:
                raise AliyunDiscoveryError("CMDB access key is not configured")
            return Client(Config(type="access_key", access_key_id=ak,
                                 access_key_secret=secret))
        return Client(Config(type="ecs_ram_role", role_name=self.role_name))

    def _client(self, product: str):
        if product in self._clients:
            return self._clients[product]
        try:
            if product == "resourcecenter":
                from alibabacloud_resourcecenter20221201.client import Client
                endpoint = "resourcecenter.aliyuncs.com"
            elif product == "actiontrail":
                from alibabacloud_actiontrail20200706.client import Client
                endpoint = f"actiontrail.{self.region}.aliyuncs.com"
            elif product == "config":
                from alibabacloud_config20200907.client import Client
                endpoint = f"config.{self.region}.aliyuncs.com"
            else:
                raise AliyunDiscoveryError(f"unknown product {product}")
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise AliyunDiscoveryError(f"{product} SDK is not installed") from exc
        from alibabacloud_tea_openapi import models as open_api_models

        config = open_api_models.Config(
            credential=self._credential(), region_id=self.region, endpoint=endpoint,
        )
        client = Client(config)
        self._clients[product] = client
        return client

    # --------------------------------------------------------------- inventory
    @staticmethod
    def _tags(resource: Any) -> dict[str, str]:
        tags: dict[str, str] = {}
        for tag in getattr(resource, "tags", None) or []:
            key = getattr(tag, "key", None) or getattr(tag, "tag_key", None)
            value = getattr(tag, "value", None) or getattr(tag, "tag_value", None)
            if key:
                tags[str(key)] = str(value or "")
        return tags

    def search_resources(self, resource_type: str, max_results: int = 100) -> list[CloudResource]:
        from alibabacloud_resourcecenter20221201 import models

        client = self._client("resourcecenter")
        filters = [
            models.SearchResourcesRequestFilter(
                key="ResourceType", value=[resource_type]
            ),
            models.SearchResourcesRequestFilter(
                key="RegionId", value=[self.region]
            ),
        ]
        found: list[CloudResource] = []
        next_token = ""
        for _ in range(10):
            request = models.SearchResourcesRequest(
                filter=filters, max_results=min(max_results, 100),
                next_token=next_token or None,
            )
            response = client.search_resources(request)
            body = response.body
            for resource in getattr(body, "resources", None) or []:
                found.append(CloudResource(
                    resource_id=str(getattr(resource, "resource_id", "") or ""),
                    resource_type=str(getattr(resource, "resource_type", resource_type)),
                    region_id=str(getattr(resource, "region_id", self.region)),
                    resource_name=str(getattr(resource, "resource_name", "") or ""),
                    tags=self._tags(resource),
                    account_id=str(getattr(resource, "account_id", "") or ""),
                ))
            next_token = str(getattr(body, "next_token", "") or "")
            if not next_token:
                break
        return found

    def inventory(self) -> dict[str, Any]:
        resources: list[CloudResource] = []
        errors: list[str] = []
        for resource_type in RESOURCE_TYPES:
            try:
                resources.extend(self.search_resources(resource_type))
            except Exception as exc:  # noqa: BLE001 - one type must not kill discovery
                errors.append(f"{resource_type}: {type(exc).__name__}: {exc}"[:300])
        return {"resources": resources, "errors": errors, "region": self.region}

    # ---------------------------------------------------------------- changes
    def recent_changes(self, hours: int = 2, max_results: int = 50) -> list[dict[str, Any]]:
        from alibabacloud_actiontrail20200706 import models

        client = self._client("actiontrail")
        now = datetime.now(timezone.utc)
        request = models.LookupEventsRequest(
            start_time=(now - timedelta(hours=max(1, int(hours)))).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            end_time=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            max_results=str(max(1, min(int(max_results), 50))),
        )
        response = client.lookup_events(request)
        changes: list[dict[str, Any]] = []
        for event in getattr(response.body, "events", None) or []:
            if not isinstance(event, dict):
                event = event.to_map() if hasattr(event, "to_map") else {}
            name = str(event.get("eventName") or "")
            if not name or not name.startswith(_WRITE_PREFIXES):
                continue
            user = (event.get("userIdentity") or {}).get("userName") or "unknown"
            resources = event.get("referencedResources") or []
            resource_name = ""
            if isinstance(resources, dict):
                resource_name = str(
                    resources.get("ResourceName")
                    or resources.get("resourceName") or ""
                )
            elif isinstance(resources, list) and resources:
                first = resources[0]
                if isinstance(first, dict):
                    resource_name = str(
                        first.get("ResourceName") or first.get("resourceName") or ""
                    )
            changes.append({
                "event_id": str(event.get("eventId") or ""),
                "event_name": name,
                "event_time": str(event.get("eventTime") or ""),
                "service": str(event.get("serviceName") or ""),
                "resource_name": resource_name,
                "operator": str(user),
                "region": str(event.get("acsRegion") or self.region),
            })
        return changes


def _logical_ci(graph: Any, ci_type: str) -> str:
    """Find the single manually-maintained logical CI for a resource type.

    Discovery creates physical CIs (`aliyun-database-...`); the catalog already
    has logical CIs (`rds-mysql`, `oss`). Link them instead of duplicating the
    service dependency. If there is ambiguity, return "" and let sync attach the
    resource directly; data quality can then flag it for explicit mapping.
    """
    if ci_type not in ("database", "storage", "middleware"):
        return ""
    candidates = [
        ci for ci in graph.list_cis(ci_type)
        if not str(ci["ci_id"]).startswith("aliyun-")
    ]
    return candidates[0]["ci_id"] if len(candidates) == 1 else ""


def _same_ci(existing: dict | None, **desired: Any) -> bool:
    if not existing:
        return False
    for key in ("name", "ci_type", "owner", "tier", "env", "status"):
        if str(existing.get(key) or "") != str(desired.get(key) or ""):
            return False
    return (existing.get("attributes") or {}) == (desired.get("attributes") or {})


def sync_inventory(graph: Any, discovery: AliyunDiscovery, *, dry_run: bool = False) -> dict:
    service = discovery.default_service
    if not graph.get_ci(service):
        raise AliyunDiscoveryError(f"default service CI {service} not found")
    data = discovery.inventory()
    created = updated = relations = unowned = unattached = 0
    errors = list(data["errors"])
    for resource in data["resources"]:
        ci_type, relation_type = RESOURCE_TYPES.get(
            resource.resource_type, ("external", "depends_on")
        )
        ci_id = f"aliyun-{ci_type}-{resource.resource_id}".lower()
        tags = resource.tags
        owner_tag = tags.get("owner") or tags.get("Owner") or ""
        owner = owner_tag or "unowned"
        tier = tags.get("tier") or tags.get("Tier") or "P2"
        env = tags.get("env") or tags.get("Env") or "prod"
        target_service = resource.service_tag or service
        is_default_host = (
            resource.resource_type == "ACS::ECS::Instance"
            and resource.resource_id == get("CMDB_DEFAULT_HOST_ID", "")
        )
        attach = bool(resource.service_tag) or is_default_host
        attributes = {
            "provider": "aliyun",
            "resource_id": resource.resource_id,
            "resource_type": resource.resource_type,
            "region_id": resource.region_id,
            "tags": tags,
            "unowned": not bool(owner_tag),
            "unattached": not bool(resource.service_tag),
        }
        desired = {
            "name": resource.resource_name or resource.resource_id,
            "ci_type": ci_type, "owner": owner, "tier": tier, "env": env,
            "status": "active", "attributes": attributes,
        }
        existing = graph.get_ci(ci_id)
        if not _same_ci(existing, **desired):
            if not dry_run:
                graph.upsert_ci(
                    ci_id, **desired, source="aliyun", actor="aliyun-sync",
                    role="editor",
                )
            if existing:
                updated += 1
            else:
                created += 1
        if not attach:
            if not owner_tag:
                unowned += 1
            else:
                unattached += 1
            continue
        if not graph.get_ci(target_service):
            errors.append(f"service tag {target_service} has no CI")
            continue

        logical = _logical_ci(graph, ci_type)
        criticality = "high" if ci_type in ("database", "middleware") else "medium"
        if logical:
            # Logical dependency -> physical resource. The service keeps its
            # stable business edge (rds-mysql/oss); the cloud resource is the
            # implementation detail underneath it.
            logical_relations = graph.relations(logical, "downstream")
            if not any(
                rel["to_ci"] == ci_id and rel["relation_type"] == "realized_by"
                for rel in logical_relations
            ):
                if not dry_run:
                    graph.upsert_relation(
                        logical, ci_id, relation_type="realized_by",
                        criticality=criticality, source="aliyun",
                        attributes={"unowned": not bool(resource.service_tag)},
                        actor="aliyun-sync", role="editor",
                    )
                relations += 1
            # Remove a stale direct service -> resource edge created by an
            # earlier sync before logical mapping existed.
            for rel in graph.relations(target_service, "downstream"):
                if rel["to_ci"] != ci_id:
                    continue
                if not dry_run:
                    graph.delete_relation(
                        target_service, ci_id, rel["relation_type"],
                        actor="aliyun-sync", role="editor",
                    )
                else:
                    relations -= 1
            continue

        relation_status = graph.relations(target_service, "downstream")
        relation_exists = any(
            rel["from_ci"] == target_service and rel["to_ci"] == ci_id
            and rel["relation_type"] == relation_type
            for rel in relation_status
        )
        if not relation_exists:
            if not dry_run:
                graph.upsert_relation(
                    target_service, ci_id, relation_type=relation_type,
                    criticality=criticality,
                    source="aliyun",
                    attributes={"unowned": not bool(resource.service_tag)},
                    actor="aliyun-sync", role="editor",
                )
            relations += 1
    return {
        "region": discovery.region, "resources": len(data["resources"]),
        "created": created, "updated": updated, "relations": relations,
        "unowned": unowned, "unattached": unattached,
        "errors": errors, "dry_run": dry_run,
    }


def sync_changes(legacy: Any, discovery: AliyunDiscovery, *, graph: Any | None = None,
                 hours: int = 2, dry_run: bool = False) -> dict:
    service = discovery.default_service
    if not legacy.get_service(service):
        raise AliyunDiscoveryError(f"default service {service} not found")
    changes = discovery.recent_changes(hours=hours)

    # Account-level events (CreateTrail, StartLogging, ...) are not service
    # changes. Only attach an event when ActionTrail names a resource that is
    # already registered as a CI, so the E-CHG evidence stays service-scoped.
    known_resources: set[str] = set()
    if graph is not None:
        for ci in graph.list_cis():
            attrs = ci.get("attributes") or {}
            for key in ("resource_id", "resource_name"):
                value = str(attrs.get(key) or "").strip().lower()
                if value:
                    known_resources.add(value)
            known_resources.add(str(ci.get("name") or "").strip().lower())

    written = skipped = 0
    for change in changes:
        resource_name = str(change.get("resource_name") or "").strip()
        if graph is not None:
            if not resource_name or resource_name.lower() not in known_resources:
                skipped += 1
                continue
        summary = (
            f"{change['event_name']} {resource_name} "
            f"({change['service']})"
        ).strip()
        if not dry_run:
            legacy.add_change(
                service, summary[:512], kind="cloud_change",
                operator=change["operator"], change_id=f"AT-{change['event_id']}"[:64],
                at=change["event_time"],
            )
        written += 1
    return {"changes": len(changes), "written": written, "skipped": skipped,
            "dry_run": dry_run}


__all__ = [
    "AliyunDiscovery", "AliyunDiscoveryError", "CloudResource",
    "RESOURCE_TYPES", "sync_inventory", "sync_changes",
]
