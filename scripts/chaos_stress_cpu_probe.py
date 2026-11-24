"""Probe container CPU during StressChaos."""
import subprocess
import time

import httpx

PROM = "http://127.0.0.1:9090/api/v1/query"
NODE = "k3d-copilot-server-0"


def q(expr: str) -> float:
    with httpx.Client(timeout=10) as client:
        response = client.get(PROM, params={"query": expr})
        result = response.json()["data"]["result"]
        return round(float(result[0]["value"][1]), 2) if result else -1.0


def kc(*args: str) -> None:
    subprocess.run(["docker", "exec", NODE, "kubectl", *args],
                   check=False, capture_output=True, text=True)


NODE_CPU = '100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[30s])))'
POD_CPU = ('sum(rate(container_cpu_usage_seconds_total'
           '{namespace="copilot",container!=""}[30s])) * 100')
print("baseline node/pod", q(NODE_CPU), q(POD_CPU))
subprocess.run(["docker", "cp", "/tmp/stress-cpu.yaml", f"{NODE}:/tmp/stress-cpu.yaml"])
kc("apply", "-f", "/tmp/stress-cpu.yaml")
time.sleep(15)
print("during node/pod", q(NODE_CPU), q(POD_CPU))
kc("delete", "-f", "/tmp/stress-cpu.yaml", "--timeout=20s")
time.sleep(5)
kc("patch", "stresschaos", "copilot-cpu-stress", "-n", "copilot",
   "--type=merge", "-p", '{"metadata":{"finalizers":[]}}')
time.sleep(5)
print("after node/pod", q(NODE_CPU), q(POD_CPU))
