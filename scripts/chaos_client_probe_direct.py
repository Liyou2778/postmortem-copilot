import socket
import time
import urllib.request

targets = [
    "http://10.42.0.116:8000/healthz",
    "http://10.42.0.115:8000/healthz",
    "http://10.43.58.37:8000/healthz",
    "http://172.24.19.160:18000/healthz",
]
for target in targets:
    try:
        start = time.perf_counter()
        body = urllib.request.urlopen(target, timeout=5).read()
        print(target, "ok", round(time.perf_counter() - start, 4), body[:20])
    except Exception as exc:  # noqa: BLE001
        print(target, "fail", type(exc).__name__, str(exc)[:120])
