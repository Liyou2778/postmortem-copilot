r"""Copy the SQLite event log into MySQL (RDS).

Why a one-shot script and not a dual-write migration: the SQLite file is tiny
(2207 events), the service can be stopped for a minute, and a verifiable
copy-then-verify-then-switch sequence is far easier to trust than a live
dual-write scheme.

The script is deliberately boring:
  1. create the MySQL schema (idempotent DDL)
  2. copy every table in row order
  3. re-read both sides and compare counts + a checksum of the event log
  4. refuse to report success if anything mismatches

It does NOT flip the application over — that is a config change (`DB_BACKEND`)
made deliberately after verification.

Usage (on the server, after filling DB_* in .env):
    /opt/copilot/.venv/bin/python scripts/migrate_to_mysql.py --dry-run
    /opt/copilot/.venv/bin/python scripts/migrate_to_mysql.py
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = ROOT / "migrations" / "002_mysql_schema.sql"

# Insert order is irrelevant (no FKs) but kept stable for reproducible output.
TABLES = [
    "events",
    "idempotency",
    "leases",
    "compensations",
    "report",
    "step_output",
    "task_evidence",
    "archive",
]

BATCH = 500


def load_env(path: Path) -> dict[str, str]:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        # Strip matching quotes, same as app/config.py — otherwise a quoted
        # password is used verbatim and fails authentication.
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        env[k.strip()] = v
    return env


def mysql_connect(cfg: dict[str, str]):
    import pymysql

    missing = [k for k in ("DB_HOST", "DB_USER", "DB_PASSWORD") if not cfg.get(k)]
    if missing:
        raise SystemExit(f"缺少配置: {', '.join(missing)}（请在 /opt/copilot/.env 中填写）")
    return pymysql.connect(
        host=cfg["DB_HOST"],
        port=int(cfg.get("DB_PORT", "3306")),
        user=cfg["DB_USER"],
        password=cfg["DB_PASSWORD"],
        database=cfg.get("DB_NAME", "copilot"),
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=10,
    )


def create_schema(mysql, dry_run: bool) -> None:
    """Create tables. Runs even in dry-run.

    The DDL is ``CREATE TABLE IF NOT EXISTS``, so executing it is idempotent and
    harmless — and it is the only way a dry-run can actually prove the schema is
    accepted by this MySQL version. Skipping it would make the dry-run report
    success while testing nothing.
    """
    ddl = SCHEMA_FILE.read_text(encoding="utf-8")
    # Strip comments FIRST, then split on ';'. Splitting first is a trap: a
    # comment containing a semicolon ("... VARCHAR(64/191); utf8mb4 costs 4
    # bytes") gets cut in half and the tail becomes an invalid statement.
    lines = [ln for ln in ddl.splitlines() if not ln.strip().startswith("--")]
    statements = [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
    print(f"  schema: {len(statements)} 条 DDL")
    cur = mysql.cursor()
    for stmt in statements:
        cur.execute(stmt)
    mysql.commit()
    print("  ✓ 建表完成（IF NOT EXISTS，可重复执行）")


def table_columns(mysql, table: str) -> list[str]:
    cur = mysql.cursor()
    cur.execute(f"SHOW COLUMNS FROM {table}")
    return [row[0] for row in cur.fetchall()]


def copy_table(sqlite_conn, mysql, table: str, dry_run: bool) -> tuple[int, int]:
    """Copy one table. Returns (sqlite_rows, mysql_rows)."""
    cur = sqlite_conn.execute(f"SELECT COUNT(*) FROM {table}")
    src_count = cur.fetchone()[0]
    if dry_run or src_count == 0:
        return src_count, 0

    cols = table_columns(mysql, table)
    placeholders = ", ".join(["%s"] * len(cols))
    col_sql = ", ".join(f"`{c}`" for c in cols)
    insert_sql = f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})"

    cursor = sqlite_conn.execute(f"SELECT {', '.join(cols)} FROM {table}")
    rows = cursor.fetchall()

    mcur = mysql.cursor()
    # REPLACE so a re-run is idempotent instead of blowing up on duplicate keys.
    mcur.execute(f"DELETE FROM {table}")
    for i in range(0, len(rows), BATCH):
        mcur.executemany(insert_sql.replace("INSERT INTO", "REPLACE INTO"), rows[i:i + BATCH])
    mysql.commit()

    mcur.execute(f"SELECT COUNT(*) FROM {table}")
    dst_count = mcur.fetchone()[0]
    return src_count, dst_count


def event_checksum(sqlite_conn, mysql) -> tuple[str, str]:
    """Order-independent checksum over (task_id, seq, type, payload) of events."""

    def digest(rows) -> str:
        h = hashlib.sha256()
        for row in sorted(rows):
            h.update("\x1f".join("" if v is None else str(v) for v in row).encode("utf-8"))
            h.update(b"\x1e")
        return h.hexdigest()

    src = sqlite_conn.execute(
        "SELECT task_id, seq, type, payload FROM events ORDER BY task_id, seq"
    ).fetchall()
    cur = mysql.cursor()
    cur.execute("SELECT task_id, seq, type, payload FROM events ORDER BY task_id, seq")
    dst = cur.fetchall()
    return digest(src), digest(dst)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.getenv("CORE_DB_PATH", "/opt/copilot/data/core.db"))
    ap.add_argument("--env", default="/opt/copilot/.env")
    ap.add_argument("--dry-run", action="store_true", help="只建表与统计，不写入数据")
    args = ap.parse_args()

    cfg = load_env(Path(args.env))
    cfg.update({k: v for k, v in os.environ.items() if k.startswith("DB_")})

    print(f"源库   : {args.db}")
    print(f"目标   : {cfg.get('DB_USER')}@{cfg.get('DB_HOST')}:{cfg.get('DB_PORT','3306')}/{cfg.get('DB_NAME','copilot')}")
    print(f"dry-run: {args.dry_run}")
    print()

    sqlite_conn = sqlite3.connect(args.db)

    print("== 1/4 建表 ==")
    mysql = mysql_connect(cfg)
    try:
        create_schema(mysql, args.dry_run)

        print("== 2/4 复制数据 ==")
        results = {}
        for table in TABLES:
            try:
                src, dst = copy_table(sqlite_conn, mysql, table, args.dry_run)
            except Exception as exc:  # noqa: BLE001
                print(f"  ✗ {table}: {type(exc).__name__}: {exc}")
                return 1
            results[table] = (src, dst)
            mark = "✓" if (args.dry_run or src == dst) else "✗"
            print(f"  {mark} {table:<15} sqlite={src:<6} mysql={dst:<6}")

        print("== 3/4 校验 ==")
        if not args.dry_run:
            src_sum, dst_sum = event_checksum(sqlite_conn, mysql)
            same = src_sum == dst_sum
            print(f"  events 校验和 sqlite={src_sum[:16]}... mysql={dst_sum[:16]}... "
                  f"{'一致 ✓' if same else '不一致 ✗'}")
            if not same:
                print("  !! 数据不一致，请勿切换 DB_BACKEND")
                return 1

        print("== 4/4 结论 ==")
        bad = [t for t, (s, d) in results.items() if not args.dry_run and s != d]
        if bad:
            print(f"  ✗ 行数不一致: {', '.join(bad)}")
            return 1
        print("  ✓ 迁移完成，可以切换 DB_BACKEND=mysql 并重启服务")
        print("  提醒：切换前先备份 SQLite 文件，切换后观察 /healthz 与任务页")
        return 0
    finally:
        try:
            mysql.close()
        except Exception:  # noqa: BLE001
            pass
        sqlite_conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
