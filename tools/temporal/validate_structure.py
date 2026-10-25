"""Offline source/configuration checks; NOT Docker, SDK, replay or database validation."""
from __future__ import annotations
import ast
import hashlib
import json
from pathlib import Path
import shlex
import sys
import tomllib

import yaml

ROOT = Path(__file__).resolve().parents[2]


def main():
    paths = [p for directory in ('src', 'scripts', 'tools', 'tests')
             for p in (ROOT / directory).rglob('*.py')
             if not any(x in p.parts for x in ('__pycache__', 'node_modules'))]
    for p in paths:
        ast.parse(p.read_text(encoding='utf-8-sig'), filename=str(p), feature_version=(3, 12))
    registry = json.loads((ROOT/'FEATURE_REGISTRY.json').read_text())
    for module in registry['modules']:
        actual = hashlib.sha256((ROOT/module['file']).read_bytes()).hexdigest()
        if actual != module['sha256']:
            raise AssertionError('Stale feature registry: ' + module['file'])
    audit = json.loads((ROOT/'ARCHITECTURE_AUDIT.json').read_text())
    for item in audit['source_files']:
        if hashlib.sha256((ROOT/item['path']).read_bytes()).hexdigest() != item['sha256']:
            raise AssertionError('Stale architecture audit: ' + item['path'])
    workflows = {x['type'] for x in registry['registered_workflows']}
    activities = {x['type'] for x in registry['registered_activities']}
    if any(check['status'] != 'passed' for check in audit['checks']):
        raise AssertionError('Architecture audit has unresolved requirements')
    for feature in registry['features']:
        for key, known in [('workflow', workflows), ('activity', activities)]:
            references = feature.get(key)
            if isinstance(references, list) and not set(references) <= known:
                raise AssertionError('Feature refers to an absent decorated definition: ' + feature['name'])
    for path in [ROOT/'docs/guides/docker-compose.temporal.yml',
                 ROOT/'src/applypilot/config/runtime_policy.yaml',
                 *sorted((ROOT/'docs/guides/temporal').rglob('*.yaml'))]:
        list(yaml.safe_load_all(path.read_text()))
    tomllib.loads((ROOT/'pyproject.toml').read_text())
    # Check COPY sources and explicit allowlist declarations, not Dockerignore's
    # complete matching semantics or image-build behavior.
    checked = []
    for filename in ('Dockerfile.control', 'Dockerfile.search', 'Dockerfile.worker'):
        path = ROOT/'docs/guides/temporal'/filename
        ignore = Path(str(path)+'.dockerignore').read_text().splitlines()
        assert ignore[0] == '**'
        for line in path.read_text().splitlines():
            if line.startswith('COPY '):
                fields = shlex.split(line)
                if any(field.startswith('--from=') for field in fields):
                    continue
                for source in fields[1:-1]:
                    assert (ROOT/source).exists(), f'Missing COPY input: {source}'
                    checked.append(source)
        assert not any('secrets' in line and line.startswith('!') for line in ignore)
    control_ignore = (ROOT/'docs/guides/temporal/Dockerfile.control.dockerignore').read_text()
    for package in ('documents', 'signals'):
        for filename in ('__init__.py', 'contracts.py'):
            assert '!src/applypilot/' + package + '/' + filename in control_ignore
    print(json.dumps({'status': 'passed', 'python_files_parsed_as_312': len(paths),
        'actual_interpreter': sys.version.split()[0], 'actual_project_runtime_supported': sys.version_info[:2] == (3,12),
        'source_module_hashes_verified': len(registry['modules']),
        'registered_workflow_types': len(workflows), 'registered_activity_types': len(activities),
        'feature_families': len(registry['features']), 'docker_copy_inputs_exist': len(checked),
        'note': 'AST/YAML/TOML/text checks only. Does not validate native SDK registration, Docker Compose expansion/build, SQL or runtime behavior.'}))


if __name__ == '__main__':
    main()
