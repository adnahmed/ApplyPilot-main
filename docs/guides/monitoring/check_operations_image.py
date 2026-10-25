"""PostgreSQL exporter image regression and local HTTP probes.

--self-test writes only to a temporary fixture, never /workspace.
--healthcheck checks the actual /metrics handler, not merely a TCP port.
--require-store additionally requires a readable operational journal. Neither
HTTP success nor a readable journal proves application/submission progress.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import os
import tempfile
import urllib.request


def self_test() -> None:
    """Import and missing-store check only; no server is provisioned at image build."""
    from applypilot.operations.metrics import render
    with tempfile.TemporaryDirectory(prefix="applypilot-exporter-test-") as name:
        root = Path(name)
        assert "applypilot_operations_store_up 0\n" in render(root)
        assert not (root / "operations").exists()
    print("Exporter import/missing-store checks passed; live PostgreSQL validation is separate.")


def probe(*, require_store: bool = False, port: int = 9108) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/metrics", timeout=5) as response:
        if response.status != 200:
            raise RuntimeError("Exporter did not return HTTP 200")
        text = response.read(4 * 1024 * 1024).decode("utf-8")
    values = {line for line in text.splitlines() if line.startswith("applypilot_operations_store_up ")}
    if values not in ({"applypilot_operations_store_up 0"}, {"applypilot_operations_store_up 1"}):
        raise RuntimeError("Exporter response is missing a valid store-availability metric")
    available = "applypilot_operations_store_up 1" in values
    if require_store and not available:
        raise RuntimeError("Exporter responds, but operations journal is missing, inaccessible or incompatible (store_up=0)")
    print(f"Exporter HTTP check passed; operations_store_up={int(available)} (not pipeline health).")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--self-test", action="store_true")
    action.add_argument("--healthcheck", action="store_true")
    action.add_argument("--require-store", action="store_true")
    parser.add_argument("--port", type=int, default=9108)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        self_test() if args.self_test else probe(require_store=args.require_store, port=args.port)
        return 0
    except Exception as exc:
        # No database paths, connection strings, raw HTTP bodies or credentials.
        print(f"Exporter check FAILED: {type(exc).__name__}")
        if isinstance(exc, RuntimeError):
            print(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
