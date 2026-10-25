"""Explicit database resets and resumable, history-preserving document cleanup."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Literal

from applypilot import postgres
from applypilot.atomic_io import atomic_record
from applypilot.postgres_sql import identifier

ResetScope = Literal['documents', 'all']
OUTPUT_DIRS = ('tailored_resumes', 'cover_letters', 'application-contexts')
PROTECTED_STATES = {'applying', 'applied', 'responded', 'interview', 'offer', 'rejected',
                    'verification_required', 'verification_quarantined'}
SAFE_ATTEMPT_STATES = {'verified_absent', 'cancelled_before_submit'}


@dataclass
class ResetReport:
    scope: str
    database: str
    dry_run: bool = False
    inventory_complete: bool = True
    phase: str = 'preview'
    old_jobs: int = 0
    affected_jobs: int = 0
    preserved_jobs: int = 0
    pprepared_attempts: int = 0
    removed_jobs: int = 0
    selected_files: int = 0
    removed_files: int = 0
    protected_files: int = 0
    unowned_files: int = 0
    skipped_files: int = 0
    backup: str | None = None
    journal: str | None = None
    dropped_schemas: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def _read_json(path, fallback):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return fallback


def _safe_path(root: Path, relative: str) -> Path:
    path = root / relative
    if not path.is_relative_to(root):
        raise ValueError('Reset path escapes the workspace')
    if any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction())
           for p in (path, *path.parents)):
        raise ValueError('Reset refuses redirected paths')
    resolved = path.resolve()
    if not any(resolved.is_relative_to(root / name) for name in OUTPUT_DIRS):
        raise ValueError('Reset path is outside generated document directories')
    return resolved


def _family(path: Path) -> str:
    for ending in ('.approval.json', '_REPORT.json', '_JOB.txt', '.pdf', '.docx', '.txt', '.json'):
        if path.name.endswith(ending):
            return os.path.normcase(str(path.absolute().parent / path.name[:-len(ending)]))
    return os.path.normcase(str(path.absolute()))


def _artifact_paths(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {'tailored_resume_path', 'cover_letter_path', 'path'} and isinstance(item, str):
                yield item
            elif key == 'files' and isinstance(item, dict):
                yield from item
            elif isinstance(item, (dict, list)) or key in {'artifact_snapshot', 'artifact_hashes_json'}:
                yield from _artifact_paths(item)
    elif isinstance(value, list):
        for item in value:
            yield from _artifact_paths(item)


def _read_only_uri(root):
    external = os.getenv('APPLYPILOT_DATABASE_URL', '').strip()
    if external:
        return external
    from applypilot.pgembed_compat import ready_postmaster
    data = Path(os.getenv('APPLYPILOT_PGDATA', str(root / 'postgres' / 'cluster-v1'))).resolve()
    info = ready_postmaster(data)
    return info.get_uri(database='postgres') if info is not None else None


def _table_exists(raw, schema, table):
    return raw.execute('SELECT to_regclass(%s)', (schema + '.' + table,)).fetchone()[0] is not None


def _rows(raw, schema, table):
    if not _table_exists(raw, schema, table):
        return []
    cursor = raw.execute('SELECT * FROM ' + identifier(schema) + '.' + identifier(table))
    names = [c.name for c in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _inventory(root, raw, scope, previous=None):
    schema = postgres.namespace(root / 'main.pgstore')
    jobs = _rows(raw, schema, 'jobs')
    attempts = _rows(raw, schema, 'application_attempts')
    submissions = _rows(raw, schema, 'feedback_submissions')
    by_url = {j['url']: j for j in jobs}
    protected = {j['url'] for j in jobs if j.get('applied_at')
                 or j.get('apply_status') not in (None, '', 'failed')
                 or j.get('state') in PROTECTED_STATES}
    protected.update(a['job_url'] for a in attempts
                     if a.get('status') not in SAFE_ATTEMPT_STATES or a.get('submitted_at'))
    protected.update(row['job_url'] for row in submissions)
    by_reservation = {a['attempt_id']: a['job_url'] for a in attempts}
    families: dict[str, set[str]] = {}
    for row in [a for a in attempts if a['job_url'] in protected] + submissions:
        for field in ('snapshot_json', 'artifact_hashes_json', 'features_json', 'uploaded_artifacts_json', 'browser_uploads_json'):
            for path in _artifact_paths(row.get(field)):
                families.setdefault(_family(Path(path)), set()).add(row['job_url'])
    hashes: dict[str, set[str]] = {}
    for job in jobs:
        hashes.setdefault(hashlib.md5(job['url'].encode()).hexdigest()[:8], set()).add(job['url'])
        for column in ('tailored_resume_path', 'cover_letter_path'):
            if job.get(column):
                families.setdefault(_family(Path(job[column]).absolute()), set()).add(job['url'])
    files, contexts = [], []
    protected_context_files = unowned_context_files = 0
    for directory in OUTPUT_DIRS:
        base = root / directory
        if not base.exists():
            continue
        _safe_path(root, directory)
        if directory == 'application-contexts':
            for context in base.iterdir():
                if not context.is_dir() or context.is_symlink() or context.is_junction():
                    unowned_context_files += 1
                    continue
                count = 0
                redirected = False
                for parent, dirs, names in os.walk(context, followlinks=False):
                    safe_dirs = []
                    for name in dirs:
                        child = Path(parent) / name
                        if child.is_symlink() or child.is_junction():
                            redirected = True
                        else:
                            safe_dirs.append(name)
                    dirs[:] = safe_dirs
                    count += len(names)
                owner = by_reservation.get(context.name)
                if scope == 'documents' and owner in protected:
                    protected_context_files += count
                elif redirected or (scope == 'documents' and owner not in by_url):
                    unowned_context_files += count
                else:
                    _safe_path(root, str(context.relative_to(root)))
                    contexts.append({'path': context.relative_to(root).as_posix(), 'kind': 'context',
                                     'owners': [owner] if owner else [], 'file_count': count,
                                     'modified_ns': context.stat().st_mtime_ns})
            continue
        for path in base.rglob('*'):
            safe = _safe_path(root, str(path.relative_to(root)))
            if safe.is_file():
                files.append(safe)
                if path.name.endswith('.approval.json'):
                    proof = _read_json(path, {})
                    owner = proof.get('job_url') if isinstance(proof, dict) else None
                    if owner in by_url:
                        families.setdefault(_family(path), set()).add(owner)
    prior_files = {f['path']: f for f in (previous or {}).get('files', [])}
    report = ResetReport(scope=scope, database=str(root / 'main.pgstore'), old_jobs=len(jobs),
                         preserved_jobs=len(jobs) if scope == 'documents' else 0,
                         pprepared_attempts=len(attempts) if scope == 'documents' else 0,
                         protected_files=protected_context_files, unowned_files=unowned_context_files)
    selected = list(contexts)
    affected = {owner for context in contexts for owner in context['owners']}
    for path in files:
        relative = path.relative_to(root).as_posix()
        owners = set(families.get(_family(path), ()))
        if relative.startswith('application-contexts/'):
            owner = by_reservation.get(path.relative_to(root).parts[1])
            if owner:
                owners.add(owner)
        if not owners:
            for match in re.findall(r'(?:^|_)([a-f0-9]{8})(?=_|\.)', path.name):
                owners.update(hashes.get(match, ()))
        if not owners and relative in prior_files:
            owners.update(u for u in prior_files[relative].get('owners', []) if u in by_url)
        if scope == 'documents':
            if owners & protected:
                report.protected_files += 1
                continue
            if len(owners) != 1:
                report.unowned_files += 1
                continue
        selected.append({'path': relative, 'owners': sorted(owners),
                         'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        affected.update(owners)
    if scope == 'documents':
        affected.update(j['url'] for j in jobs if j['url'] not in protected and
                        (j.get('tailored_resume_path') or j.get('cover_letter_path')))
    report.affected_jobs = len(affected) if scope == 'documents' else len(jobs)
    report.selected_files = sum(item.get('file_count', 1) for item in selected)
    report.removed_jobs = len(jobs) if scope == 'all' else 0
    return report, selected, sorted(affected)


def _clear_documents(raw, schema, urls):
    columns = {r[0] for r in raw.execute('SELECT column_name FROM information_schema.columns '
                                        'WHERE table_schema=%s AND table_name=%s', (schema, 'jobs'))}
    nulls = ('tailored_resume_path', 'tailored_at', 'document_variant',
             'tailor_error', 'cover_letter_path', 'cover_letter_at',
             'cover_error', 'selected_evidence')
    zeros = ('tailor_attempts', 'tailor_deferred_count', 'cover_attempts', 'cover_deferred_count',
             'tailor_repair_pending', 'cover_repair_pending', 'tailor_repair_attempts', 'cover_repair_attempts')
    updates = [identifier(k) + '=NULL' for k in nulls if k in columns]
    updates += [identifier(k) + '=0' for k in zeros if k in columns]
    if 'state' in columns:
        updates.append("state=CASE WHEN state IN ('tailoring','tailored','tailor_failed','cover_writing',"
                       "'cover_failed','ready_to_apply') THEN 'scored' ELSE state END")
    if urls and updates:
        raw.execute('UPDATE ' + identifier(schema) + '.jobs SET ' + ','.join(updates)
                    + ' WHERE url=ANY(%s)', (urls,))


def _delete_files(root, journal_path, journal, report):
    report.phase = 'deleting_files'
    pending = []
    for item in journal['files']:
        path = _safe_path(root, item['path'])
        if not path.exists():
            continue
        try:
            if item.get('kind') == 'context':
                if path.parent != (root / 'application-contexts').resolve():
                    raise ValueError('Context cleanup target must be one owned application context')
                if path.stat().st_mtime_ns != item['modified_ns']:
                    report.skipped_files += item['file_count']
                    pending.append(item)
                    continue
                shutil.rmtree(path)
                report.removed_files += item['file_count']
                continue
            if hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
                report.skipped_files += 1
                pending.append(item)
                continue
            path.unlink()
            report.removed_files += 1
        except OSError:
            report.skipped_files += item.get('file_count', 1)
            pending.append(item)
    report.phase = 'cleanup_pending' if pending else 'complete'
    if pending:
        report.warnings.append('Some files changed or were busy; rerun the same scope to reconcile remaining cleanup.')
    journal.update(phase=report.phase, files=pending, report=report.to_dict())
    atomic_record(journal_path, journal)


def reset_derived_data(root: Path, *, scope: ResetScope, dry_run: bool = False) -> ResetReport:
    if scope not in ('documents', 'all'):
        raise ValueError('Choose reset scope documents or all')
    root = Path(root).expanduser().resolve()
    report = ResetReport(scope=scope, database=str(root / 'main.pgstore'), dry_run=dry_run)
    if dry_run:
        dsn = _read_only_uri(root)
        if not dsn:
            report.inventory_complete = False
            report.warnings.append('PostgreSQL is stopped; read-only database inventory is unavailable. No server was started.')
            return report
        try:
            with postgres.psycopg.connect(dsn, application_name='applypilot-reset-preview',
                                         connect_timeout=5, options='-c default_transaction_read_only=on') as raw:
                report, _, _ = _inventory(root, raw, scope)
                report.dry_run = True
        except postgres.psycopg.Error:
            report.inventory_complete = False
            report.warnings.append('Read-only database inventory is unavailable. No reset was attempted.')
        return report

    from applypilot.control import maintenance, require_offline_maintenance
    require_offline_maintenance(root)
    with maintenance(root):
        dsn = postgres.server_uri()
        schema = postgres.namespace(root / 'main.pgstore')
        journal_path = root / 'resets' / (scope + '.json')
        previous = _read_json(journal_path, {})
        if not isinstance(previous, dict):
            raise ValueError('Reset journal is unreadable; preserve it before retrying')
        if previous.get('phase') == 'complete':
            previous = {}
        if scope == 'all':
            postgres.require_idle()
            if previous.get('phase') == 'prepared' and previous.get('backup'):
                with postgres.psycopg.connect(dsn, application_name='applypilot-admin') as raw:
                    remaining = {r[0] for r in raw.execute('SELECT nspname FROM pg_namespace')}
                if previous.get('dropped_schemas') and not (set(previous['dropped_schemas']) & remaining):
                    previous['phase'] = 'database_committed'
            if previous.get('phase') in {'database_committed', 'deleting_files', 'cleanup_pending', 'initializing'}:
                report = ResetReport(**previous['report'])
                with postgres.psycopg.connect(dsn, application_name='applypilot-admin') as raw:
                    current = _rows(raw, schema, 'jobs')
                references = {_family(Path(j[k])) for j in current for k in
                              ('tailored_resume_path', 'cover_letter_path') if j.get(k)}
                original = previous['files']
                previous['files'] = [item for item in original
                                     if _family(_safe_path(root, item['path'])) not in references]
                report.protected_files += len(original) - len(previous['files'])
                _delete_files(root, journal_path, previous, report)
                _initialize_after_reset(root, journal_path, previous, report)
                return report
        with postgres.psycopg.connect(dsn, application_name='applypilot-admin') as raw:
            lock_key = int.from_bytes(hashlib.sha256((schema + ':application-admission').encode()).digest()[:8], 'big', signed=True)
            raw.execute('SELECT pg_advisory_xact_lock(%s)', (lock_key,))
            if _table_exists(raw, schema, 'jobs'):
                raw.execute('LOCK TABLE ' + identifier(schema) + '.jobs IN EXCLUSIVE MODE')
            if _table_exists(raw, schema, 'application_attempts'):
                raw.execute('LOCK TABLE ' + identifier(schema) + '.application_attempts IN EXCLUSIVE MODE')
            report, files, urls = _inventory(root, raw, scope, previous)
            report.journal = str(journal_path)
            journal = {'scope': scope, 'phase': 'prepared', 'files': files, 'inventory': files,
                       'jobs': urls, 'created_at': datetime.now(timezone.utc).isoformat()}
            if previous.get('backup'):
                journal['previous_backups'] = list(dict.fromkeys([
                    *previous.get('previous_backups', []), previous['backup']]))
            if scope == 'documents':
                atomic_record(journal_path, journal)
                _clear_documents(raw, schema, urls)
            else:
                existing = {r[0] for r in raw.execute('SELECT nspname FROM pg_namespace')}
                stores = [(root / n, postgres.namespace(root / n)) for n in postgres.STORE_PATHS]
                selected = [(p, s) for p, s in stores if s in existing]
                backup = root / 'backups' / ('fresh-reset-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
                backup.mkdir(parents=True, mode=0o700)
                for path, name in selected:
                    postgres.dump_store(path, backup / (name + '.pgdump'))
                report.backup = str(backup)
                report.dropped_schemas = tuple(s for _, s in selected)
                report.removed_jobs = report.old_jobs
                journal['backup'] = report.backup
                journal['dropped_schemas'] = list(report.dropped_schemas)
                journal['report'] = report.to_dict()
                atomic_record(journal_path, journal)
                for _, name in selected:
                    raw.execute('DROP SCHEMA ' + identifier(name) + ' CASCADE')
        report.phase = 'database_committed'
        journal.update(phase=report.phase, report=report.to_dict())
        atomic_record(journal_path, journal)
        _delete_files(root, journal_path, journal, report)
        if scope == 'all':
            _initialize_after_reset(root, journal_path, journal, report)
        return report


def _initialize_after_reset(root, journal_path, journal, report):
    from applypilot import database
    from applypilot.reasoning import learning_store
    final_phase = report.phase
    journal.update(phase='initializing', report=report.to_dict())
    atomic_record(journal_path, journal)
    postgres._ready.clear()
    database._schema_ready.clear()
    learning_store._schema_ready.clear()
    database.init_db(root / 'main.pgstore')
    database.close_connection(root / 'main.pgstore')
    journal.update(phase=final_phase, report=report.to_dict())
    atomic_record(journal_path, journal)
