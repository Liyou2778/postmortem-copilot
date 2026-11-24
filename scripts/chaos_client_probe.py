import socket
import time
import urllib.request

for target in ("http://postmortem-copilot:8000/healthz",
               "http://172.24.19.160:18000/healthz"):
    try:
        start = time.perf_counter()
        body = urllib.request.urlopen(target, timeout=5).read()
        print(target, "ok", round(time.perf_counter() - start, 4), body[:20])
    except Exception as exc:  # noqa: BLE001
        print(target, "fail", type(exc).__name__, str(exc)[:120])
try:
    print("dns", socket.gethostbyname("postmortem-copilot"))
except Exception as exc:  # noqa: BLE001
    print("dns fail", exc)
