"""Idempotent M10.2 migration: bind ecs-copilot to the self-hosted CMDB/deploy.

Run on the ECS after deploying the CMDB service and setting:
    CMDB_URL=http://127.0.0.1:9200
    DEPLOY_URL=http://127.0.0.1:9200
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "core"))

from app.catalog import CatalogStore  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.db import from_settings  # noqa: E402


def main() -> int:
    settings = load_settings()
    store = CatalogStore(from_settings(settings), settings, seed=False)

    for name, kind, url, desc in (
        ("ecs-cmdb", "cmdb", settings.cmdb_url, "平台自带轻量 CMDB"),
        ("ecs-deploy", "deploy", settings.deploy_url, "平台自带发布/变更登记"),
    ):
        if store.get_datasource(name) is None:
            store.upsert_datasource(name, kind=kind, base_url=url, mock=False,
                                    description=desc, actor="migration:m10.2")
            print(f"created datasource {name} -> {url}")
        else:
            print(f"datasource {name} already exists")

    for kind, datasource in (("cmdb", "ecs-cmdb"), ("deploy", "ecs-deploy")):
        if store.resolve("ecs-copilot", kind) is None:
            store.upsert_binding(
                "ecs-copilot", kind, datasource_name=datasource,
                mode="real", actor="migration:m10.2",
            )
            print(f"created binding ecs-copilot/{kind} -> {datasource}")
        else:
            print(f"binding ecs-copilot/{kind} already exists")

    print("catalog stats:", store.stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
