"""Idempotent M10.3 migration: add the impact business policy to ecs-copilot.

Zero coefficients are intentional: the report must show "not configured" until
finance/owners provide a real users-per-request and cost-per-minute basis.
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
    binding = store.resolve("ecs-copilot", "prometheus")
    if not binding:
        print("ecs-copilot/prometheus binding not found")
        return 0
    policy = binding.get("impact_policy") or {}
    if not policy:
        policy = {
            "users_per_request": 0,
            "cost_per_minute": 0,
            "currency": "CNY",
            "note": "未配置财务口径，报告只展示请求/Target 级影响面",
        }
        store.upsert_binding(
            "ecs-copilot", "prometheus",
            datasource_name=binding["datasource_name"],
            mode=binding["mode"],
            selector=binding.get("selector") or "",
            metric=binding.get("metric") or "node_load1",
            secondary_metric=binding.get("secondary_metric") or "",
            metric_type=binding.get("metric_type") or "gauge",
            secondary_metric_type=binding.get("secondary_metric_type") or "gauge",
            extra_metrics=binding.get("extra_metrics") or [],
            impact_policy=policy,
            actor="migration:m10.3",
        )
        print("added impact policy (zero coefficients, explicit note)")
    else:
        print("impact policy already exists:", policy)

    # Ensure a real deployment change exists for this milestone.
    try:
        print("catalog stats:", store.stats())
    except Exception as exc:  # noqa: BLE001
        print("stats failed:", exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
