"""One identified application, one guarded adapter invocation, finite reconciliation.

No worker_loop, batch launcher, admission reservation, recovery queue or browser
membership is used here. The Workflow schedules preparation, submission and
observation separately; only submission may perform the irreversible action.
"""
from __future__ import annotations

import asyncio
import json
from applypilot.database import get_connection, close_connection
from applypilot.execution import check_cancelled, current_task
from applypilot.orchestration.contracts import digest, job_identity


def _identity(value, command):
    expected = job_identity(command['workspace'], value['job_url'])
    if command.get('application_id') != expected or value.get('job_id') != expected:
        raise ValueError('Application identity does not match its immutable job reference')
    return expected


def _result(state, attempt=None, **detail):
    if attempt:
        detail.update(attempt_id=attempt['attempt_id'], operation_id=attempt.get('operation_id') or '')
        detail['channel'] = attempt.get('channel', '')
        if attempt.get('channel') == 'linkedin-easy-apply':
            from .provider_observations import restriction_fact
            if fact := restriction_fact(attempt['attempt_id']):
                detail['provider_restriction'] = fact
    return {'state': state, **detail}


def _selected(value, root, *, prepare=False):
    from .queue import list_actionable_jobs, _queue_predicate
    policy = root['options']
    if prepare:
        from applypilot.discovery.repository import hydrate_job
        from .application_contract import ensure_contract
        clauses, params = _queue_predicate(policy.get('min_score'), policy.get('max_score'),
                                          policy.get('max_age_days'), None)
        conn = get_connection()
        row = conn.execute("SELECT j.*,r.route AS application_route,r.target AS application_url "
            "FROM jobs j JOIN application_routes r ON r.job_url=j.url WHERE j.url=? AND "
            + ' AND '.join('(' + clause + ')' for clause in clauses),
            (value['job_url'], *params)).fetchone()
        if row is None:
            return None
        ensure_contract(conn, hydrate_job(conn, row), profile=root.get('profile'))
    rows = list_actionable_jobs(get_connection(), job_url=value['job_url'], limit=1,
        min_score=policy.get('min_score'), max_score=policy.get('max_score'),
        max_age_days=policy.get('max_age_days'),
        doc_format=policy.get('doc_format', 'pdf'))
    return rows[0] if rows else None


def prepare(value, root, command):
    from .guard import prepare_application_attempt, historical_attempt_status, GuardRejected
    from .email_preflight import preflight_email_route
    from .runtime import RuntimeSelection, select_runtime
    from .provider_policy import enforce, ProviderRestricted
    application_id = _identity(value, command)
    check_cancelled()
    try:
        if root['options'].get('dry_run'):
            return _result('dry_run', eligible=_selected(value, root) is not None)
        attempt_id = command['attempt_id']
        existing = historical_attempt_status(attempt_id)
        if existing:
            if existing.get('application_id') != application_id or existing.get('operation_id') != command['operation_id']:
                raise ValueError('Attempt identity was reused across application operations')
            return inspect(value, root, command)
        job = _selected(value, root, prepare=True)
        if job is None:
            return _result('not_applicable')
        job = preflight_email_route(job, dry_run=False)
        # Preflight reads observed routes only. It does not own execution or send.
        check_cancelled()
        close_connection()
        email = job.get('application_route') == 'email'
        requested = root['options'].get('browser_runtime', 'auto')
        selection = (RuntimeSelection('gmail', 'gmail', 'email application', '') if email else
                     select_runtime('browseros' if requested in {'auto', 'playwright'} else requested))
        try:
            enforce({'target': job['application_url'], 'channel': job['application_route']}, conn=get_connection())
        except ProviderRestricted as exc:
            return _result('provider_restricted',
                reason=exc.reason, retry_after_seconds=exc.retry_after_seconds)
        accepted = prepare_application_attempt(job, attempt_id=attempt_id, workspace=command['workspace'],
            operation_id=command['operation_id'], previous_attempt_id=command.get('previous_attempt_id', ''),
            channel=job['application_route'], target=job['application_url'], browser_runtime=selection.selected)
        return _result('prepared', {'attempt_id': accepted.attempt_id, 'operation_id': command['operation_id'],
                                   'channel': job['application_route']})
    except GuardRejected:
        # A race or protected legacy attempt is an evidence question, not a
        # reason to acquire a new reservation or exhaust a local retry budget.
        return inspect(value, root, command)
    finally:
        close_connection()


