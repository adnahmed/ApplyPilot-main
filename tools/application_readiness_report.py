"""Read-only diagnosis for an ApplyPilot workspace. No jobs, browsers or sends.

Run from your checkout: python tools/application_readiness_report.py --output report.json
Imports the CHECKED-OUT admission predicate, not a permissive alternate gate.
This is an inspection report, never a grant to submit an application.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from applypilot import postgres
import sys
import time


def read_options(root):
    path = root / 'autopilot-options.json'
    if not path.exists():
        return {}
    if path.stat().st_size > 100_000:
        raise ValueError('options file exceeds diagnostic limit')
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError('options must be a mapping')
    return value


def collect(root: Path, *, min_score: int = 8, timeout: float = 15, check_files=False,
            doc_format='pdf') -> dict:
    """Return aggregate reasons only: no personal details, URLs or file paths.

    First-blocker counts are a mutually exclusive partition. Independent counts
    overlap and MUST NOT be added. Complete source admission is taken from the
    application's own function; explanatory sub-checks do not replace it.
    """
    from applypilot.discovery.admission import current_admission_sql
    from applypilot.referrals.store import hold_sql

    if type(min_score) is not int or not 0 <= min_score <= 10:
        raise ValueError('min_score must be 0..10')
    if not 0 < timeout <= 60:
        raise ValueError('timeout must be >0 and <=60 seconds per section')
    root = Path(root).expanduser().resolve()
    path = root / 'main.pgstore'
    value = {'format': 'applypilot_application_readiness', 'format_version': 1,
             'captured_at': datetime.now(timezone.utc).isoformat(),
             'read_only': True, 'min_score': min_score, 'check_files': check_files,
             'sections': {}, 'unavailable_sections': [],
             'scope': 'Current local DB snapshot, not retained incident-group totals.',
             'not_checked': ['live browser/provider health', 'submission proof',
                 'company capacities', 'blocked-site policy', 'allocation lane credits',
                 'worker-specific route and maximum-score filters', 'configured attempt ceiling'],
             'privacy': 'No job URLs, descriptions, account identifiers, credentials or document paths exported.'}
    if not check_files:
        value['not_checked'].append('local document existence/format')
    if not path.is_file():
        value['unavailable_sections'].append({'section': 'database', 'reason': 'database_missing'})
        return value
    conn = postgres.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1.5)
    conn.row_factory = postgres.Row
    conn.raw.execute("SET default_transaction_read_only=on")
    conn.set_lock_timeout(1.5)
    conn.execute('BEGIN')

    def section(name, run):
        start = time.monotonic()
        conn.set_progress_handler(lambda: int(time.monotonic() - start > timeout), 5000)
        try:
            data = run()
            value['sections'][name] = {'available': True, 'data': data,
                                       'elapsed_seconds': round(time.monotonic() - start, 3)}
        except (postgres.Error, ValueError, TypeError, KeyError, OSError) as exc:
            # Never leak a raw SQL error, account identifier or a local file path.
            reason = 'query_interrupted' if getattr(exc, 'sqlite_errorcode', None) == postgres.SQLITE_INTERRUPT else type(exc).__name__
            value['sections'][name] = {'available': False, 'reason': reason}
            value['unavailable_sections'].append({'section': name, 'reason': reason})
        finally:
            conn.set_progress_handler(None, 0)

    try:
        section('inventory', lambda: dict(conn.execute('''SELECT COUNT(*) stored_jobs,
            COUNT(full_description) with_description, COUNT(fit_score) scored,
            COALESCE(SUM(CASE WHEN fit_score>=? THEN 1 ELSE 0 END),0) high_fit,
            COALESCE(SUM(CASE WHEN applied_at IS NOT NULL THEN 1 ELSE 0 END),0) submitted,
            COALESCE(SUM(CASE WHEN state='ready_to_apply' THEN 1 ELSE 0 END),0) recorded_ready,
            COUNT(tailored_resume_path) resume_references,
            COUNT(cover_letter_path) cover_references FROM jobs''', (min_score,)).fetchone()))

        def policy():
            row = conn.execute('''SELECT dc.runtime_json FROM discovery_active_context ac
                JOIN discovery_contexts dc ON dc.context_key=ac.context_key WHERE ac.singleton_id=1''').fetchone()
            if row is None:
                return {'active_context_exists': False}
            raw = json.loads(row[0])
            mode = raw.get('rollout_mode')
            out = {'active_context_exists': True, 'rollout_mode': mode if mode in {'live', 'shadow'} else 'unknown'}
            for key in ('worker_ceiling', 'discovery_browser_slots', 'application_browser_slots'):
                v = raw.get(key)
                out[key] = v if type(v) is int else None
            out['buffer'] = {k: v for k, v in raw.get('buffer', {}).items()
                             if k in {'low_watermark', 'high_watermark', 'ready_target', 'revalidate_after_days'} and type(v) is int}
            return out
        section('active_policy', policy)

        # Nonexclusive gates are deliberately named as facts, not guessed causes.
        checks = [
            ('unsubmitted_and_unowned', "j.applied_at IS NULL AND j.agent_id IS NULL AND "
                "(j.apply_status IS NULL OR j.apply_status='failed') AND COALESCE(j.state,'') NOT IN "
                "('applying','applied','archived','verification_required','verification_quarantined')"),
            ('source_reported_open', "j.discovery_source_status='open'"),
            ('source_observation_present', "EXISTS(SELECT 1 FROM discovery_postings p WHERE p.job_url=j.url AND p.source_status='open')"),
            ('source_observation_fresh', "EXISTS(SELECT 1 FROM discovery_postings p WHERE p.job_url=j.url AND p.source_status='open' "
                "AND ap_day(p.last_seen_at)<=ap_day('now','+5 minutes') AND ap_day(p.last_seen_at)>ap_day('now','-' || "
                "COALESCE((SELECT ap_json_number(dc.runtime_json,'$.buffer.revalidate_after_days') FROM discovery_active_context ac "
                "JOIN discovery_contexts dc ON dc.context_key=ac.context_key WHERE ac.singleton_id=1),7) || ' days'))"),
            ('current_context_and_profile', "EXISTS(SELECT 1 FROM discovery_active_context ac JOIN discovery_contexts dc "
                "ON dc.context_key=ac.context_key WHERE ac.singleton_id=1 AND dc.context_key=j.discovery_context_key "
                "AND dc.intent_fingerprint=j.discovery_intent_fingerprint AND dc.profile_version=j.discovery_profile_version "
                "AND dc.rules_version=j.discovery_rules_version)"),
            ('content_assessment_current', "j.discovery_assessment_revision IS NOT NULL AND j.discovery_content_hash IS NOT NULL "
                "AND j.discovery_content_hash=j.discovery_assessed_content_hash"),
            ('role_relevant', "j.discovery_role_status='relevant'"),
            ('discovery_eligibility_supported', "j.discovery_eligibility='eligible'"),
            ('work_arrangement_selected', "j.discovery_selected_arrangement IS NOT NULL"),
            ('portfolio_disposition_accepted', "j.discovery_disposition IN ('ranked','explore','off_portfolio')"),
            ('live_rollout', "EXISTS(SELECT 1 FROM discovery_contexts dc WHERE dc.context_key=j.discovery_context_key "
                "AND ap_json_text(dc.runtime_json,'$.rollout_mode')='live')"),
            ('exact_discovery_score_admission', current_admission_sql('j', 'score')),
        ]
        score_gate_count = len(checks)
        checks.extend([
            ('score_matches_current_assessment', "j.score_error IS NULL AND j.eligibility='eligible' "
                "AND j.score_assessment_revision=j.discovery_assessment_revision "
                "AND j.scored_at IS NOT NULL AND ap_day(j.scored_at)<=ap_day('now')"),
            ('exact_prepare_admission', current_admission_sql('j', 'prepare')),
            ('exact_apply_admission', current_admission_sql('j', 'apply')),
            ('resolved_application_route', "EXISTS(SELECT 1 FROM application_routes r WHERE r.job_url=j.url "
                "AND r.route IN ('external','linkedin-easy-apply','email') AND r.target IS NOT NULL AND r.target!='')"),
            ('no_protected_attempt', "NOT EXISTS(SELECT 1 FROM application_attempts aa WHERE aa.job_url=j.url "
                "AND aa.status IN ('reserved','side_effect_intent','verification_required','verification_quarantined','submitted'))"),
            ('no_active_referral_hold', '(' + hold_sql('j') + ')'),
            ('resume_reference_present', "NULLIF(j.tailored_resume_path,'') IS NOT NULL"),
        ])

        def gates():
            sql = 'SELECT ' + ','.join(f'COALESCE(({clause}),0) AS g{i}' for i, (_, clause) in enumerate(checks))
            sql += ",j.full_description IS NOT NULL AS described,COALESCE(j.fit_score>=?,0) AS high_fit FROM jobs j"
            independent = Counter()
            first_high = Counter()
            first_described = Counter()
            high_fit = described = 0
            for row in conn.execute(sql, (min_score,)):
                flags = [bool(row[f'g{i}']) for i in range(len(checks))]
                for (name, _), passed in zip(checks, flags):
                    if not passed:
                        independent[name] += 1
                if row['high_fit']:
                    high_fit += 1
                    first_high[next((name for (name, _), passed in zip(checks, flags) if not passed), 'structural_checks_passed')] += 1
                if row['described']:
                    described += 1
                    first_described[next((checks[i][0] for i in range(score_gate_count) if not flags[i]), 'discovery_score_admission_passed')] += 1
            return {'first_blocker_high_fit': dict(first_high), 'high_fit_total': high_fit,
                    'first_blocker_described': dict(first_described), 'described_total': described,
                    'independent_failed_checks_all_jobs': {name: independent[name] for name, _ in checks},
                    'first_blocker_order': [name for name, _ in checks],
                    'note': 'First blockers partition each cohort; independent failures overlap. Structural success is not a submission permission or an exact ready-queue count.'}
        section('admission_gates', gates)

        def exact_admission_counts():
            return dict(conn.execute('SELECT ' + ','.join(
                f'COALESCE(SUM(CASE WHEN {current_admission_sql("j", stage)} THEN 1 ELSE 0 END),0) AS "{stage}"'
                for stage in ('detail', 'score', 'prepare', 'apply')) + ' FROM jobs j').fetchone())
        section('exact_discovery_admission_counts', exact_admission_counts)

        def distributions():
            categories = {
                'discovery_role_status': ('relevant','irrelevant','unknown'),
                'discovery_eligibility': ('eligible','ineligible','unknown'),
                'eligibility': ('eligible','ineligible','unknown'),
                'discovery_disposition': ('ranked','explore','off_portfolio','needs_evidence','blocked'),
                'discovery_source_status': ('open','closed','unknown'),
            }
            out = {}
            for col, permitted in categories.items():
                placeholders = ','.join('?' for _ in permitted)
                out[col] = {r[0]: r[1] for r in conn.execute(
                    f"SELECT CASE WHEN {col} IS NULL THEN 'missing' WHEN {col} IN ({placeholders}) THEN {col} ELSE 'other' END k,COUNT(*) n "
                    f'FROM jobs WHERE fit_score>=? GROUP BY k', (*permitted, min_score))}
            return out
        section('high_fit_distributions', distributions)

        if check_files:
            def artifacts():
                from applypilot.apply.guard import upload_artifact_paths
                passed = failed = 0
                for row in conn.execute('SELECT tailored_resume_path,cover_letter_path FROM jobs WHERE fit_score>=?', (min_score,)):
                    # Local file metadata only. No contents, paths or hashes are exported.
                    # Refuse UNC/network paths rather than contacting a remote share.
                    if any(str(p or '').startswith(('\\\\','//')) for p in row):
                        failed += 1
                        continue
                    try:
                        upload_artifact_paths(row[0], row[1], doc_format)
                        passed += 1
                    except (ValueError, OSError):
                        failed += 1
                return {'uploadable_metadata': passed, 'missing_or_unusable_or_network_path': failed,
                        'note': 'Local existence/extension/size only, not content validation or current-revision proof.'}
            section('high_fit_local_artifacts', artifacts)
        return value
    finally:
        conn.set_progress_handler(None, 0)
        conn.rollback()
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=Path(os.environ.get('APPLYPILOT_DIR', Path.home()/'.applypilot')))
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--min-score', type=int)
    parser.add_argument('--timeout', type=float, default=15)
    parser.add_argument('--check-files', action='store_true', help='Inspect local document metadata, never contents.')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    src = args.repo / 'src'
    if (src/'applypilot'/'discovery'/'admission.py').is_file():
        sys.path.insert(0, str(src.resolve()))
    os.environ['APPLYPILOT_DIR'] = str(args.workspace.expanduser().resolve())
    try:
        args.workspace = args.workspace.expanduser().resolve()
        options = read_options(args.workspace)
        score = args.min_score if args.min_score is not None else options.get('min_score', 8)
        doc_format = options.get('doc_format', 'pdf')
        if doc_format not in {'pdf','docx'}:
            doc_format = 'pdf'
        result = collect(args.workspace, min_score=score, timeout=args.timeout,
                         check_files=args.check_files, doc_format=doc_format)
        # Explicitly requested output is the only write. Never overwrite a workspace DB/config.
        output = args.output.expanduser().resolve()
        if output.suffix.lower() != '.json':
            raise ValueError('output must have .json extension')
        if output.exists():
            raise ValueError('output already exists; choose a new report filename')
        with output.open('x', encoding='utf-8') as handle:
            json.dump(result, handle, indent=2)
            handle.write('\n')
    except (OSError, ValueError, ImportError, postgres.Error) as exc:
        print(f'Diagnostic could not finish ({type(exc).__name__}); no workspace repair was attempted.', file=sys.stderr)
        return 1
    print('Read-only application readiness report saved.')
    gates = result.get('sections',{}).get('admission_gates',{})
    if gates.get('available'):
        print('High-fit jobs, grouped by first blocking check:')
        for reason, count in gates['data']['first_blocker_high_fit'].items():
            print(f'  {reason}: {count}')
    if result['unavailable_sections']:
        print('Some sections are unavailable; the report does NOT replace them with zero.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
