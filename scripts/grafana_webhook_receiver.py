"""Minimal Grafana webhook receiver for alert-notification verification."""
from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OUT = Path("/tmp/grafana-alerts.jsonl")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        record = {"at": time.time(), "path": self.path, "body": body.decode("utf-8", "replace")}
        with OUT.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, fmt: str, *args) -> None:
        print(f"{time.strftime('%H:%M:%S')} {fmt % args}", flush=True)


def main() -> int:
    server = ThreadingHTTPServer(("0.0.0.0", 9009), Handler)
    print("Grafana webhook receiver on :9009 -> /tmp/grafana-alerts.jsonl", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
