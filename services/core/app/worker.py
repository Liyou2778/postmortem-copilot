"""Standalone step worker.

Pulls one step at a time from the queue, holds a lease while it runs, and keeps
the lease alive with a heartbeat. If the process dies the heartbeat stops, the
lease lapses, and the reaper requeues the step — that is the whole recovery
story, and it is why the lease (not a DB status flag) is the source of truth
while a step is running.

Failure routing:

* ``NeedsHumanDecision`` -> the step is marked UNKNOWN and NOT requeued; a
  retry could duplicate a side effect.
* any other exception      -> the step is failed and the item is nacked, so it
  is redelivered (at-least-once) and the step-level idempotency key protects
  the side effect.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from .agents import AgentContext, ChangeAgent, LogAgent, TelemetryAgent
from .config import Settings, load_settings
from .contracts import AgentOutput
from .lease import LeaseManager
from .llm import ModelClient
from .queue import WorkItem, WorkQueue
from .service import IllegalTransition, NeedsHumanDecision, StaleLease, TaskService
from .step_store import StepOutputStore
from .tools import build_default_registry

log = logging.getLogger(__name__)

AGENTS = {
    "telemetry": TelemetryAgent,
    "log": LogAgent,
    "change": ChangeAgent,
}


@dataclass
class WorkerStats:
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    unknown: int = 0


class StepWorker:
    def __init__(
        self,
        service: TaskService,
        queue: WorkQueue,
        step_store: StepOutputStore,
        llm: ModelClient | None = None,
        settings: Settings | None = None,
        worker_id: str | None = None,
        lease_ttl_ms: int = 30_000,
        heartbeat_s: float = 5.0,
        catalog: Any | None = None,
    ) -> None:
        self.service = service
        self.queue = queue
        self.step_store = step_store
        self.settings = settings or load_settings()
        self.catalog = catalog
        self.llm = llm or ModelClient()
        self.worker_id = worker_id or f"worker-{int(time.time())}"
        self.lease_ttl_ms = lease_ttl_ms
        self.heartbeat_s = heartbeat_s
        self.stats = WorkerStats()
        self._tools = None

    # ------------------------------------------------------------------ tools
    def _tool_backend(self):
        if self._tools is None:
            if self.settings.tool_backend == "mcp":
                from .mcp_gateway import MCPGateway

                gw = MCPGateway(
                    timeout=max(30.0, self.settings.tool_timeout_s * 3),
                    settings=self.settings,
                )
                gw.start()
                if self.catalog is not None:
                    from .catalog_router import CatalogRouter

                    self._tools = CatalogRouter(gw, self.catalog, self.settings)
                else:
                    self._tools = gw
            else:
                self._tools = build_default_registry(self.settings)
        return self._tools

    def close(self) -> None:
        if self._tools is not None and hasattr(self._tools, "close"):
            try:
                self._tools.close()
            except Exception:  # noqa: BLE001
                pass

    # -------------------------------------------------------------- heartbeat
    class _Heartbeat:
        def __init__(self, leases: LeaseManager, task_id: str, step_id: str, owner: str,
                     fence: int, interval: float, ttl_ms: int) -> None:
            self._leases = leases
            self._task_id = task_id
            self._step_id = step_id
            self._owner = owner
            self._fence = fence
            self._interval = interval
            self._ttl_ms = ttl_ms
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._loop, daemon=True)

        def _loop(self) -> None:
            while not self._stop.wait(self._interval):
                ok = self._leases.renew(
                    self._task_id, self._step_id, self._owner, self._fence, self._ttl_ms
                )
                if not ok:
                    log.warning("heartbeat lost the lease for %s/%s",
                                self._task_id[:8], self._step_id)
                    return

        def __enter__(self) -> "StepWorker._Heartbeat":
            self._thread.start()
            return self

        def __exit__(self, *exc: object) -> None:
            self._stop.set()
            self._thread.join(timeout=2)

    # ---------------------------------------------------------------- process
    def process(self, item: WorkItem) -> str:
        """Execute one queued step. Returns the outcome label."""
        svc = self.service
        view = svc.view(item.task_id)
        status = view.step_status(item.step_id)
        if status.value in ("SUCCEEDED", "SKIPPED"):
            self.queue.ack(item)
            return "skipped"

        owner = f"{self.worker_id}:{item.step_id}"
        fence = svc.lease_step(item.task_id, item.step_id, owner=owner,
                               ttl_ms=self.lease_ttl_ms)
        if fence is None:
            # Someone else owns it; leave the item for the queue's own retry.
            self.queue.nack(item)
            return "contended"

        try:
            svc.begin_step(item.task_id, item.step_id, owner, fence)
        except (StaleLease, IllegalTransition) as exc:
            log.warning("cannot begin %s: %s", item.step_id, exc)
            self.queue.nack(item)
            return "rejected"

        # Drill hook: simulate a long-running step so a kill can land mid-flight.
        drill_delay = float(os.getenv("DRILL_STEP_DELAY_S", "0") or 0)
        if drill_delay > 0:
            time.sleep(drill_delay)

        agent_cls = AGENTS.get(item.step_id)
        if agent_cls is None:
            svc.succeed(item.task_id, item.step_id, fence, {"skipped": True}, owner)
            self.queue.ack(item)
            return "unsupported"

        try:
            with self._Heartbeat(svc._leases, item.task_id, item.step_id,  # noqa: SLF001
                                 owner, fence, self.heartbeat_s, self.lease_ttl_ms):
                ctx = AgentContext(
                    task_id=item.task_id,
                    service=view.title or "unknown-service",
                    window="30m",
                    tools=self._tool_backend(),
                    llm=self.llm,
                    settings=self.settings,
                    evidence=self.step_store.load_evidence(item.task_id),
                )
                output: AgentOutput = agent_cls().run(ctx)
                self.step_store.save_evidence(item.task_id, ctx.evidence)
                self.step_store.save_output(item.task_id, item.step_id, output)
            svc.succeed(item.task_id, item.step_id, fence,
                        {"claims": len(output.claims)}, owner)
            self.queue.ack(item)
            self.stats.succeeded += 1
            return "succeeded"

        except NeedsHumanDecision as exc:
            # Do NOT requeue: retrying may duplicate a side effect.
            try:
                svc.mark_unknown(item.task_id, item.step_id, str(exc), self.worker_id)
            except IllegalTransition:
                pass
            self.queue.ack(item)
            self.stats.unknown += 1
            return "unknown"

        except Exception as exc:  # noqa: BLE001 - recorded, then redelivered
            try:
                svc.fail(item.task_id, item.step_id, fence,
                         f"{type(exc).__name__}: {exc}", owner)
            except (StaleLease, IllegalTransition):
                pass
            self.queue.nack(item)
            self.stats.failed += 1
            return "failed"

        finally:
            self.stats.processed += 1

    # ------------------------------------------------------------------- loop
    def run_forever(self, stop_event: threading.Event | None = None,
                    max_items: int | None = None) -> WorkerStats:
        stop = stop_event or threading.Event()
        handled = 0
        while not stop.is_set():
            if max_items is not None and handled >= max_items:
                break
            item = self.queue.reserve(timeout_ms=800)
            if item is None:
                continue
            outcome = self.process(item)
            if outcome not in ("skipped", "contended", "rejected"):
                handled += 1
            log.info("step %s/%s -> %s", item.task_id[:8], item.step_id, outcome)
        return self.stats
