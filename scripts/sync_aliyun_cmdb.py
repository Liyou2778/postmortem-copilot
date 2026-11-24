"""Sync Aliyun inventory/ActionTrail changes into the enterprise CMDB.

Usage:
    python scripts/sync_aliyun_cmdb.py --dry-run
    python scripts/sync_aliyun_cmdb.py --apply --changes --hours 2
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "core"))
sys.path.insert(0, str(ROOT))

from app.catalog import CatalogStore  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.db import from_settings  # noqa: E402
from services.cmdb.aliyun import (  # noqa: E402
    AliyunDiscovery, AliyunDiscoveryError, sync_changes, sync_inventory,
)
from services.cmdb.graph import GraphStore  # noqa: E402
from services.cmdb.store import CmdbStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="write to the CMDB; without this it is a dry run")
    parser.add_argument("--changes", action="store_true",
                        help="also import ActionTrail write events")
    parser.add_argument("--hours", type=int, default=2)
    parser.add_argument("--region", default="")
    parser.add_argument("--role-name", default="")
    parser.add_argument("--default-service", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    db = from_settings(settings)
    legacy = CmdbStore(db)
    graph = GraphStore(db)
    graph.seed_from_legacy(legacy)
    discovery = AliyunDiscovery(
        region=args.region or None, role_name=args.role_name or None,
        default_service=args.default_service or None,
    )
    dry_run = not args.apply
    result = {"dry_run": dry_run, "region": discovery.region}
    try:
        result["inventory"] = sync_inventory(graph, discovery, dry_run=dry_run)
        if args.changes:
            result["changes"] = sync_changes(
                legacy, discovery, graph=graph, hours=args.hours, dry_run=dry_run
            )
    except AliyunDiscoveryError as exc:
        result["error"] = str(exc)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        inv = result["inventory"]
        print(f"region={discovery.region} resources={inv['resources']} "
              f"created={inv['created']} updated={inv['updated']} "
              f"relations={inv['relations']} unowned={inv['unowned']} "
              f"dry_run={dry_run}")
        for error in inv["errors"]:
            print("WARN", error)
        if "changes" in result:
            print(f"actiontrail changes={result['changes']['changes']} "
                  f"written={result['changes']['written']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
