"""Install the pinned Jobseek crawler in its own Python 3.13 environment."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

COMMIT = 'e65fb972e4fbb349d10ed66dc9418b1d271f52ca'
REPOSITORY = 'https://github.com/colophon-group/jobseek.git'


def run(*command, cwd=None):
    subprocess.run([str(part) for part in command], cwd=cwd, check=True)


def registry_counts(executable):
    """Cache actual installed registries without launching a browser or reading a site."""
    program = ('import json; from src.core.monitors import all_monitor_types; '
               'from src.core.scrapers import all_scraper_types; '
               'print(json.dumps({"monitor_count":len(all_monitor_types()),'
               '"scraper_count":len(all_scraper_types())}))')
    with tempfile.TemporaryDirectory(prefix='applypilot-jobseek-install-') as directory:
        output = subprocess.check_output([str(executable), '-I', '-c', program], cwd=directory, text=True)
    counts = json.loads(output.strip().splitlines()[-1])
    if any(type(counts.get(key)) is not int or counts[key] < 1 for key in ('monitor_count', 'scraper_count')):
        raise ValueError('Installed Jobseek registries are empty or invalid')
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', type=Path, help='Reuse an existing clean checkout at the pinned commit')
    args = parser.parse_args()
    uv = shutil.which('uv')
    if not uv or not shutil.which('git'):
        parser.error('Install uv and git before installing the Jobseek runtime')
    directory = Path(os.environ.get('APPLYPILOT_DIR', Path.home() / '.applypilot')).expanduser().resolve()
    directory = directory / 'tools' / 'jobseek'
    directory.mkdir(parents=True, exist_ok=True)
    checkout = args.checkout.resolve() if args.checkout else directory / 'source'
    if not checkout.exists():
        run('git', 'clone', '--filter=blob:none', '--no-checkout', '--sparse', REPOSITORY, checkout)
        run('git', 'sparse-checkout', 'set', 'apps/crawler/src', 'apps/crawler/contracts', cwd=checkout)
        run('git', 'checkout', '--detach', COMMIT, cwd=checkout)
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=checkout, text=True).strip()
    dirty = subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'],
                                    cwd=checkout, text=True).strip()
    if revision != COMMIT or dirty:
        parser.error('Source checkout must be clean and exactly at ' + COMMIT)
    runtime = directory / 'runtime'
    executable = runtime / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not executable.is_file():
        run(uv, 'venv', '--python', '3.13', runtime)
    run(uv, 'pip', 'install', '--python', executable, checkout / 'apps' / 'crawler')
    run(executable, '-m', 'playwright', 'install', 'chromium')
    shutil.copyfile(checkout / 'LICENSE', directory / 'UPSTREAM-LICENSE')
    (directory / 'installation.json').write_text(json.dumps({
        'repository': REPOSITORY, 'commit': COMMIT, 'python': str(executable),
        'integration': 'pure monitor_one / scrape_one; no upstream persistence services',
        **registry_counts(executable),
    }, indent=2) + '\n', encoding='utf-8')
    print('Installed Jobseek crawler ' + COMMIT + ' at ' + str(runtime))


if __name__ == '__main__':
    main()
