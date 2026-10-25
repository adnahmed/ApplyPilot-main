"""Explain idle application selection on a throttled, read-only side thread.

The real selector remains authoritative. This later DB snapshot names failed
checks; it neither claims jobs nor treats a query timeout as an empty queue.
"""
from __future__ import annotations
from collections import Counter, OrderedDict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import re
from applypilot import postgres
import threading
import time
import uuid

from applypilot.operations import worker_journal as journal

LABELS = {
    "application_requirements": "Employer instructions need extraction, a requested document, or a specific applicant contribution",
    "company_posting_suitability": "Employer/posting evidence needs a current suitable assessment",
    "company_research_pending": "Employer/posting evidence is missing, stale, or awaiting automatic research retry",
    "company_posting_review_required": "Employer identity, posting credibility, or engagement remains unresolved",
    "company_posting_unsuitable": "Sourced evidence identifies a closed vacancy, talent pool, or incompatible engagement",
    "automatic_recovery_pending": "Automatic recovery is scheduled; see its next retry time",
    "application_status": "Application status is not pending/retryable",
    "protected_lifecycle": "Job is applying, applied, archived, needs attention, or quarantined",
    "already_applied": "A submission timestamp already exists",
    "prepare_permission": "Current assessment does not allow document preparation",
    "worker_ownership": "Job is already owned by a worker",
    "attempt_budget": "Application attempt budget is exhausted",
    "retry_backoff": "Transient pre-submit failure is waiting for its next retry",
    "provider_admission_wait": "Provider admission is waiting for its recovery condition",
    "minimum_score": "No score at or above this worker's minimum",
    "maximum_score": "Score exceeds this worker's range",
    "score_timestamp": "Valid score timestamp is missing or in the future",
    "job_identity": "Job identity is missing",
    "supported_route": "Supported application route is missing",
    "route_target": "Application route has no destination",
    "application_route_unresolved": "Canonical posting or posting-specific application destination is unresolved",
    "application_route_talent_network": "Destination registers interest in a talent network rather than an identified vacancy",
    "application_route_screening": "Destination is a staffing screening workflow; an employer application route is required",
    "worker_route": "Route belongs to a different worker channel",
    "source_age": "Source evidence exceeds this worker's age limit",
    "blocked_source": "Source is blocked in settings",
    "blocked_job_url": "Job address matches a blocked pattern",
    "blocked_route_url": "Application address matches a blocked pattern",
    "protected_attempt_or_duplicate_target": "Existing or uncertain attempt protects this destination",
    "duplicate_submission_target": "Another job already uses this submission destination",
    "referral_hold": "An active referral-first hold is waiting",
    "score_eligibility": "Scoring has not established eligibility",
    "assessment_missing": "Discovery assessment has not been recorded",
    "active_context_missing": "No active discovery/search context is recorded",
    "context_revision_mismatch": "Assessment does not match the active search/profile/rules",
    "source_evidence_missing": "Matching source posting evidence is missing",
    "source_evidence_stale": "Source observation timestamp is missing or dated in the future",
    "source_revision_mismatch": "Source posting revision no longer matches the assessment",
    "live_rollout_disabled": "This search context is not in live rollout",
    "portfolio_disposition": "Portfolio policy has not admitted this posting",
    "source_not_open": "Current source evidence does not establish an open job",
    "assessment_revision_mismatch": "Content has changed since discovery assessment",
    "role_not_relevant": "Discovery has not established role relevance",
    "discovery_eligibility": "Discovery eligibility needs evidence or is blocked",
    "arrangement_missing": "No eligible work arrangement was selected",
    "score_missing": "No saved score",
    "apply_permission": "Current assessment does not allow applications",
    "current_discovery_admission": "Current context/source/portfolio admission check failed",
    "company_capacity": "Company's configured application capacity is occupied",
    "document_files": "Required upload document is missing or has the wrong format",
    "document_repair_pending": "A required document is being repaired",
    "submission_identity_conflict": "This posting matches an existing or uncertain application",
    "target_circuit_open": "Application target is waiting for its circuit breaker to clear",
    "allocation_or_snapshot_wait": "Allocation or a changed readiness snapshot withheld this package",
    "passes_common_checks": "Passes common queue checks; allocation still applies",
}
_STATE = None
_STATE_LOCK = threading.Lock()