def submit(value, root, command):
    from contextlib import nullcontext
    from . import launcher
    from .guard import attempt_status, GuardRejected, record_outcome_unknown
    from .attempt_lifecycle import begin_dispatch, finish_dispatch
    from .runtime import RuntimeSelection, select_runtime
    from applypilot.execution import browser_operation
    application_id = _identity(value, command)
    attempt_id = command['attempt_id']
    if root['options'].get('dry_run'):
        return _result('dry_run')
    execution = current_task().get('key')
    if not execution:
        raise ValueError('Submission must run inside a Temporal Activity')
    invocation_id = digest({'attempt': attempt_id, 'activity': execution})
    began = False
    try:
        check_cancelled()
        began = begin_dispatch(attempt_id, application_id, command['operation_id'], invocation_id,
                               conn=get_connection())
        if not began:
            return inspect(value, root, {**command, 'after_dispatch': True})
        attempt = attempt_status(attempt_id)
        job = json.loads(attempt['snapshot_json'])
        job.update(_attempt_id=attempt_id, _attempt_snapshot=attempt['snapshot_json'],
                   _doc_format=attempt['doc_format'], _queue_policy=json.loads(attempt['queue_policy_json'] or '{}'))
        launcher.set_doc_format(attempt['doc_format'])
        email = attempt['channel'] == 'email'
        if not email and attempt['browser_runtime'] != 'browseros':
            raise GuardRejected('Unfenced live browser transport is not permitted')
        selection = (RuntimeSelection('gmail', 'gmail', 'email application', '') if email else select_runtime('browseros'))
        from applypilot import config
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        # BrowserOS owns an attempt-scoped authenticated session and pages; no
        # Chrome worker matching, global tab cleanup, or listener/pool is started.
        with (nullcontext() if email else browser_operation()):
            check_cancelled()
            if email:
                from .email_application import run_email_job
                result, duration, _questions = run_email_job(job, 0, attempt_id)
            else:
                result, duration, _questions = launcher.run_job(job, port=0, worker_id=0,
                    dry_run=False, runtime_selection=selection, attempt_id=attempt_id)
        if result == 'takeover':
            record_outcome_unknown(attempt_id, reason='Application session ended')
        elif result == 'skipped':
            launcher.end_attempt(value['job_url'], attempt_id)
        elif result not in {'applied', 'guarded_applied', 'guarded_verification'}:
            launcher.mark_result(value['job_url'], 'failed', result.split(':', 1)[-1],
                duration_ms=duration, task_id=attempt_id,
                error_detail=job.get('_failure_detail', ''))
            raise RuntimeError(job.get('_failure_detail') or result)
    except BaseException:
        if began:
            try:
                close_connection()
                launcher.end_attempt(value['job_url'], attempt_id)
            except Exception:
                # Missing acknowledgement is retained as uncertainty. No reset,
                # new ID or re-send is performed here, even during cancellation.
                pass
        raise
    finally:
        close_connection()
        if began:
            try:
                finish_dispatch(attempt_id, invocation_id, conn=get_connection())
            finally:
                close_connection()
    return inspect(value, root, {**command, 'after_dispatch': True})


