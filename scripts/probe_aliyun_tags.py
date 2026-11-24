import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "core"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.cmdb.aliyun import AliyunDiscovery  # noqa: E402

discovery = AliyunDiscovery()
for resource_type in ("ACS::ECS::Instance", "ACS::RDS::DBInstance", "ACS::OSS::Bucket"):
    try:
        resources = discovery.search_resources(resource_type)
        print(resource_type, json.dumps([
            {"id": r.resource_id, "tags": r.tags} for r in resources
        ], ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        print(resource_type, "ERROR", type(exc).__name__, str(exc)[:300])
