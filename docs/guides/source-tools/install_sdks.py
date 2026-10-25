"""Conflict-isolated SDKs inside Docker, not in ApplyPilot's host environment."""
import json
from pathlib import Path
import subprocess
import venv

PACKAGES = {
    'ddgs':'ddgs>=9,<10', 'playwright':'playwright>=1.40,<2',
    'twikit':'twikit>=2.3,<3',
    'linkedin_scraper':'linkedin-scraper>=3.1.2,<4',
}
versions={}
for name, requirement in PACKAGES.items():
    directory=Path('/opt/tools')/name
    venv.EnvBuilder(with_pip=True).create(directory)
    python=str(directory/'bin/python')
    subprocess.run([python,'-m','pip','install','--no-cache-dir',requirement],check=True)
    subprocess.run([python,'-c',f'import {name}'],check=True)
    versions[name]=subprocess.check_output([python,'-m','pip','freeze'],text=True).splitlines()
Path('/opt/applypilot/sdk-versions.json').write_text(json.dumps(versions,indent=2)+'\n')
