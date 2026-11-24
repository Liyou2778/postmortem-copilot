import sys
sys.path.insert(0, "services/core")
from app.config import load_settings
from app.db import from_settings
from app.store import EventStore

db = from_settings(load_settings())
store = EventStore(db=db)
try:
    print("find", store.find_task_create("smoke-key"))
    store.remember_task_create("smoke-key", "smoke-task")
    print("remembered")
    print("find2", store.find_task_create("smoke-key"))
except Exception as exc:
    import traceback
    traceback.print_exc()
