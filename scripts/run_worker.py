r"""Run a standalone step worker against a SQLite database.

Used by the kill drill and by local multi-process testing.

    python scripts/run_worker.py --db drill.db --idle-exit 3

Environment:
    DRILL_STEP_DELAY_S   artificial delay inside the step (drill only)
    TOOL_BACKEND         local | mcp
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "core"))

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    pass

from app.catalog import CatalogStore  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.lease import LeaseManager  # noqa: E402
from app.queue import SqliteQueue, build_queue  # noqa: E402
from app.saga import SagaRegistry  # noqa: E402
from app.service import TaskService  # noqa: E402
from app.step_store import StepOutputStore  # noqa: E402
from app.store import EventStore  # noqa: E402
from app.worker import StepWorker  # noqa: E402


def build(db_path: str, worker_id: str, heartbeat_s: float = 1.0):
    settings = load_settings()
    store = EventStore(db_path)
    leases = LeaseManager(store._conn, store._lock)
    saga = SagaRegistry(store._conn, store._lock)
    service = TaskService(store, leases, saga)
    if settings.queue_backend == "redis":
        queue = build_queue("redis", redis_url=settings.redis_url)
        print(f"queue backend: redis ({settings.redis_url})")
    else:
        queue = SqliteQueue(store._conn, store._lock, visibility_timeout_s=5.0)
    step_store = StepOutputStore(store._conn, store._lock)
    catalog = CatalogStore(store._conn, settings, seed=settings.catalog_seed)
    worker = StepWorker(
        service, queue, step_store,
        settings=settings,
        worker_id=worker_id,
        lease_ttl_ms=3_000,        # short on purpose so a kill is detected fast
        heartbeat_s=heartbeat_s,
        catalog=catalog,
    )
    return store, service, queue, step_store, worker


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="drill.db")
    ap.add_argument("--worker-id", default=f"w-{os.getpid()}")
    ap.add_argument("--max-items", type=int, default=0)
    ap.add_argument("--idle-exit", type=int, default=0,
                    help="exit after N consecutive empty reserves (0 = never)")
    ap.add_argument("--heartbeat-s", type=float, default=1.0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format=f"[{args.worker_id}] %(levelname)s %(message)s")
    store, _service, queue, _store2, worker = build(args.db, args.worker_id,
                                                   args.heartbeat_s)
    handled = 0
    idle = 0
    try:
        while True:
            item = queue.reserve(timeout_ms=600)
            if item is None:
                idle += 1
                if args.idle_exit and idle >= args.idle_exit:
                    break
                continue
            idle = 0
            outcome = worker.process(item)
            logging.info("processed %s/%s -> %s", item.task_id[:8], item.step_id, outcome)
            handled += 1
            if args.max_items and handled >= args.max_items:
                break
    finally:
        worker.close()
        store.close()
    print(f"{args.worker_id}: handled={handled}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
