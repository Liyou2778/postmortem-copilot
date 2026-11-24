"""Idempotent M9 catalog migration: real Loki for ecs-copilot.

Existing deployments already seeded the catalog, so ``seed_defaults`` is
intentionally a no-op there. This script adds only the new datasource/binding
and leaves every operator edit untouched.

Run on the ECS after deploying M9 code:
    /opt/copilot/.venv/bin/python scripts/migrate_m9_catalog.py
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
    db = from_settings(settings)
    store = CatalogStore(db, settings, seed=False)

    if store.get_datasource("ecs-loki") is None:
        store.upsert_datasource(
            "ecs-loki",
            kind="loki",
            base_url=settings.loki_url,
            mock=False,
            description="ECS journald 日志（Promtail -> Loki 2.9.8，真实日志）",
            actor="migration:m9",
        )
        print(f"created datasource ecs-loki -> {settings.loki_url}")
    else:
        print("datasource ecs-loki already exists")

    if store.resolve("ecs-copilot", "loki") is None:
        store.upsert_binding(
            "ecs-copilot",
            "loki",
            datasource_name="ecs-loki",
            mode="real",
            selector='job="journal",unit="copilot.service"',
            actor="migration:m9",
        )
        print("created binding ecs-copilot/loki -> ecs-loki")
    else:
        print("binding ecs-copilot/loki already exists")

    print("catalog stats:", store.stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
