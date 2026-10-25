"""Optional viewer entrypoint without loading LLM/application worker imports."""
import argparse
from .server import serve

parser = argparse.ArgumentParser(description="ApplyPilot local web dashboard")
parser.add_argument("--port", type=int, default=8765)
parser.add_argument("--no-open", action="store_true")
parser.add_argument("--read-only", action="store_true")
parser.add_argument("--open", action="store_true", help="Explicitly open the system browser")
args = parser.parse_args()
serve(port=args.port, open_browser=args.open and not args.no_open, read_only=args.read_only)
