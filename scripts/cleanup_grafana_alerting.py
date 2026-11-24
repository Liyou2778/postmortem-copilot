"""Clean up the Grafana test alert and avoid NoData false firing."""
import httpx

BASE = "http://127.0.0.1:3000"
PW = open("/root/grafana-admin.txt", encoding="utf-8").read().strip()

with httpx.Client(timeout=20, auth=("admin", PW)) as client:
    rules = client.get(BASE + "/api/v1/provisioning/alert-rules").json()
    for rule in rules:
        title = rule.get("title")
        uid = rule.get("uid")
        if title == "Copilot alert channel test":
            response = client.delete(f"{BASE}/api/v1/provisioning/alert-rules/{uid}")
            print("delete test", response.status_code)
        elif title == "Copilot HTTP 5xx rate":
            body = dict(rule)
            body["noDataState"] = "OK"
            response = client.put(
                f"{BASE}/api/v1/provisioning/alert-rules/{uid}", json=body
            )
            print("update 5xx", response.status_code, response.text[:200])
    final = client.get(BASE + "/api/v1/provisioning/alert-rules").json()
    print("remaining", [(r.get("title"), r.get("noDataState")) for r in final])
