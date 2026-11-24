r"""Kill drill: prove a task survives a worker being killed mid-step.

Scenario
--------
1. create a task and dispatch 3 analysable steps
2. start worker A with a long step delay
3. kill -9 worker A while it holds a lease
4. run the lease reaper  -> the step must be requeued, not lost
5. start worker B        -> it takes over and finishes
6. assemble              -> a report is produced

Run: .\.venv\Scripts\python.exe scripts\kill_drill.py
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "core"))

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    pass

from app.config import load_settings  # noqa: E402
from app.lease import LeaseManager  # noqa: E402
from app.pipeline import assemble, dispatch  # noqa: E402
from app.queue import SqliteQueue  # noqa: E402
from app.reaper import LeaseReaper  # noqa: E402
from app.saga import SagaRegistry  # noqa: E402
from app.service import TaskService  # noqa: E402
from app.step_store import StepOutputStore  # noqa: E402
from app.store import EventStore  # noqa: E402

DB = "drill.db"
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
PLAN = [
    {"step_id": "telemetry", "role": "telemetry"},
    {"step_id": "log", "role": "log"},
    {"step_id": "change", "role": "change"},
]


def q(sql: str, args: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(DB, timeout=10)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def banner(text: str) -> None:
    print(f"\n=== {text} ===")


def main() -> int:
    for suffix in ("", "-wal", "-shm"):
        Path(DB + suffix).unlink(missing_ok=True)

    settings = load_settings()
    store = EventStore(DB)
    leases = LeaseManager(store._conn, store._lock)
    saga = SagaRegistry(store._conn, store._lock)
    service = TaskService(store, leases, saga)
    queue = SqliteQueue(store._conn, store._lock, visibility_timeout_s=3.0)
    step_store = StepOutputStore(store._conn, store._lock)
    reaper = LeaseReaper(service, leases)

    banner("1) 建任务并派发")
    task_id = service.create_task("订单服务支付超时复盘（演练）")
    service.plan(task_id, PLAN, actor="drill")
    service.start(task_id, actor="drill")
    enqueued = dispatch(service, queue, task_id)
    print(f"  task={task_id[:8]}  enqueued={enqueued}  queue={queue.stats()}")

    banner("2) 启动 worker A（每步 15s，便于中途击杀）")
    env_a = {**os.environ, "DRILL_STEP_DELAY_S": "15", "TOOL_BACKEND": settings.tool_backend}
    worker_a = subprocess.Popen(
        [PY, "scripts/run_worker.py", "--db", DB, "--worker-id", "A",
         "--heartbeat-s", "1"],
        cwd=ROOT, env=env_a, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    print(f"  worker A pid={worker_a.pid}")

    # wait until A holds a lease (i.e. it is genuinely mid-step)
    held = False
    for _ in range(120):
        rows = q("SELECT step_id, owner, fence FROM leases WHERE owner != ''")
        if rows:
            held = True
            print(f"  A 已持有租约: {rows[0]}")
            break
        time.sleep(0.1)
    if not held:
        print("  !! 未观察到租约，演练中止")
        worker_a.kill()
        return 1

    banner("3) kill -9 worker A（模拟进程被强杀）")
    worker_a.kill()
    worker_a.wait(timeout=10)
    print(f"  A 已死亡，退出码={worker_a.returncode}")

    banner("4) 等待租约过期并运行回收器")
    # worker uses a 3s lease TTL and a 1s heartbeat; once the process is dead the
    # heartbeat stops and the lease lapses within ~3s.
    time.sleep(5.0)
    results = reaper.reap()
    for r in results:
        print(f"  {r.step_id}: {r.action} ({r.detail})")
    if not results:
        print("  !! 回收器没有扫到任何过期租约")

    banner("5) 启动 worker B 接管")
    env_b = {**os.environ, "DRILL_STEP_DELAY_S": "0", "TOOL_BACKEND": settings.tool_backend}
    proc_b = subprocess.run(
        [PY, "scripts/run_worker.py", "--db", DB, "--worker-id", "B", "--idle-exit", "4"],
        cwd=ROOT, env=env_b, capture_output=True, text=True, timeout=300,
    )
    tail = [ln for ln in (proc_b.stdout or "").splitlines() if "processed" in ln or "handled" in ln]
    for line in tail[-6:]:
        print("  " + line)

    banner("6) 装配报告")
    report = assemble(service, step_store, task_id, settings=settings)
    view = service.view(task_id)
    print(f"  任务状态 : {view.status.value}")
    for sid, st in view.steps.items():
        print(f"    {sid:10} {st['status']}")
    print(f"  证据/结论: {len(report.evidence)} / {len(report.claims)}")
    for c in report.claims:
        print(f"    rank{c.root_cause_rank} score={report.claim_scores.get(c.claim_id, 0):.3f} "
              f"{c.statement[:56]}")

    banner("7) 判定")
    checks = {
        "3 个步骤全部成功": all(s["status"] == "SUCCEEDED" for s in view.steps.values()),
        "产出至少 1 条结论": len(report.claims) > 0,
        "回收器确实处理了过期租约": len(results) >= 1,
        "无重复队列项": q("SELECT COUNT(*) FROM work_queue")[0][0] == 3,
    }
    for name, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    store.close()
    return 0 if all(checks.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