def _admission_reason(stage="apply"):
    if stage not in {"apply", "prepare"}:
        raise ValueError("Unknown admission stage")
    # Explanatory subchecks only, reached after the exact production gate fails.
    # The ELSE retains the full production gate for evidence not represented here.
    return f"""CASE
      WHEN j.discovery_assessment_revision IS NULL THEN 'assessment_missing'
      WHEN COALESCE(j.discovery_source_status,'')!='open' THEN 'source_not_open'
      WHEN j.discovery_content_hash IS NULL OR j.discovery_content_hash IS DISTINCT FROM j.discovery_assessed_content_hash
        THEN 'assessment_revision_mismatch'
      WHEN NOT EXISTS(SELECT 1 FROM discovery_active_context ac JOIN discovery_contexts dc
                      ON dc.context_key=ac.context_key WHERE ac.singleton_id=1) THEN 'active_context_missing'
      WHEN NOT EXISTS(SELECT 1 FROM discovery_active_context ac JOIN discovery_contexts dc
                      ON dc.context_key=ac.context_key WHERE ac.singleton_id=1
                      AND dc.context_key=j.discovery_context_key AND dc.intent_fingerprint=j.discovery_intent_fingerprint
                      AND dc.profile_version=j.discovery_profile_version AND dc.rules_version=j.discovery_rules_version)
        THEN 'context_revision_mismatch'
      WHEN NOT EXISTS(SELECT 1 FROM discovery_assessments da JOIN discovery_postings dp ON dp.identity_key=da.identity_key
                      WHERE da.revision=j.discovery_assessment_revision AND dp.job_url=j.url) THEN 'source_evidence_missing'
      WHEN NOT EXISTS(SELECT 1 FROM discovery_assessments da JOIN discovery_postings dp ON dp.identity_key=da.identity_key
                      WHERE da.revision=j.discovery_assessment_revision AND dp.job_url=j.url
                      AND ap_day(dp.last_seen_at)<=ap_day('now','+5 minutes'))
        THEN 'source_evidence_stale'
      WHEN NOT EXISTS(SELECT 1 FROM discovery_assessments da JOIN discovery_postings dp ON dp.identity_key=da.identity_key
                      WHERE da.revision=j.discovery_assessment_revision AND dp.job_url=j.url
                      AND dp.source_status='open' AND dp.content_hash=da.content_hash
                      AND da.content_hash=j.discovery_content_hash AND dp.source_revision=da.source_revision
                      AND dp.source_revision=j.discovery_source_revision) THEN 'source_revision_mismatch'
      WHEN NOT EXISTS(SELECT 1 FROM discovery_contexts dc WHERE dc.context_key=j.discovery_context_key
                      AND ap_json_text(dc.runtime_json,'$.rollout_mode')='live') THEN 'live_rollout_disabled'
      WHEN COALESCE(j.discovery_disposition,'') NOT IN ('ranked','explore','off_portfolio') THEN 'portfolio_disposition'
      WHEN COALESCE(j.discovery_role_status,'')!='relevant' THEN 'role_not_relevant'
      WHEN COALESCE(j.discovery_eligibility,'')!='eligible' THEN 'discovery_eligibility'
      WHEN j.discovery_selected_arrangement IS NULL THEN 'arrangement_missing'
      WHEN j.fit_score IS NULL THEN 'score_missing'
      WHEN COALESCE(j.discovery_{stage}_allowed,0)!=1 THEN '{stage}_permission'
      ELSE 'current_discovery_admission' END"""


def _ordered_checks(policy, conn=None):
    from .queue import queue_checks
    checks = queue_checks(policy.get('min_score'), policy.get('max_score'),
                          policy.get('max_age_days'), policy.get('route_kind'), conn=conn)
    # Readability only: this reorders the explanation, not the worker's filters.
    order = ['already_applied','application_status','protected_lifecycle','worker_ownership','attempt_budget','retry_backoff',
             'minimum_score','maximum_score','score_timestamp','job_identity','supported_route',
             'route_target','worker_route','blocked_source','blocked_job_url','blocked_route_url',
             'protected_attempt_or_duplicate_target','duplicate_submission_target','referral_hold',
             'current_discovery_admission','score_eligibility','source_age']
    rank = {v:i for i,v in enumerate(order)}
    return sorted(checks, key=lambda v:rank.get(v[0],99))


