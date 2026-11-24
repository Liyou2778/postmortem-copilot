"""Observe node CPU during a Chaos Mesh StressChaos experiment."""
import json
import subprocess
import time

import httpx

PROM = "http://127.0.0.1:9090/api/v1/query"
NODE = "k3d-copilot-server-0"


def cpu() -> float:
    with httpx.Client(timeout=10) as client:
        response = client.get(PROM, params={
            "query": '100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[1m])))'
        })
        data = response.json()["data"]["result"]
        return round(float(data[0]["value"][1]), 2) if data else -1.0


def kubectl(*args: str) -> None:
    subprocess.run(
        ["docker", "exec", NODE, "kubectl", *args],
        check=False, capture_output=True, text=True,
    )


def main() -> int:
    print("baseline CPU%", cpu())
    subprocess.run(
        ["docker", "cp", "/tmp/stress-cpu.yaml", f"{NODE}:/tmp/stress-cpu.yaml"],
        check=False,
    )
    kubectl("apply", "-f", "/tmp/stress-cpu.yaml")
    time.sleep(8)
    print("during CPU%", cpu())
    kubectl("delete", "-f", "/tmp/stress-cpu.yaml", "--timeout=20s")
    time.sleep(3)
    if subprocess.run(
        ["docker", "exec", NODE, "kubectl", "get", "stresschaos",
         "copilot-cpu-stress", "-n", "copilot"],
        capture_output=True,
    ).returncode == 0:
        kubectl("patch", "stresschaos", "copilot-cpu-stress", "-n", "copilot",
                "--type=merge", "-p", '{"metadata":{"finalizers":[]}}')
    time.sleep(8)
    print("after CPU%", cpu())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
