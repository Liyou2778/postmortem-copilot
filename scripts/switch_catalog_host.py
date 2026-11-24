"""Swap catalog datasource hosts between loopback (systemd) and VPC (K8s).

The catalog stores absolute base URLs. Host systemd services bind loopback;
pods reach the same services through the ECS private IP. This script performs
the atomic switch and is reversible:

    python scripts/switch_catalog_host.py --host 172.24.19.160 --apply
    python scripts/switch_catalog_host.py --host 127.0.0.1 --apply
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "core"))

from app.catalog import CatalogStore  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.db import from_settings  # noqa: E402


def replace_host(url: str, host: str) -> str:
    parsed = urlparse(url)
    if not parsed.hostname:
        return url
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    return urlunparse(parsed._replace(netloc=netloc))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    # Allow the target private IP during the switch even if the current .env
    # allowlist only contains loopback.
    os.environ["TOOL_URL_ALLOWLIST"] = (
        f"{args.host},localhost,127.0.0.1,prometheus,loki"
    )
    settings = load_settings()
    store = CatalogStore(from_settings(settings), settings, seed=False)
    changed = 0
    for ds in store.list_datasources():
        old = ds.get("base_url") or ""
        new = replace_host(old, args.host)
        if new == old:
            continue
        print(f"{ds['name']}: {old} -> {new}")
        if args.apply:
            store.upsert_datasource(
                ds["name"], kind=ds["kind"], base_url=new,
                mock=bool(ds.get("mock")), description=ds.get("description") or "",
                actor="switch:k8s",
            )
        changed += 1
    print(f"{'applied' if args.apply else 'dry-run'} changed={changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
