"""Lease reaper: recover steps whose worker stopped heartbeating.

The reaper is the only component allowed to act on an expired lease. It makes
one decision per step, and that decision is the difference between "safe to
retry" and "outcome unknown":

* no dangling invocation  -> step timed out, it can be re-queued safely
* dangling invocation     -> a tool call started but never finished, so the side
                             effect may or may not have happened. Escalate to
                             UNKNOWN for a human instead of retrying.

Retrying the second case is how systems create duplicate tickets, duplicate
refunds and duplicate notifications.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .lease import LeaseManager
from .service import IllegalTransition, StaleLease, StepStatus, TaskService

log = logging.getLogger(__name__)


@dataclass
class ReapResult:
    task_id: str
    step_id: str
    action: str          # "requeued" | "unknown" | "released" | "skipped"
    detail: str = ""


class LeaseReaper:
    def __init__(self, service: TaskService, leases: LeaseManager) -> None:
        self.service = service
        self.leases = leases

    def reap(self, actor: str = "reaper") -> list[ReapResult]:
        results: list[ReapResult] = []
        expired = self.leases.expired_leases()

        for task_id, step_id, owner, fence in expired:
            try:
                view = self.service.view(task_id)
            except Exception:  # noqa: BLE001 - orphan lease row
                self.leases.force_release(task_id, step_id)
                results.append(ReapResult(task_id, step_id, "released", "task not found"))
                continue

            try:
                status = view.step_status(step_id)
            except KeyError:
                self.leases.force_release(task_id, step_id)
                results.append(ReapResult(task_id, step_id, "released", "step not in plan"))
                continue

            if status not in (StepStatus.DISPATCHED, StepStatus.RUNNING):
                # The step already reached a terminal state; only the lease row
                # is stale. Drop it without touching the journal.
                self.leases.force_release(task_id, step_id)
                results.append(
                    ReapResult(task_id, step_id, "released", f"status={status.value}")
                )
                continue

            dangling = step_id in self.service.dangling_steps(task_id)
            try:
                if dangling:
                    self.service.mark_unknown(
                        task_id, step_id,
                        f"lease expired on {owner} with a dangling tool invocation",
                        actor,
                    )
                    results.append(
                        ReapResult(task_id, step_id, "unknown",
                                   "side effect indeterminate, escalated")
                    )
                else:
                    self.service.timeout_step(
                        task_id, step_id, f"lease expired on {owner}", actor
                    )
                    results.append(ReapResult(task_id, step_id, "requeued", "lease expired"))
            except (IllegalTransition, StaleLease) as exc:
                results.append(ReapResult(task_id, step_id, "skipped", str(exc)[:160]))
            finally:
                self.leases.force_release(task_id, step_id)

        if results:
            log.info("reaper swept %d expired leases: %s", len(results),
                     ", ".join(f"{r.step_id}={r.action}" for r in results))
        return results

    def sweep_until_quiet(self, max_rounds: int = 5) -> list[ReapResult]:
        """Reap repeatedly; a takeover can create a new lease that also lapses."""
        all_results: list[ReapResult] = []
        for _ in range(max_rounds):
            batch = self.reap()
            if not batch:
                break
            all_results.extend(batch)
        return all_results
