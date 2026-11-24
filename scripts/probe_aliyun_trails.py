import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "core"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alibabacloud_actiontrail20200706 import models
from alibabacloud_actiontrail20200706.client import Client as ATClient
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_credentials.client import Client as CredClient
from alibabacloud_credentials.models import Config as CredConfig
from app.config import get

cred = CredClient(CredConfig(type="ecs_ram_role", role_name=get("CMDB_RAM_ROLE_NAME", "copilot-cmdb-reader")))
cfg = open_api_models.Config(credential=cred, region_id=get("ALIYUN_REGION", "cn-hangzhou"),
                             endpoint="actiontrail.cn-hangzhou.aliyuncs.com")
client = ATClient(cfg)
try:
    resp = client.describe_trails(models.DescribeTrailsRequest())
    trails = resp.body.trail_list or []
    print("trails:", len(trails))
    for t in trails:
        print({k: getattr(t, k, None) for k in ("name", "home_region", "status", "trail_region", "event_rw")})
except Exception as exc:
    print("ERROR", type(exc).__name__, str(exc)[:800])
