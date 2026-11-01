"""Local POST sink for the M3 outage smoke.

Logs each batch as a JSONL line to RECEIVER_LOG. Run with
``python tests/sink_receiver.py 8765 /tmp/sink.jsonl``.
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


def make_handler(log_path: str):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return  # quiet

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8") if length else ""
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = {"_invalid": body}
            with open(log_path, "a") as f:
                f.write(json.dumps(parsed) + "\n")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

    return H


def main():
    port = int(sys.argv[1])
    log_path = sys.argv[2]
    # NOTE: append-only; tests need both pre- and post-outage batches in one file.
    server = HTTPServer(("127.0.0.1", port), make_handler(log_path))
    server.serve_forever()


if __name__ == "__main__":
    main()
