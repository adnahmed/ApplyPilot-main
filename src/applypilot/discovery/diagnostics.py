"""Read-only explanations for saved descriptions that cannot enter scoring.

First matching blocker per raw candidate. This is diagnostic evidence, not a
replacement for the dispatch predicate, allocation policy, or job reservation.
"""
from __future__ import annotations

from .admission import current_admission_sql


def score_admission_sql(*, assumptions=False):
    current = current_admission_sql("j", "score")
    assumption_case = ("WHEN CASE WHEN ap_json_valid(COALESCE(j.discovery_assumptions_json,'')) "
                       "THEN ap_json_array_length(j.discovery_assumptions_json) ELSE 0 END > 0 "
                       "THEN 'evaluation_admitted_with_optimistic_assumptions'" if assumptions else "")
    return f"""
    WITH context AS (
      SELECT ac.context_key,
             ap_json_text(dc.runtime_json, '$.rollout_mode') AS rollout
      FROM discovery_active_context ac JOIN discovery_contexts dc ON dc.context_key=ac.context_key
      WHERE ac.singleton_id=1
    ), candidates AS (
      SELECT j.* FROM jobs j WHERE full_description IS NOT NULL AND fit_score IS NULL
    ), reasons AS (
      SELECT CASE
        WHEN NOT EXISTS (SELECT 1 FROM context) THEN 'no_active_context'
        WHEN COALESCE((SELECT rollout FROM context),'')!='live' THEN 'rollout_not_live'
        WHEN NOT EXISTS (SELECT 1 FROM discovery_postings dp WHERE dp.job_url=j.url) THEN 'missing_source_observation'
        WHEN COALESCE(j.discovery_source_status,'unknown')!='open' THEN 'source_not_confirmed_open'
        WHEN NOT EXISTS (SELECT 1 FROM discovery_postings dp WHERE dp.job_url=j.url
             AND dp.source_status='open' AND ap_day(dp.last_seen_at)<=ap_day('now','+5 minutes'))
             THEN 'invalid_source_observation_timestamp'
        WHEN j.discovery_context_key IS DISTINCT FROM (SELECT context_key FROM context) THEN 'assessment_for_old_context'
        WHEN j.discovery_assessment_revision IS NULL OR j.discovery_content_hash IS NULL
             OR j.discovery_content_hash IS DISTINCT FROM j.discovery_assessed_content_hash THEN 'missing_or_outdated_assessment'
        WHEN COALESCE(j.discovery_role_status,'unknown') NOT IN ('relevant','unknown')
             THEN 'role_explicitly_excluded'
        WHEN COALESCE(j.discovery_eligibility,'unknown') NOT IN ('eligible','unknown')
             THEN 'eligibility_explicitly_excluded'
        WHEN COALESCE(j.discovery_disposition,'needs_evidence')
             NOT IN ('ranked','explore','off_portfolio','needs_evidence')
             THEN 'portfolio_admission_blocked'
        WHEN ({current}) IS NOT TRUE THEN 'source_or_assessment_revision_mismatch'
        WHEN j.applied_at IS NOT NULL OR j.agent_id IS NOT NULL
             OR COALESCE(j.apply_status,'failed')!='failed'
             OR COALESCE(j.state,'') IN ('applying','applied',
                                        'verification_required','verification_quarantined','archived')
             THEN 'application_lifecycle_held'
        {assumption_case}
        ELSE 'evaluation_admitted_check_age_and_reservations'
      END AS reason FROM candidates j
    ) SELECT reason,COUNT(*) AS jobs FROM reasons GROUP BY reason ORDER BY jobs DESC,reason
    """


def scoring_breakdown(conn):
    columns = {row[1] for row in conn.table_info('jobs')}
    rows = [dict(zip(("reason", "jobs"), row)) for row in
            conn.execute(score_admission_sql(assumptions="discovery_assumptions_json" in columns))]
    return {"available": True, "raw_candidates": sum(row["jobs"] for row in rows),
            "reasons": rows, "policy_label": "described jobs within the selected age limit may be evaluated; explicit exclusions still block",
            "optimistic_assumption_jobs": sum(row["jobs"] for row in rows
                                               if row["reason"] == "evaluation_admitted_with_optimistic_assumptions"),
            "predicate_source": "applypilot.discovery.admission.current_admission_sql(stage='score')",
            "note": "First diagnostic reason per raw scoring candidate. Recorded optimistic assumptions explain evaluation provenance; they grant no preparation, application, or outreach permission. Eligible queues also check age and reservations. Saved totals are not runnable work."}
