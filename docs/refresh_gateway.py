"""Compatibility CLI for the gateway's canonical refresh entry point."""
from pathlib import Path
import runpy

if __name__ == '__main__':
    entry = Path(__file__).resolve().parents[1] / 'gateway' / 'refresh_gateway.py'
    if not entry.is_file():
        raise SystemExit('Gateway checkout missing. Run ./scripts/meta.ps1 git update first.')
    runpy.run_path(str(entry), run_name='__main__')
