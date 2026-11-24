import sys
sys.path.insert(0, "services/core")
import httpx
from app.config import get, load_settings
from app.db import from_settings

db = from_settings(load_settings())
cur = db.conn.cursor()
cur.execute("SELECT tenant_id, COUNT(*) FROM ci GROUP BY tenant_id")
print("tenant counts", cur.fetchall())
token = get("CMDB_TOKEN_VIEWER", "")
with httpx.Client(timeout=10) as client:
    response = client.get(
        "http://127.0.0.1:9200/api/v2/cis",
        headers={"X-CMDB-Token": token},
    )
    print("v2", response.status_code, len(response.json().get("cis", [])))