def inspect_selection(db_path, policy, *, sample_limit=12):
    """One nonmutating snapshot. Counts partition the minimum-score cohort.

    File checks use the same cheap path validation as SQL selection, not document
    hashing or model/browser calls. Final reservation always rechecks everything.
    Rows are bounded; SQL completion is not a timing policy.
    """
    from applypilot import config
    from .queue import delivery_blocker
    from applypilot.discovery.admission import _allocation, select_jobs
    started = time.monotonic()
    value = {'available': False, 'captured_at': datetime.now(timezone.utc).isoformat(),
             'scope': 'Jobs at or above this worker minimum score, or its explicit target; first blockers partition this cohort',
             'read_only': True, 'sample_limit': sample_limit, 'samples_truncated': False,
             'snapshot_not_selection': True, 'doc_format': policy.get('doc_format','pdf'),
             'worker_policy': {'score_min': policy.get('min_score') if policy.get('min_score') is not None else config.DEFAULTS['min_score'],
                               'score_max':policy.get('max_score'), 'age_days':policy.get('max_age_days') if policy.get('max_age_days') is not None else config.DEFAULTS['max_job_age_days'],
                               'route_kind':policy.get('route_kind'), 'attempt_limit':config.DEFAULTS['max_apply_attempts'],
                               'explicit_target':policy.get('target_url') is not None}}

    conn = None
    try:
        path = Path(db_path).resolve()
        conn = postgres.connect(path.as_uri()+'?mode=ro', uri=True, timeout=0.1)
        conn.row_factory = postgres.Row
        conn.raw.execute("SET default_transaction_read_only=on")
        conn.execute('BEGIN')
        total = conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
        checks = _ordered_checks(policy, conn)
        blocker = delivery_blocker(conn, policy.get('doc_format', 'pdf'))
        cases, args = [], []
        for code, sql, params in checks:
            why = _admission_reason() if code == 'current_discovery_admission' else "'"+code+"'"
            if code == 'company_posting_suitability':
                from applypilot.integrations.research import posting_suitability_reason_sql
                why = posting_suitability_reason_sql("j")
            cases.append(f'WHEN NOT COALESCE(({sql}),FALSE) THEN {why}')
            args.extend(params)
        expr = "CASE " + ' '.join(cases) + " ELSE 'passes_common_checks' END"
        target = policy.get('target_url')
        threshold = policy.get('min_score')
        if threshold is None: threshold=config.DEFAULTS['min_score']
        where, cohort_params = ('(j.url=? OR r.target=?)',(target,target)) if target is not None else ('j.fit_score>=?',(threshold,))
        base = f' FROM jobs j LEFT JOIN application_routes r ON r.job_url=j.url WHERE {where}'
        job_columns = {row[1] for row in conn.table_info('jobs')}
        assumptions_column = ("j.discovery_assumptions_json" if "discovery_assumptions_json" in job_columns
                              else "NULL")
        results = conn.execute(f'SELECT j.*,r.target AS trace_target,r.route AS trace_route,{assumptions_column} AS assumptions_json,{expr} AS blocker'+base+' ORDER BY j.url LIMIT 5001',
                               (*args,*cohort_params)).fetchall()
        if len(results) > 5000:
            raise ValueError("Diagnostic cohort exceeds the 5000-row bound; no complete count published")
        results = [dict(row) for row in results]
        for row in results:
            if row['blocker'] != 'passes_common_checks':
                continue
            row['blocker'] = blocker({**row, 'application_url': row['trace_target'],
                                     'application_route': row['trace_route']}) or 'passes_common_checks'
        counts = Counter(row['blocker'] for row in results)
        assumptions = Counter()
        assumption_jobs = 0
        for row in results:
            try:
                values = json.loads(row['assumptions_json']) if row['assumptions_json'] else []
            except (TypeError, ValueError):
                values = []
            safe = set()
            for item in values if isinstance(values, list) else ():
                if isinstance(item, str):
                    code = item
                elif isinstance(item, dict):
                    reason, requirement = item.get('reason'), item.get('requirement')
                    code = ':'.join(v for v in (reason, requirement) if isinstance(v, str))
                else:
                    continue
                if re.fullmatch(r'[a-z][a-z0-9_.:-]{0,79}', code):
                    safe.add(code)
            if safe:
                assumption_jobs += 1
                assumptions.update(safe)
        samples = []
        for row in results:
            if row['blocker']=='passes_common_checks': continue
            if len(samples)>=sample_limit:break
            samples.append({'job_id':journal.job_id(row['url']), 'job_title': row['title'],
                            'company': row['company'], 'reason':row['blocker']})
        candidates = [row['url'] for row in results if row['blocker']=='passes_common_checks']
        allocation = _allocation(conn, None, 'apply')
        allocation_info = {'active_context': allocation is not None}
        if allocation:
            _, share, transfer, credit, fill = allocation
            allocation_info.update(exploration_share=str(share), available_credit=str(credit),
                                   reallocate_unused_to_preferred=transfer,
                                   fill_idle_capacity=fill,
                                   finite_batch=policy.get('allocation_batch_id') is not None)
        # This is the actual read-only allocation function, not another copy of its math.
        chosen = []
        if candidates:
            candidate_set = set(candidates)
            # No SQL parameter ceiling for large cohorts. Read selected admission
            # against the same production predicates in this transaction instead.
            sqlchecks = ' AND '.join('('+sql+')' for _,sql,_ in checks)
            flat = [p for _,_,pp in checks for p in pp]
            query = 'SELECT j.* FROM jobs j LEFT JOIN application_routes r ON r.job_url=j.url WHERE '+sqlchecks
            if target is not None:
                query+=' AND (j.url=? OR r.target=?)';flat.extend([target,target])
            budget = policy.get('allocation_batch_limit')
            if budget != 0:
                chosen = select_jobs(conn,query,flat,limit=1,lane='apply',consume=False,
                                     batch_id=policy.get('allocation_batch_id'),allocation_limit=budget,
                                     row_filter=lambda row: row['url'] in candidate_set)
        value.update(available=True, stored_jobs=total, cohort_count=len(results),
                     first_blockers=dict(counts), passing_common_checks=len(candidates),
                     allocated_preview=len(chosen), allocation=allocation_info, samples=samples,
                     optimistic_assumption_jobs=assumption_jobs,
                     optimistic_assumptions=dict(assumptions),
                     samples_truncated=sum(n for k,n in counts.items() if k!='passes_common_checks')>len(samples))
        if chosen:
            value['reason']='candidate_available_in_later_snapshot'
        elif candidates:
            value['reason']='allocation_wait'
        elif counts:
            value['reason']=counts.most_common(1)[0][0]
        else:
            value['reason']='no_jobs_in_score_or_target_cohort'
    except Exception as exc:
        value.update(available=False, reason='diagnostic_unavailable', exception_type=type(exc).__name__)
        # No incomplete scan counts masquerading as complete results.
    finally:
        if conn is not None:
            conn.rollback();conn.close()
        value['elapsed_seconds']=round(time.monotonic()-started,3)
    return value


