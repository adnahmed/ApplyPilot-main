"""Generate documentation from source without importing application integrations.

This tool is NOT imported by the runtime and does not dispatch or admit work.
AST function inventory is exhaustive for src/applypilot; semantic feature-family
coverage is curated and explicitly does not certify a complete migration.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import inspect
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'src' / 'applypilot'
TERMS = ('dask', 'distributed', 'supervisor', 'reservation', 'lease', 'cooldown',
         'next_retry_at', 'budget', 'run_cycle', 'run_round', 'worker_registry')


def decorators(node):
    return [ast.unparse(item) for item in node.decorator_list]


def inventory_file(path: Path) -> dict:
    text = path.read_text(encoding='utf-8')
    module = ast.parse(text, filename=str(path), feature_version=(3, 12))
    functions, classes, imports = [], [], []

    def visit(node, prefix=''):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + child.name
                functions.append({'symbol': name, 'line': child.lineno, 'end_line': child.end_lineno,
                    'inputs': ast.unparse(child.args),
                    'declared_output': ast.unparse(child.returns) if child.returns else None,
                    'async': isinstance(child, ast.AsyncFunctionDef), 'decorators': decorators(child),
                    'calls': sorted({ast.unparse(call.func) for call in ast.walk(child) if isinstance(call, ast.Call)}),
                    'semantic_side_effect_review': 'Not inferred solely from AST; see curated feature family.'})
                visit(child, name + '.')
            elif isinstance(child, ast.ClassDef):
                name = prefix + child.name
                classes.append({'symbol': name, 'line': child.lineno,
                                'bases': [ast.unparse(base) for base in child.bases],
                                'decorators': decorators(child)})
                visit(child, name + '.')
            else:
                visit(child, prefix)

    visit(module)
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom):
            imports.append({'module': '.' * node.level + (node.module or ''),
                            'names': [item.name for item in node.names], 'line': node.lineno})
        elif isinstance(node, ast.Import):
            imports.extend({'module': item.name, 'names': [], 'line': node.lineno} for item in node.names)
    markers = {}
    for term in TERMS:
        matches = [index for index,line in enumerate(text.splitlines(),1)
                   if re.search(r'\b' + re.escape(term) + r'\b', line, re.I)]
        if matches:
            markers[term] = matches
    return {'file': path.relative_to(ROOT).as_posix(), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'lines': len(text.splitlines()), 'imports': imports, 'classes': classes,
            'functions': functions, 'lexical_markers_not_proof_of_scheduler_behavior': markers}


def build() -> dict:
    curated = json.loads((ROOT/'docs/guides/temporal/feature-status.json').read_text())
    from applypilot.orchestration.worker import workflow_types, activities_for
    from applypilot.orchestration.contracts import ROLES
    workflows, activities = [], []
    for cls in workflow_types():
        definition = getattr(cls, '__temporal_workflow_definition')
        workflows.append({'type': definition.name, 'symbol': cls.__name__,
            'module': cls.__module__, 'source': Path(inspect.getfile(cls)).relative_to(ROOT).as_posix()})
    for role in ROLES:
        if role == 'workflow':
            continue
        for fn in activities_for(role):
            definition = getattr(fn, '__temporal_activity_definition')
            activities.append({'type': definition.name, 'symbol': fn.__qualname__,
                'module': fn.__module__, 'role': role, 'queue': f'applypilot.default.{role}',
                'source': Path(inspect.getfile(fn)).relative_to(ROOT).as_posix()})
    known_workflows = {row['type'] for row in workflows}
    known_activities = {row['type'] for row in activities}
    for feature in curated['features']:
        for name in feature['modules']:
            if not (SOURCE/name).is_file():
                raise ValueError('Feature inventory points to a missing module: ' + name)
        for key, known in (('workflow', known_workflows), ('activity', known_activities)):
            if not isinstance(feature[key], list) or not set(feature[key]) <= known:
                raise ValueError('Feature references an unregistered ' + key + ': ' + feature['name'])
    modules = [inventory_file(path) for path in sorted(SOURCE.rglob('*.py'))]
    source_registry = ast.parse((SOURCE/'integrations/registry.py').read_text())
    registry = next(ast.literal_eval(node.value) for node in source_registry.body
                    if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='SOURCES' for t in node.targets))
    return {**curated, 'inventory_generator': 'tools/temporal/inventory.py',
        'python_grammar_checked': '3.12 (AST parsing under the validation interpreter; not a 3.12 runtime test)',
        'module_count': len(modules), 'function_definition_count': sum(len(m['functions']) for m in modules),
        'registered_workflows': workflows, 'registered_activities': activities,
        'source_registry': [{'upstream': item[0], 'repository_disposition': item[1], 'capability_description': item[2]} for item in registry],
        'provider_page_modules': [m['file'] for m in modules if '/discovery/' in m['file']
             and any(f['symbol'].endswith('fetch_page') for f in m['functions'])],
        'limitations': ['A keyword hit can be a legitimate business/infrastructure term or a comment.',
            'Static calls are not a complete resolved transitive call graph.',
            'Curated feature families do not prove that every useful behavior has a native path.',
            'Existing generic Temporal Activities are not classified as migrated just because they are registered.'],
        'modules': modules}


def feature_map(data: dict) -> str:
    lines = ['# ApplyPilot feature map', '', 'Source mapping for the current Temporal execution path. Live provider coverage is recorded separately in `VALIDATION.json`.', '',
        f"The current source contains {data['module_count']} Python modules and {data['function_definition_count']} function definitions. "
        f"The table below maps {len(data['features'])} curated feature families. `FEATURE_REGISTRY.json` records every parsed source module, "
        'function signature, declared output, decorator, direct call expression, and source location. '
        'An AST inventory is not proof of semantic coverage.', '',
        'Queue names below use the default workspace. `applypilot.default.research` is the new capability queue. '
        'Workflow and Activity identifiers are checked against the production worker registration functions.', '',
        '| Feature | Current workflow/activity path | Queue | Trigger | Status |',
        '|---|---|---|---|---|']
    for item in data['features']:
        values = [item['name'], item['temporal_path'] or 'No independent native path', item['queue'] or 'Not independently assigned', item['trigger'], item['migration_status']]
        lines.append('| ' + ' | '.join(value.replace('|','\\|') for value in values) + ' |')
    lines += ['', '## Feature contracts and remaining work', '']
    for item in data['features']:
        lines += ['### ' + item['name'], '',
            '**Source:** ' + ', '.join('`src/applypilot/' + name + '`' for name in item['modules']) + '.', '',
            f"**Entity:** {item['domain_entity']}. **Inputs:** {item['inputs']}. **Outputs:** {item['outputs']}.", '',
            '**Side effects:** ' + item['side_effects'] + '. **Dashboard:** ' + item['dashboard'] + '.', '',
            '**Persistence:** ' + item['persistence'] + '.', '',
            '**Idempotency identity:** ' + item['idempotency_identity'] + '.', '',
            '**Remaining:** ' + item['remaining_work'], '']
    lines += ['## Actual external-source registry', '', '| Upstream | Repository disposition | Capability |', '|---|---|---|']
    for item in data['source_registry']:
        lines.append('| ' + ' | '.join(item.values()) + ' |')
    lines += ['', '## Regeneration', '', '```bash', 'python tools/temporal/inventory.py', '```', '',
        'This generator is documentation only. Nothing in the runtime consults it to schedule, block, reserve, or retry work.', '']
    return '\n'.join(line.rstrip() for line in lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'FEATURE_REGISTRY.json')
    parser.add_argument('--feature-map', type=Path, default=ROOT/'FEATURE_MAP.md')
    args = parser.parse_args()
    data = build()
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    args.feature_map.write_text(feature_map(data), encoding='utf-8')
    print(json.dumps({'module_count':data['module_count'], 'function_definition_count':data['function_definition_count'],
                     'feature_families':len(data['features']),
                     'registered_workflows':len(data['registered_workflows']),
                     'registered_activities':len(data['registered_activities'])}))


if __name__ == '__main__':
    main()
