"""Provision one Grafana alert rule for Copilot 5xx traffic."""
import json
import sys

import httpx

BASE = "http://127.0.0.1:3000"
PW = open("/root/grafana-admin.txt", encoding="utf-8").read().strip()
auth = ("admin", PW)

with httpx.Client(timeout=15, auth=auth) as client:
    folders = client.get(BASE + "/api/folders").json()
    folder = next((f for f in folders if f.get("title") == "Copilot"), None)
    if not folder:
        print("folder not found")
        sys.exit(1)
    rule = {
        "title": "Copilot HTTP 5xx rate",
        "ruleGroup": "copilot",
        "folderUID": folder["uid"],
        "condition": "C",
        "data": [
            {
                "refId": "A",
                "relativeTimeRange": {"from": 300, "to": 0},
                "datasourceUid": "prometheus",
                "model": {
                    "editorMode": "code",
                    "expr": 'sum(rate(copilot_http_requests_total{status=~"5.."}[5m]))',
                    "instant": True,
                    "range": False,
                },
            },
            {
                "refId": "C",
                "datasourceUid": "__expr__",
                "model": {
                    "conditions": [{
                        "evaluator": {"params": [0.05], "type": "gt"},
                        "operator": {"type": "and"},
                        "query": {"params": ["A"]},
                        "reducer": {"params": [], "type": "last"},
                        "type": "threshold",
                    }],
                    "datasource": {"type": "__expr__", "uid": "__expr__"},
                    "expression": "A",
                    "intervalMs": 1000,
                    "maxDataPoints": 43200,
                    "type": "threshold",
                },
            },
        ],
        "noDataState": "NoData",
        "execErrState": "Error",
        "for": "2m",
        "annotations": {"summary": "Copilot 5xx rate above 0.05/s for 2m"},
        "labels": {"severity": "warning"},
        "isPaused": False,
    }
    response = client.post(BASE + "/api/v1/provisioning/alert-rules", json=rule)
    print("create", response.status_code, response.text[:400])
    rules = client.get(BASE + "/api/v1/provisioning/alert-rules").json()
    print("rules", [(r.get("title"), r.get("condition")) for r in rules])
