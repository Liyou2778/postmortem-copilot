"""Idempotent M10.1 migration: add the metric pack to ecs-copilot.

The column additions are handled by CatalogStore at startup. This script only
backfills the existing binding (seed_defaults is a no-op on a populated
catalog) and leaves all other operator edits untouched.
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
        print("ecs-copilot/prometheus binding not found; nothing to do")
        return 0

    extras = binding.get("extra_metrics") or []
    has_disk = any(item.get("metric") == "node_disk_io_time_seconds_total"
                   for item in extras)
    if not has_disk:
        extras.append({
            "metric": "node_disk_io_time_seconds_total",
            "type": "counter",
            "label": "磁盘 IO 时间占比",
        })
        store.upsert_binding(
            "ecs-copilot",
            "prometheus",
            datasource_name=binding["datasource_name"],
            mode=binding["mode"],
            selector=binding.get("selector") or "",
            metric=binding.get("metric") or "node_load1",
            secondary_metric=binding.get("secondary_metric") or "",
            metric_type=binding.get("metric_type") or "gauge",
            secondary_metric_type=binding.get("secondary_metric_type") or "gauge",
            extra_metrics=extras,
            actor="migration:m10.1",
        )
        print("added node_disk_io_time_seconds_total (counter) to ecs-copilot")
    else:
        print("metric pack already contains the disk IO counter")

    updated = store.resolve("ecs-copilot", "prometheus")
    print("binding:", {
        "metric": updated.get("metric"),
        "metric_type": updated.get("metric_type"),
        "secondary_metric": updated.get("secondary_metric"),
        "secondary_metric_type": updated.get("secondary_metric_type"),
        "extra_metrics": updated.get("extra_metrics"),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
