"""Idempotent M10.7 migration: add CPU utilization to the metric pack."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "core"))

from app.catalog import CatalogStore  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.db import from_settings  # noqa: E402


def main() -> int:
    store = CatalogStore(from_settings(load_settings()), load_settings(), seed=False)
    binding = store.resolve("ecs-copilot", "prometheus")
    if not binding:
        print("binding not found")
        return 0
    extras = list(binding.get("extra_metrics") or [])
    if not any(item.get("metric") == "node_cpu_seconds_total" for item in extras):
        extras.append({
            "metric": "node_cpu_seconds_total",
            "type": "counter",
            "label": "CPU 利用率",
        })
        store.upsert_binding(
            "ecs-copilot", "prometheus",
            datasource_name=binding["datasource_name"],
            mode=binding["mode"], selector=binding.get("selector") or "",
            metric=binding.get("metric") or "node_load1",
            secondary_metric=binding.get("secondary_metric") or "",
            metric_type=binding.get("metric_type") or "gauge",
            secondary_metric_type=binding.get("secondary_metric_type") or "gauge",
            extra_metrics=extras,
            impact_policy=binding.get("impact_policy") or {},
            actor="migration:m10.7",
        )
        print("added CPU metric; total extras:", len(extras))
    else:
        print("CPU metric already present:", extras)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
