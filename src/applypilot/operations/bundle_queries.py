"""Fixed, read-only support inventories. No user-supplied SQL or side effects.

Raw queues intentionally reproduce the earlier manual field-count snapshot
(at score 8). They are NOT dispatchable counts. See admitted_queues.json and
full task/assessment/admission tables for gates. Time eligibility is not a lease.
"""
from applypilot.backlog import MAX_BRIEF_RETRIES, MAX_SCORE_RETRIES

FIELDS = ("description", "full_description", "detail_scraped_at", "detail_error", "fit_score",
          "scored_at", "score_error", "tailored_resume_path", "cover_letter_path",
          "interview_brief_path", "application_url", "eligibility", "applied_at", "apply_status")
MAIN_QUERIES = {
    # A support sample must still be produced on a multi-gigabyte inventory.
    # Exact population counts belong to the cached storage census; this fixed
    # first-N sample has no full-table DISTINCT or sort.
    "source_postings": """
        WITH sample AS (
          SELECT provider,source_status,job_url,first_seen_at,last_seen_at,source_revision
          FROM discovery_postings LIMIT 20000
        )
        SELECT provider,source_status,COUNT(*) AS sampled_postings,
               COUNT(DISTINCT job_url) AS sampled_distinct_jobs,
               MIN(first_seen_at) AS sampled_first_seen_at,
               MAX(last_seen_at) AS sampled_last_seen_at,
               MAX(source_revision) AS sampled_max_source_revision,
               20000 AS sample_limit,1 AS bounded_sample
        FROM sample GROUP BY 1,2
    """,
    # Keep raw stage scans independent: a slow historical count can be marked
    # unavailable without suppressing every other queue report.
    "raw_queue_enrich_not_dispatchable": "SELECT 'enrich' AS queue,COUNT(*) AS jobs,1 AS raw_not_dispatchable FROM jobs WHERE detail_scraped_at IS NULL OR detail_error_category='retriable'",
    "raw_queue_score_not_dispatchable": f"SELECT 'score' AS queue,COUNT(*) AS jobs,1 AS raw_not_dispatchable FROM jobs WHERE full_description IS NOT NULL AND fit_score IS NULL",
    "raw_queue_tailor_score8_not_dispatchable": "SELECT 'tailor' AS queue,COUNT(*) AS jobs,8 AS fixed_score,1 AS raw_not_dispatchable FROM jobs WHERE fit_score>=8 AND full_description IS NOT NULL AND tailored_resume_path IS NULL AND eligibility='eligible'",
    "raw_queue_cover_score8_not_dispatchable": "SELECT 'cover' AS queue,COUNT(*) AS jobs,8 AS fixed_score,1 AS raw_not_dispatchable FROM jobs WHERE fit_score>=8 AND tailored_resume_path IS NOT NULL AND full_description IS NOT NULL AND (cover_letter_path IS NULL OR cover_letter_path='') AND eligibility='eligible'",
    "raw_queue_brief_score8_not_dispatchable": f"SELECT 'brief' AS queue,COUNT(*) AS jobs,8 AS fixed_score,1 AS raw_not_dispatchable FROM jobs WHERE fit_score>=8 AND full_description IS NOT NULL AND (interview_brief_path IS NULL OR interview_brief_path='') AND COALESCE(brief_attempts,0)<{MAX_BRIEF_RETRIES} AND eligibility='eligible'",
    "raw_queue_apply_score8_not_dispatchable": "SELECT 'apply' AS queue,COUNT(*) AS jobs,8 AS fixed_score,1 AS raw_not_dispatchable FROM jobs WHERE fit_score>=8 AND tailored_resume_path IS NOT NULL AND applied_at IS NULL AND application_url IS NOT NULL AND eligibility='eligible'",
    "database_census": "SELECT ap_utc(EXTRACT(EPOCH FROM CURRENT_TIMESTAMP)) AS captured_utc,version() AS postgres_version,COUNT(*) AS total_jobs,MIN(discovered_at) AS oldest_discovered_at,MAX(discovered_at) AS newest_discovered_at FROM jobs",
    "jobs_by_state": "SELECT state,apply_status,COUNT(*) AS jobs FROM jobs GROUP BY state,apply_status",
    "materialized_fields": "SELECT COUNT(*) AS total_jobs," + ",".join(
        f"SUM(CASE WHEN {f} IS NOT NULL AND TRIM(CAST({f} AS TEXT))<>'' THEN 1 ELSE 0 END) AS has_{f}" for f in FIELDS) + " FROM jobs",
    "discovery_gates": "SELECT discovery_context_key,discovery_role_status,discovery_eligibility,discovery_disposition,discovery_prepare_allowed,discovery_apply_allowed,COUNT(*) AS jobs FROM jobs GROUP BY 1,2,3,4,5,6",
    "active_discovery_gates": "SELECT discovery_role_status,discovery_eligibility,discovery_disposition,discovery_prepare_allowed,discovery_apply_allowed,COUNT(*) AS jobs FROM jobs WHERE discovery_context_key=(SELECT context_key FROM discovery_active_context WHERE singleton_id=1) GROUP BY 1,2,3,4,5",
    "applications": "SELECT status,channel,browser_runtime,COUNT(*) AS attempts,COUNT(DISTINCT job_url) AS jobs,COUNT(DISTINCT target) AS targets,COUNT(DISTINCT worker_id) AS workers,MIN(prepared_at) AS oldest_prepared_at,MAX(updated_at) AS latest_update FROM application_attempts GROUP BY 1,2,3",
    "application_routes": "SELECT r.route,j.state,j.apply_status,COUNT(*) AS jobs FROM application_routes r JOIN jobs j ON j.url=r.job_url GROUP BY 1,2,3",
}
OPERATIONS_QUERIES = {
    "process_liveness": "SELECT *,ap_utc(heartbeat) AS heartbeat_utc,ROUND(CAST(EXTRACT(EPOCH FROM CURRENT_TIMESTAMP) AS DOUBLE PRECISION)-heartbeat,1) AS heartbeat_age_seconds,CASE WHEN running=0 THEN 'stopped' WHEN CAST(EXTRACT(EPOCH FROM CURRENT_TIMESTAMP) AS DOUBLE PRECISION)-heartbeat>120 THEN 'stale' ELSE 'recent_heartbeat' END AS reported_liveness FROM processes ORDER BY heartbeat DESC",
    "active_spans": "SELECT a.*,p.pid,p.role,p.running,p.heartbeat,ap_utc(a.started_at) AS started_utc,ROUND(CAST(EXTRACT(EPOCH FROM CURRENT_TIMESTAMP) AS DOUBLE PRECISION)-a.started_at,1) AS age_seconds FROM active a LEFT JOIN processes p ON p.id=a.process_id ORDER BY a.started_at",
    "finishes_last_60_minutes": "SELECT stage,operation,outcome,COUNT(*) AS finishes,COUNT(DISTINCT process_id) AS processes,COUNT(DISTINCT NULLIF(job_id,'')) AS distinct_jobs,ROUND(SUM(duration),1) AS summed_span_seconds,ROUND(AVG(duration),2) AS avg_span_seconds,ROUND(MAX(duration),2) AS max_span_seconds FROM events WHERE kind='finish' AND at>=CAST(EXTRACT(EPOCH FROM CURRENT_TIMESTAMP) AS DOUBLE PRECISION)-3600 GROUP BY 1,2,3",
    "event_window": "SELECT COUNT(*) AS retained_events,MIN(at) AS earliest_at,MAX(at) AS latest_at,ap_datetime('now') AS captured_utc FROM events",
}

# The dashboard and support export share the same non-mutating explanation.
from applypilot.discovery.diagnostics import score_admission_sql
MAIN_QUERIES["score_admission_reasons"] = score_admission_sql()
