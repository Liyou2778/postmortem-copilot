"""Remove account-level ActionTrail bootstrap events from the service feed.

Before the resource-scoping filter, CreateTrail/StartLogging could be imported as
if they were ecs-copilot changes. This is a one-off operator cleanup; the sync
now skips events that are not attached to a registered CI.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "core"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_settings  # noqa: E402
from app.db import from_settings  # noqa: E402
from services.cmdb.store import CmdbStore  # noqa: E402

GLOBAL_MARKERS = ("CreateTrail", "StartLogging", "CreateServiceLinkedRole")


def main() -> int:
    settings = load_settings()
    store = CmdbStore(from_settings(settings))
    removed = 0
    for change in store.changes("ecs-copilot", "720h"):
        if change["change_id"].startswith("AT-") and any(
            marker in change["summary"] for marker in GLOBAL_MARKERS
        ):
            if store.delete_change(change["change_id"]):
                removed += 1
                print("removed", change["change_id"], change["summary"][:80])
    print("removed", removed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