class Diagnostics:
    def __init__(self, root, db_path):
        self.root, self.db_path, self.pid = Path(root), Path(db_path), os.getpid()
        self.lock=threading.Lock();self.states=OrderedDict();self.polls={}
        self.queue=queue.Queue(maxsize=32)
        self.thread=threading.Thread(target=self._run, name='application-selection-diagnostics', daemon=True)
        self.thread.start()

    def observe(self, worker_id, policy, selection_id, selected, elapsed, *, error=None):
        now=time.time();mono=time.monotonic()
        policy_id=hashlib.sha256(json.dumps(policy,sort_keys=True,default=str).encode()).hexdigest()[:24]
        run_id=os.environ.get('APPLYPILOT_OPS_RUN_ID','')
        key=(run_id,policy_id)
        with self.lock:
            state=self.states.setdefault(key, {'id':uuid.uuid4().hex,'last':-float('inf'),'pending':False})
            self.states.move_to_end(key)
            if len(self.states)>64:self.states.popitem(last=False)
            poll=self.polls.setdefault((run_id,worker_id), {'polls':0,'empty':0})
            poll['polls']+=1;poll['empty']=poll['empty']+1 if not selected and error is None else 0
            if len(self.polls)>256:self.polls.pop(next(iter(self.polls)))
            due = not selected and error is None and not state['pending'] and mono-state['last']>=60
            if due:
                state.update(pending=True,last=mono,id=uuid.uuid4().hex)
                try:self.queue.put_nowait((key,dict(policy),state['id']))
                except queue.Full:state['pending']=False
            out={'selection_id':selection_id,'policy_id':policy_id,'diagnostic_id':state['id'],
                 'polls':poll['polls'],'empty_polls':poll['empty'],'last_checked_at':now,
                 'selection_result':'error' if error else 'selected' if selected else 'empty'}
            should_log=bool(selected or error or poll['empty']==1 or mono-poll.get('logged',-float('inf'))>=60)
            if should_log:poll['logged']=mono
        if should_log:
            journal.emit('selection.'+out['selection_result'],worker_id=worker_id,**{k:v for k,v in out.items() if k!='selection_result'},
                         stage='applications',elapsed_seconds=round(elapsed,3),
                         job_id=journal.job_id(selected.get('url')) if selected else '',
                         job_title=selected.get('title') if selected else '',
                         reason=type(error).__name__ if error else 'no_candidate_returned' if not selected else 'candidate_selected',
                         exception_type=type(error).__name__ if error else None)
        return out

    def _run(self):
        while True:
            key,policy,diagnostic_id=self.queue.get()
            try:
                snapshot=inspect_selection(self.db_path,policy)
                payload={'format_version':1,'run_id':key[0],'policy_id':key[1],
                         'diagnostic_id':diagnostic_id,'snapshot':journal._safe(snapshot)}
                journal.emit('selection.explained',run_id=key[0],stage='applications',
                             policy_id=key[1],diagnostic_id=diagnostic_id,snapshot=snapshot)
                from applypilot.web.runtime import atomic_json
                folder=self.root/'operations'/'selection-snapshots'
                if any(p.is_symlink() for p in (self.root,self.root/'operations',folder)):
                    raise OSError('symlink diagnostics directory')
                atomic_json(folder/(key[1]+'.json'),payload)
                with self.lock:
                    if key in self.states:self.states[key]['snapshot']=snapshot
                # Only these bounded diagnostic files, never work/attempt state.
                for path in folder.glob('*.json'):
                    if re.fullmatch(r'[a-f0-9]{24}\.json',path.name) and not path.is_symlink() and time.time()-path.stat().st_mtime>86400:
                        path.unlink(missing_ok=True)
            except Exception:
                journal._warn()
            finally:
                with self.lock:
                    if key in self.states:self.states[key]['pending']=False
                self.queue.task_done()


def observe(worker_id, policy, selection_id, selected, elapsed, *, error=None):
    """Called after selection. Enqueues at most one scan per policy/minute."""
    global _STATE
    try:
        from applypilot import config
        if not journal.enabled(): return None
        with _STATE_LOCK:
            if _STATE is None or _STATE.pid!=os.getpid() or _STATE.root!=Path(config.APP_DIR):
                _STATE=Diagnostics(config.APP_DIR,config.DB_PATH)
            manager=_STATE
        result=manager.observe(worker_id,policy,selection_id,selected,elapsed,error=error)
        from .dashboard import update_state
        update_state(worker_id,selection_checked_at=result['last_checked_at'],selection_polls=result['polls'],
                     selection_empty_polls=result['empty_polls'],selection_result=result['selection_result'],
                     selection_policy_id=result['policy_id'],selection_diagnostic_id=result['diagnostic_id'])
        return result
    except Exception:
        journal._warn()
        return None
