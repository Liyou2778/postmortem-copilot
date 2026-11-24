import sys, traceback
sys.path.insert(0, "services/core")
from app.config import load_settings
from app.db import from_settings

db = from_settings(load_settings())
for table in ("ci", "ci_relation", "change_request"):
    try:
        cur = db.conn.cursor()
        cur.execute(
            f"ALTER TABLE {table} ADD COLUMN tenant_id "
            "VARCHAR(64) NOT NULL DEFAULT 'team-core'"
        )
        db.commit()
        print(table, "OK")
    except Exception as exc:
        print(table, "ERR", type(exc).__name__, exc)
