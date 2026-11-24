"""Minimal OTLP/HTTP receiver for integration verification.

Not a production collector: it accepts POST /v1/traces, appends the JSON body
to a file, and returns 200. Used to prove the app-side exporter works without
claiming a real Collector deployment.
"""
from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Handler(BaseHTTPRequestHandler):
    output = Path("/tmp/otel-spans.jsonl")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
            count = sum(
                len(scope.get("spans") or [])
                for resource in payload.get("resourceSpans") or []
                for scope in resource.get("scopeSpans") or []
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            count = -1
        record = {"at": time.time(), "path": self.path, "spans": count}
        with self.output.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"partialSuccess":{}}')

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        print(f"{time.strftime('%H:%M:%S')} {fmt % args}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=4318)
    parser.add_argument("--output", default="/tmp/otel-spans.jsonl")
    args = parser.parse_args()
    Handler.output = Path(args.output)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"OTLP receiver on {args.host}:{args.port} -> {args.output}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
