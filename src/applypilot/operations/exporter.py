"""Container-network-only, read-only exporter. No dashboard token or API access."""
from __future__ import annotations
import argparse
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .metrics import render


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True, help="Workspace root; only operations/ is mounted")
    parser.add_argument("--port", type=int, default=9108)
    parser.add_argument("--require-postgres-url", action="store_true")
    args = parser.parse_args()
    if args.require_postgres_url and not os.environ.get("APPLYPILOT_DATABASE_URL"):
        parser.error("Container telemetry requires APPLYPILOT_DATABASE_URL for a least-privilege PostgreSQL reader")
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/metrics":
                self.send_error(404)
                return
            body = render(args.root).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
