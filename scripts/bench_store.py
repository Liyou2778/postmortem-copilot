r"""Storage-layer benchmark: SQLite (WAL file) vs MySQL (RDS over VPC).

Why a separate micro-benchmark: HTTP load tests mix database cost with
framework overhead, network RTT to the client, and LLM latency. To answer
"what does the database switch actually cost us", the operations must be
measured on their own, with the same code path and the same data shapes.

This runs both backends **in one process** against the same operations, so
machine, kernel and Python version are held constant. It never touches the
production database's live data: MySQL rows are written under a benchmark
task-id prefix and deleted afterwards; SQLite uses a throwaway file.

Usage (on the ECS, so MySQL traffic is over the internal endpoint):
    python scripts/bench_store.py --rounds 3 --events 200
"""
from __future__ import annotations

import argparse
import pathlib
import statistics
import sys
import time
import uuid

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    pass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "services" / "core"))

from app.db import MYSQL, Database, ensure_schema  # noqa: E402
from app.events import EventType  # noqa: E402
from app.store import EventStore  # noqa: E402

TASK_PREFIX = "bench-"


def load_env(path: str = "/opt/copilot/.env") -> dict[str, str]:
    env: dict[str, str] = {}
    p = pathlib.Path(path)
    if not p.exists():
        return env
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        env[k.strip()] = v
    return env


def stats(samples_ms: list[float]) -> dict[str, float]:
    ordered = sorted(samples_ms)
    def pct(p: float) -> float:
        return ordered[min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))]
    return {
        "mean": statistics.fmean(samples_ms),
        "p50": pct(50),
        "p95": pct(95),
        "p99": pct(99),
        "max": ordered[-1],
    }


def bench_append(store: EventStore, task_id: str, n: int) -> list[float]:
    """Single event append — the write that every state change goes through."""
    samples = []
    for i in range(n):
        t0 = time.perf_counter()
        store.append(task_id, EventType.STEP_QUEUED, {"i": i, "pad": "x" * 200}, "bench")
        samples.append((time.perf_counter() - t0) * 1000)
    return samples


def bench_load(store: EventStore, task_id: str, rounds: int) -> list[float]:
    """Replay a task's whole event stream."""
    samples = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        store.load(task_id)
        samples.append((time.perf_counter() - t0) * 1000)
    return samples


def bench_list_recent(store: EventStore, rounds: int) -> list[float]:
    samples = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        store.list_tasks_recent(limit=20)
        samples.append((time.perf_counter() - t0) * 1000)
    return samples


def cleanup_mysql(db: Database) -> int:
    cur = db.execute("SELECT COUNT(*) FROM events WHERE task_id LIKE ?", (TASK_PREFIX + "%",))
    n = cur.fetchone()[0]
    if n:
        db.execute("DELETE FROM events WHERE task_id LIKE ?", (TASK_PREFIX + "%",))
        db.commit()
    return n


def run_backend(name: str, store: EventStore, args, cleanup=None) -> dict:
    task_id = TASK_PREFIX + uuid.uuid4().hex[:12]

    print(f"\n=== {name} ===")
    print(f"  写入 {args.events} 条事件...", flush=True)
    append_samples = bench_append(store, task_id, args.events)

    print(f"  重放事件流 {args.rounds * 20} 次...", flush=True)
    load_samples = bench_load(store, task_id, args.rounds * 20)

    print(f"  列表查询 {args.rounds * 20} 次...", flush=True)
    list_samples = bench_list_recent(store, args.rounds * 20)

    result = {
        "append": stats(append_samples),
        "load": stats(load_samples),
        "list_recent": stats(list_samples),
        "events_written": args.events,
    }
    for op, s in result.items():
        if isinstance(s, dict):
            print(f"  {op:<12} mean={s['mean']:7.3f}ms p50={s['p50']:7.3f} "
                  f"p95={s['p95']:7.3f} p99={s['p99']:7.3f} max={s['max']:7.3f}")

    if cleanup is not None:
        removed = cleanup()
        print(f"  清理: 删除 {removed} 条基准事件")
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=int, default=200, help="每个后端写入的事件数")
    ap.add_argument("--rounds", type=int, default=3, help="负载倍数（每轮 20 次读）")
    ap.add_argument("--sqlite-path", default="/tmp/bench_store.db")
    ap.add_argument("--env", default="/opt/copilot/.env")
    # A dedicated, empty schema keeps the comparison honest: benchmarking MySQL
    # against the production database (2000+ events, ~1900 tasks) while SQLite
    # starts empty measures dataset size, not the backend.
    ap.add_argument("--mysql-db", default="", help="用独立空库做基准，例如 copilot_bench")
    ap.add_argument("--skip-mysql", action="store_true")
    args = ap.parse_args()

    env = load_env(args.env)

    # --- SQLite (throwaway file, same settings as production: WAL) ---
    sqlite_path = pathlib.Path(args.sqlite_path)
    if sqlite_path.exists():
        sqlite_path.unlink()
    sqlite_db = Database.sqlite(str(sqlite_path))
    sqlite_store = EventStore(str(sqlite_path), db=sqlite_db)
    sqlite_result = run_backend("SQLite（本地文件，WAL）", sqlite_store, args)
    sqlite_db.close()
    size_kb = sqlite_path.stat().st_size / 1024 if sqlite_path.exists() else 0
    print(f"  SQLite 文件大小: {size_kb:.1f} KB")

    mysql_result = None
    if not args.skip_mysql:
        if not env.get("DB_HOST"):
            print("\n跳过 MySQL：.env 里没有 DB_HOST")
            return 1
        mysql_db = Database.mysql(
            host=env["DB_HOST"], port=int(env.get("DB_PORT", "3306")),
            user=env["DB_USER"], password=env["DB_PASSWORD"],
            database=args.mysql_db or env.get("DB_NAME", "copilot"),
        )
        print(f"\n  MySQL 基准库: {args.mysql_db or env.get('DB_NAME', 'copilot')}")
        ensure_schema(mysql_db)
        mysql_store = EventStore(":memory:", db=mysql_db)
        mysql_result = run_backend("MySQL（RDS，内网）", mysql_store, args,
                                   cleanup=lambda: cleanup_mysql(mysql_db))
        mysql_db.close()

    # --- comparison ---
    if mysql_result:
        print("\n=== 对比（MySQL / SQLite 倍数，>1 表示 MySQL 更慢）===")
        print(f"  {'操作':<14}{'SQLite p50':>12}{'MySQL p50':>12}{'倍数':>8}")
        for op in ("append", "load", "list_recent"):
            a = sqlite_result[op]["p50"]
            b = mysql_result[op]["p50"]
            ratio = b / a if a else float("inf")
            print(f"  {op:<14}{a:>10.3f}ms{b:>10.3f}ms{ratio:>7.1f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
