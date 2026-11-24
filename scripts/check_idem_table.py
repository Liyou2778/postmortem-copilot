import sys
sys.path.insert(0, "services/core")
from app.config import load_settings
from app.db import from_settings

db = from_settings(load_settings())
try:
    cur = db.execute("SHOW TABLES LIKE 'task_create_idempotency'")
    print("table", cur.fetchall())
    cur = db.execute("DESCRIBE task_create_idempotency")
    print(cur.fetchall())
except Exception as exc:
    print("ERR", type(exc).__name__, exc)
