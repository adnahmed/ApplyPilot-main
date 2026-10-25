"""Deprecated executable spelling: submit a native backup request and exit."""
import asyncio
import json


def main():
    from applypilot.orchestration.client import run_work
    print(json.dumps(asyncio.run(run_work("backup", {"automatic": True})), indent=2))


if __name__ == "__main__":
    main()