def inspect(value, root, command):
    from .guard import historical_attempt_status, cancel_before_submit
    from .attempt_lifecycle import safe_to_retry, reconcile_non_submission
    _identity(value, command)
    try:
        if root['options'].get('dry_run'):
            return _result('dry_run', eligible=_selected(value, root) is not None)
        conn = get_connection()
        job = conn.execute('SELECT applied_at,state,apply_status FROM jobs WHERE url=?', (value['job_url'],)).fetchone()
        if job is None:
            return _result('not_applicable')
        if job['applied_at']:
            return _result('submitted')
        requested = command.get('attempt_id')
        attempt = historical_attempt_status(requested, conn=conn) if requested else None
        if requested and attempt is None and command.get('after_dispatch'):
            raise RuntimeError('The dispatched application attempt record is missing')
        if attempt is None:
            row = conn.execute('SELECT * FROM application_attempts WHERE job_url=? '
                               'ORDER BY prepared_at DESC,attempt_id DESC LIMIT 1', (value['job_url'],)).fetchone()
            attempt = dict(row) if row else None
        if attempt is None:
            return _result('not_applicable' if job['state'] == 'archived' else 'not_started')
        if attempt['job_url'] != value['job_url'] or attempt.get('application_id') not in (None, command['application_id']):
            raise ValueError('Observed attempt belongs to another application')
        if attempt['status'] == 'submitted':
            return _result('submitted', attempt)
        if attempt['status'] in {'side_effect_intent', 'verification_required', 'verification_quarantined'}:
            if attempt['channel'] == 'email':
                from .email_application import reconcile_email_application
                if conn.execute('SELECT 1 FROM application_email_deliveries WHERE attempt_id=?',
                                (attempt['attempt_id'],)).fetchone():
                    asyncio.run(reconcile_email_application(attempt['attempt_id'], conn=conn))
            else:
                from .submission_evidence import reconcile_captured_submission
                reconcile_captured_submission(attempt['attempt_id'], conn=conn)
            attempt = historical_attempt_status(attempt['attempt_id'], conn=conn)
            if attempt['status'] == 'submitted':
                return _result('submitted', attempt)
            attempt = reconcile_non_submission(attempt['attempt_id'], conn=conn)
        if safe_to_retry(attempt):
            if job['state'] == 'archived':
                return _result('not_applicable', attempt)
            return _result('confirmed_not_submitted', attempt)
        own = (attempt['attempt_id'] == requested and attempt.get('operation_id') == command.get('operation_id'))
        if own and attempt['status'] == 'prepared' and not attempt.get('adapter_invocation_id'):
            if not command.get('after_dispatch'):
                return _result('prepared', attempt)
            # Fence an Activity that timed out before it reached dispatch. A late
            # task delivery will see cancellation and cannot invoke the adapter.
            cancelled = cancel_before_submit(attempt['attempt_id'], reason='activity_not_dispatched', conn=conn)
            return _result('confirmed_not_submitted' if safe_to_retry(cancelled) else 'outcome_unknown', cancelled)
        # Even a prepared row may belong to a disconnected browser. Age, worker
        # disappearance or a missing email receipt NEVER proves non-submission.
        return _result('outcome_unknown', attempt)
    finally:
        close_connection()


def review(value, root, command):
    from .attempt_lifecycle import apply_review
    from applypilot.orchestration import store
    _identity(value, command)
    if root['options'].get('dry_run'):
        return _result('dry_run')
    try:
        conn = get_connection()
        evidence = store.get(conn, command['review_reference'])
        # A signal can precede the first evidence Activity or Continue-As-New.
        # Resolve its own stable attempt, never whichever row happens to be current.
        attempt_id = evidence.get('attempt_id')
        row = conn.execute('SELECT job_url FROM application_attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
        if row is None or row['job_url'] != value['job_url'] or evidence.get('job_url') != value['job_url']:
            raise ValueError('Review evidence belongs to another posting')
        result = apply_review(attempt_id, command['application_id'], evidence, conn=conn)
        return _result('confirmed_not_submitted', result)
    finally:
        close_connection()
