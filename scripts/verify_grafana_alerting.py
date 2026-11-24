"""Provision a firing test alert and verify it reaches the local webhook."""
import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:3000"
PW = open("/root/grafana-admin.txt", encoding="utf-8").read().strip()
auth = ("admin", PW)

with httpx.Client(timeout=20, auth=auth) as client:
    folders = client.get(BASE + "/api/folders").json()
    folder = next(f for f in folders if f.get("title") == "Copilot")
    contact = client.post(BASE + "/api/v1/provisioning/contact-points", json={
        "name": "copilot-webhook",
        "type": "webhook",
        "settings": {
            "url": "http://172.24.19.160:9009/alert",
            "httpMethod": "POST",
        },
        "disableResolveMessage": False,
    })
    print("contact", contact.status_code, contact.text[:200])
    policy = client.put(BASE + "/api/v1/provisioning/policies", json={
        "receiver": "copilot-webhook",
        "group_by": ["grafana_folder", "alertname"],
        "routes": [],
    })
    print("policy", policy.status_code, policy.text[:200])
    rule = {
        "title": "Copilot alert channel test",
        "ruleGroup": "copilot",
        "folderUID": folder["uid"],
        "condition": "C",
        "data": [
            {
                "refId": "A",
                "relativeTimeRange": {"from": 60, "to": 0},
                "datasourceUid": "prometheus",
                "model": {
                    "editorMode": "code", "expr": "vector(1)",
                    "instant": True, "range": False,
                },
            },
            {
                "refId": "C",
                "datasourceUid": "__expr__",
                "model": {
                    "conditions": [{
                        "evaluator": {"params": [0], "type": "gt"},
                        "operator": {"type": "and"},
                        "query": {"params": ["A"]},
                        "reducer": {"params": [], "type": "last"},
                        "type": "threshold",
                    }],
                    "datasource": {"type": "__expr__", "uid": "__expr__"},
                    "expression": "A", "intervalMs": 1000,
                    "maxDataPoints": 43200, "type": "threshold",
                },
            },
        ],
        "noDataState": "NoData",
        "execErrState": "Error",
        "for": "0s",
        "annotations": {"summary": "notification channel test"},
        "labels": {"severity": "test"},
        "isPaused": False,
    }
    created = client.post(BASE + "/api/v1/provisioning/alert-rules", json=rule)
    print("rule", created.status_code, created.text[:300])

print("waiting 90s for evaluation...")
time.sleep(90)
from pathlib import Path
path = Path("/tmp/grafana-alerts.jsonl")
print("webhook_file_exists", path.exists())
if path.exists():
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    print("webhook_lines", len(lines))
    for line in lines[-2:]:
        print(line[:300])
