"""Review policy instructions before installing an exported learning bundle."""
from __future__ import annotations

import difflib
from copy import deepcopy
import re
import secrets
import time

from applypilot.onboarding.storage import read_json, write_json
from applypilot.reasoning.policies import bundle_fingerprint, export_bundle, install_bundle, validate


def fingerprint(bundle):
    return bundle_fingerprint(bundle)


def instructions(bundle):
    rows = {(row['task'], row['schema_hash'], 'fallback'): row for row in bundle.get('artifacts', [])}
    for row in bundle.get('revisions', []):
        if row['state'] == 'active':
            rows[(row['task'], row['schema_hash'], 'active')] = row
    return rows


def installed_after(current, imported):
    """Mirror installation's scope retirement and artifact replacement in memory."""
    revisions = {row['revision']: deepcopy(row) for row in current['revisions']}
    scopes = {(row['task'], row['schema_hash']) for row in imported['revisions']}
    for row in revisions.values():
        if row['state'] == 'active' and (row['task'], row['schema_hash']) in scopes:
            row['state'] = 'retired'
    revisions.update({row['revision']: deepcopy(row) for row in imported['revisions']})
    artifacts = {row['task']: deepcopy(row) for row in current['artifacts']}
    artifacts.update({row['task']: deepcopy(row) for row in imported['artifacts']})
    return {'format': current['format'], 'revisions': list(revisions.values()), 'artifacts': list(artifacts.values())}


def preview(root, data):
    bundle = validate(data.get('bundle'))
    current = export_bundle(workspace=root)
    before, after = instructions(current), instructions(installed_after(current, bundle))
    scopes = {(row['task'], row['schema_hash']) for row in bundle['revisions']}
    artifact_tasks = {row['task'] for row in bundle['artifacts']}
    changes = []
    for key in sorted(set(before) | set(after)):
        task, schema_hash, policy_type = key
        if not ((policy_type == 'active' and (task, schema_hash) in scopes)
                or (policy_type == 'fallback' and task in artifact_tasks)):
            continue
        previous, row = before.get(key, {}), after.get(key, {})
        old_text, new_text = previous.get('instructions', ''), row.get('instructions', '')
        change = 'removed' if previous and not row else 'added' if row and not previous else 'changed' if previous != row else 'unchanged'
        changes.append({'task': task, 'schema_hash': schema_hash, 'policy_type': policy_type,
                        'before': old_text, 'after': new_text, 'before_revision': previous.get('revision'),
                        'revision': row.get('revision'), 'changed': change != 'unchanged', 'change': change,
                        'diff': '\n'.join(difflib.unified_diff(old_text.splitlines(), new_text.splitlines(),
                                     fromfile='Installed', tofile='After import', lineterm=''))})
    identifier = secrets.token_hex(16)
    folder = root / 'setup' / 'policy-previews'
    folder.mkdir(parents=True, exist_ok=True)
    for path in folder.glob('*.json'):
        if not path.is_symlink() and path.stat().st_mtime < time.time() - 86400:
            path.unlink()
    write_json(folder / (identifier + '.json'), {'bundle': bundle, 'before': fingerprint(current), 'created': time.time()})
    return {'preview_id': identifier, 'changes': changes, 'revision_count': len(bundle['revisions']),
            'message': 'Review each task and schema below, including deployments that will be removed. Recorded evaluations and compatibility checks remain in force.'}


def install(root, data):
    identifier = str(data.get('preview_id', ''))
    if not re.fullmatch(r'[a-f0-9]{32}', identifier):
        raise ValueError('Preview the policy file before installing it')
    path = root / 'setup' / 'policy-previews' / (identifier + '.json')
    saved = read_json(path, {}, limit=6_000_000)
    if not saved or saved.get('created', 0) < time.time() - 86400:
        raise ValueError('This preview expired. Upload the policy again')
    if data.get('trust') is not True:
        raise ValueError('Confirm that you trust the source of these instructions')
    result = install_bundle(saved['bundle'], trust=True, replace=data.get('replace') is True,
                            workspace=root, expected_fingerprint=saved['before'])
    path.unlink(missing_ok=True)
    return {**result, 'message': 'The reviewed policy was installed. Future work uses the installed instructions.'}
