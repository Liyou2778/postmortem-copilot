"""Dump the live SQLite schema as JSON (used to build the MySQL DDL).

Run on the server:
    /opt/copilot/.venv/bin/python /root/dump_schema.py
"""
from __future__ import annotations

import json
import sqlite3
import sys

DB = sys.argv[1] if len(sys.argv) > 1 else "/opt/copilot/data/core.db"

conn = sqlite3.connect(DB)
tables = []
for name, sql in conn.execute(
    "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL "
    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
):
    cols = [
        {"name": r[1], "type": r[2], "notnull": bool(r[3]), "pk": int(r[5])}
        for r in conn.execute(f"PRAGMA table_info({name})")
    ]
    count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
    tables.append({"name": name, "columns": cols, "rows": count, "sql": sql})

indexes = [
    sql
    for (sql,) in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
    )
]

print(json.dumps({"tables": tables, "indexes": indexes}, ensure_ascii=False, indent=2))
