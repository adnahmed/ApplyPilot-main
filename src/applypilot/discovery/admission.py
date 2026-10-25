"""Fail-closed discovery admission and durable, separately budgeted queues.

Application selection is read-only. Call reserve_allocation inside the SAME
BEGIN transaction as the real application reservation, after its
identity/artifact/attempt/capacity checks. A failed reservation must roll back
that transaction, including the allocation. This module never resets lifecycle.
"""
from __future__ import annotations

from fractions import Fraction
import os
import re


def provisional_scoring_enabled() -> bool:
    """Explicit evaluation-only opt-in; never alters prepare/apply permission."""
    raw = os.getenv("APPLYPILOT_PROVISIONAL_SCORING", "0").strip().lower()
    if raw in {"", "0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    raise ValueError("APPLYPILOT_PROVISIONAL_SCORING must be 0/1 or false/true")


def _alias(alias: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
        raise ValueError("invalid SQL alias")
    return alias


def last_seen_sql(alias: str = "j") -> str:
    j = _alias(alias)
    return (f"COALESCE((SELECT MAX(p.last_seen_at) FROM discovery_postings p "
            f"WHERE p.job_url={j}.url AND p.source_status='open' "
            f"AND p.content_hash={j}.discovery_content_hash "
            f"AND p.source_revision={j}.discovery_source_revision), {j}.discovered_at)")


def current_admission_sql(alias: str = "j", stage: str = "prepare") -> str:
    """SQL expression, not WHERE. Unknown/legacy/content-stale rows fail closed.

    detail/evidence permits absent/unresolved assessments. Scoring evaluates
    described jobs with unresolved role/arrangement. Explicit denials stay
    blocked. Action permission comes from the revisioned effective assessment.
    prepare/apply additionally require a saved eligible score.
    The application guard must additionally validate route and artifact identity.
    """
    j = _alias(alias)
    if stage not in {"detail", "evidence", "score", "prepare", "apply"}:
        raise ValueError(f"unknown admission stage: {stage}")
    current = f"""
        {j}.discovery_assessment_revision IS NOT NULL
        AND {j}.discovery_content_hash IS NOT NULL
        AND {j}.discovery_content_hash = {j}.discovery_assessed_content_hash
        AND {j}.discovery_source_status = 'open'
        AND EXISTS (
            SELECT 1 FROM discovery_active_context ac
            JOIN discovery_contexts dc ON dc.context_key = ac.context_key
            JOIN discovery_assessments da
              ON da.revision = {j}.discovery_assessment_revision
            JOIN discovery_postings dp ON dp.identity_key = da.identity_key
            WHERE ac.singleton_id = 1
              AND dc.context_key = {j}.discovery_context_key
              AND dc.intent_fingerprint = {j}.discovery_intent_fingerprint
              AND dc.profile_version = {j}.discovery_profile_version
              AND dc.rules_version = {j}.discovery_rules_version
              AND da.context_key = dc.context_key
              AND da.content_hash = {j}.discovery_content_hash
              AND da.job_url = {j}.url
              AND dp.job_url = {j}.url
              AND dp.content_hash = da.content_hash
              AND dp.source_status = 'open'
              AND ap_day(dp.last_seen_at) <= ap_day('now', '+5 minutes')
              AND dp.source_revision = da.source_revision
              AND dp.source_revision = {j}.discovery_source_revision
        )
    """
    # Revalidation age schedules source refresh; it is not another job-age
    # policy. Queue callers apply the user's max_age_days independently.
    if stage in {"detail", "evidence"}:
        # No assessment is unknown, not a denial of evidence gathering. A
        # supported block/closure remains a block even when its context is old.
        return f"""(COALESCE({j}.discovery_source_status, 'unknown') NOT IN ('closed')
            AND COALESCE({j}.discovery_disposition, 'needs_evidence') != 'blocked'
            AND COALESCE({j}.discovery_role_status, 'unknown') != 'irrelevant')"""
    if stage == "score":
        # Evaluation answers unresolved relevance/eligibility; requiring those
        # answers before evaluating starves the score service. Never synthesize
        # source evidence or turn this admission into action permission.
        return f"""({current}
            AND EXISTS (SELECT 1 FROM discovery_contexts policy
                WHERE policy.context_key = {j}.discovery_context_key
                  AND ap_json_text(policy.runtime_json, '$.rollout_mode') = 'live')
            AND length(trim(COALESCE({j}.full_description, ''))) > 0
            AND COALESCE({j}.discovery_role_status, 'unknown') IN ('relevant','unknown')
            AND COALESCE({j}.discovery_eligibility, 'unknown') IN ('eligible','unknown')
            AND COALESCE({j}.discovery_disposition, 'needs_evidence')
                IN ('ranked','explore','off_portfolio','needs_evidence'))"""
    from applypilot.targets.store import permit_sql
    target_permission = permit_sql(j) if stage in {'prepare', 'apply'} else '1=1'
    relevant = f"""{current} AND ({target_permission})
        AND EXISTS (SELECT 1 FROM discovery_contexts policy
                    WHERE policy.context_key = {j}.discovery_context_key
                      AND ap_json_text(policy.runtime_json, '$.rollout_mode') = 'live')
        AND {j}.discovery_role_status = 'relevant'
        AND {j}.discovery_eligibility = 'eligible'
        AND {j}.discovery_selected_arrangement IS NOT NULL
        AND {j}.discovery_disposition IN ('ranked','explore','off_portfolio')"""
    if stage == "score":
        return f"({relevant})"
    permission = "apply" if stage == "apply" else "prepare"
    return f"""({relevant}
        AND {j}.eligibility = 'eligible'
        AND {j}.fit_score IS NOT NULL
        AND {j}.discovery_{permission}_allowed = 1
        AND (
            ({j}.discovery_disposition = 'ranked'
             AND {j}.discovery_preference_rank > 0
             AND {j}.discovery_preference_id IS NOT NULL)
            OR {j}.discovery_disposition = 'explore'
            OR ({j}.discovery_disposition = 'off_portfolio' AND EXISTS (
                SELECT 1 FROM discovery_contexts policy
                WHERE policy.context_key = {j}.discovery_context_key
                  AND (policy.allow_off_portfolio_{permission} = 1
                    OR ap_json_flag(policy.runtime_json,'$.admission.fill_idle_capacity') = 1)
            ))
        )
    )"""


def order_sql(alias: str = "j", *, score_expr: str | None = None) -> str:
    """ORDER BY body; apply only after shared admission/lane filtering."""
    j = _alias(alias)
    return (f"{j}.discovery_preference_rank ASC NULLS LAST, "
            f"{j}.discovery_role_tier ASC NULLS LAST, "
            f"{score_expr or (j + '.fit_score')} DESC NULLS LAST, {j}.discovered_at DESC, {j}.url ASC")


def _allocation(conn, context_key, lane, *, lock=False):
    if lane not in {"prepare", "apply"}:
        raise ValueError("Selection lane must be prepare or apply")
    row = conn.execute(
        f"SELECT dc.context_key,dc.{lane}_share,dc.reallocate_unused_to_preferred,"
        "ap_json_flag(dc.runtime_json,'$.admission.fill_idle_capacity') "
        "FROM discovery_active_context ac JOIN discovery_contexts dc ON dc.context_key=ac.context_key "
        "WHERE ac.singleton_id=1 AND (? IS NULL OR dc.context_key=?)", (context_key,context_key)).fetchone()
    return (row[0],Fraction(row[1]),row[2]==1,Fraction(0),row[3]==1) if row else None


def _application_lane_available(conn, *, explore=False, doc_format="pdf") -> bool:
    from applypilot.apply.queue import _queue_predicate, company_capacity
    from applypilot.apply.guard import upload_artifact_paths
    from applypilot.apply.application_contract import missing_requirements, selected_documents
    clauses, params = _queue_predicate(None, None, None, None)
    under_cap = company_capacity(conn)
    disposition = "IN ('explore','off_portfolio')" if explore else "= 'ranked'"
    rows = conn.execute(
        "SELECT j.*,r.target FROM jobs j "
        "JOIN application_routes r ON r.job_url=j.url WHERE " + " AND ".join(clauses)
        + " AND j.discovery_disposition " + disposition + " AND COALESCE(j.state,'') != 'archived'"
        ,
        params).fetchall()
    for row in rows:
        job = dict(row)
        if not under_cap(job['company'], job['site'], job['strategy'], job['target'], job['url']):
            continue
        if missing_requirements(job):
            continue
        try:
            documents = selected_documents(job)
            upload_artifact_paths(documents['resume'], documents['cover_letter'], doc_format, require_resume=False)
            return True
        except (OSError, ValueError):
            continue
    return False


def preferred_lane_available(conn, lane: str, *, doc_format: str = "pdf") -> bool:
    """Check runnable preferred inventory in the reservation's read snapshot."""
    if lane == "apply":
        return _application_lane_available(conn, doc_format=doc_format)
    from applypilot.database import _jobs_by_stage
    return any(_jobs_by_stage(conn=conn, stage=stage, count=True, preferred_only=True)
               for stage in ("pending_tailor", "pending_cover", "pending_brief"))


def _slot_split(credit: Fraction, limit: int, share: Fraction):
    total = credit + limit * share
    explore = max(0, min(limit, int(total)))
    return limit - explore, explore, total - explore


def count_candidates(conn, sql, params, *, lane="prepare", stage=None) -> int:
    """Read-only inventory of candidates in enabled allocation lanes.

    Queue depth is not a fractional batch allocation. Rounding N * share for
    a small N can report zero forever, preventing the worker that advances
    fractional carry from ever being called. A worker still uses select_jobs
    and its real finite budget; this count neither reserves nor spends slots.
    Entirely disabled lanes remain zero. All SQL admission/lifecycle filters
    from the caller are retained.
    """
    from .repository import transaction

    if stage == "score":
        with transaction(conn, immediate=False):
            return int(conn.execute(
                f"SELECT COUNT(*) FROM ({sql.rstrip().rstrip(';')}) j "
                f"WHERE {current_admission_sql(stage='score')}", params).fetchone()[0])

    with transaction(conn, immediate=False):
        allocation = _allocation(conn, None, lane)
        if allocation is None:
            return 0
        key, share, transfer, _credit, fill = allocation
        lanes = []
        if share < 1 or transfer or fill:
            lanes.append("j.discovery_disposition = 'ranked'")
        if share > 0 or fill:
            lanes.append("j.discovery_disposition IN ('explore','off_portfolio')")
        if not lanes:
            return 0
        base = (f"SELECT COUNT(*) FROM ({sql.rstrip().rstrip(';')}) j "
                f"WHERE {current_admission_sql(stage=stage or lane)} "
                "AND j.discovery_context_key = ? AND (" + " OR ".join(lanes) + ")")
        return int(conn.execute(base, (*params, key)).fetchone()[0])


def select_jobs(conn, sql, params, *, limit, context_key=None, lane="prepare",
                stage=None, consume=True, batch_id=None, allocation_limit=None, doc_format="pdf", row_filter=None):
    """Select separate preferred/explore lanes BEFORE LIMIT using exact credit.

    sql must be an unbounded SELECT exposing jobs columns (including url and
    discovery_*), with no ordering/limit. Positional SQL parameters are accepted.
    Preparation persists the finite batch's fractional carry even when no row
    is returned; apply previews never mutate credit. A supplied batch_id reads
    one durable run allowance created by begin_allocation_batch. Unused slots
    are NOT transferred unless active policy says so.
    Scoring spends no action credit. For action queues,
    consume=False is a read-only allocation preview, NOT queue depth (use
    count_candidates for scheduler inventory). Zero is always no slots.
    """
    if type(limit) is not int or limit < 0:
        raise ValueError("limit must be a nonnegative integer")
    if allocation_limit is None:
        allocation_limit = limit
    if type(allocation_limit) is not int or allocation_limit < limit:
        raise ValueError("allocation_limit must be an integer at least as large as limit")
    if batch_id is not None and (not isinstance(batch_id, str) or not batch_id):
        raise ValueError("batch_id must be a nonempty string")
    if limit == 0:
        return []
    from .repository import transaction

    if row_filter is not None and lane != "apply":
        raise ValueError("post-query filtering is only supported for read-only application previews")

    def read_lane(query, arguments, count, ordering):
        if not count:
            return []
        from applypilot.targets.priority import select
        ranked = (f"SELECT j.* FROM ({query}) j LEFT JOIN ("
                  "SELECT l.job_url,MIN(CASE t.priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END) AS rank "
                  "FROM company_targets t JOIN company_target_jobs l ON l.target_id=t.id "
                  "WHERE t.state='active' GROUP BY l.job_url) priority ON priority.job_url=j.url "
                  "WHERE COALESCE(priority.rank,1)=? ORDER BY " + ordering + " LIMIT ? OFFSET ?")
        candidates, priorities = [], {}
        page_size = min(256, max(32, count) if row_filter is not None else count)
        for rank, name in enumerate(("high", "normal", "low")):
            offset, accepted = 0, 0
            while accepted < count:
                size = page_size if row_filter is not None else min(page_size, count - accepted)
                rows = conn.execute(ranked, (*arguments, rank, size, offset)).fetchall()
                for row in rows:
                    if row["url"] in priorities or (row_filter is not None and not row_filter(row)):
                        continue
                    priorities[row["url"]] = name
                    candidates.append(row)
                    accepted += 1
                    if accepted == count:
                        break
                if len(rows) < size:
                    break
                offset += len(rows)
        return select(conn, candidates, count, lane='jobs:' + lane,
                      priority=lambda row: priorities[row['url']],
                      consume=False)

    if stage == "score":
        # Evaluation spends no prepare/apply allocation credit.
        # Keep the caller's age, retries, ownership and lifecycle predicates.
        with transaction(conn, immediate=False):
            return conn.execute(
                f"SELECT j.* FROM ({sql.rstrip().rstrip(';')}) j "
                f"WHERE {current_admission_sql(stage='score')} "
                "AND (? IS NULL OR j.discovery_context_key=?) "
                "ORDER BY (j.discovery_role_status='relevant') DESC, " + order_sql()
                + " LIMIT ?", (*params, context_key, context_key, limit)).fetchall()

    with transaction(conn, immediate=False):
        allocation = _allocation(conn, context_key, lane)
        if allocation is None:
            return []
        key, share, transfer, _credit, fill = allocation
        preferred_slots, explore_slots, _ = _slot_split(Fraction(0), limit, share)
        base = f"SELECT j.* FROM ({sql.rstrip().rstrip(';')}) j WHERE {current_admission_sql(stage=stage or lane)}"
        base += " AND j.discovery_context_key = ?"
        from applypilot.tracking.feedback_learning import register_priority
        learned_score = register_priority(conn)
        ordering = order_sql(score_expr=learned_score)

        capacity = min(limit, preferred_slots + explore_slots)
        preferred = read_lane(
            base + " AND j.discovery_disposition = 'ranked'",
            (*params, key), capacity if fill or transfer else min(capacity, preferred_slots),
            ordering,
        )
        explore = read_lane(
            base + " AND j.discovery_disposition IN ('explore','off_portfolio')", (*params, key),
            capacity if fill else min(capacity, explore_slots),
            ordering,
        )
        if (not fill and lane == "apply" and batch_id is None and limit == 1
                and not preferred and not explore and share > 0):
            explore = read_lane(base + " AND j.discovery_disposition IN ('explore','off_portfolio')",
                                (*params, key), 1, ordering)
            if explore:
                explore_slots = 1
        selected_preferred = list(preferred[:preferred_slots])
        selected_explore = list(explore[:explore_slots])
        result = selected_preferred + selected_explore
        # The caller's query excludes capped, protected and already-running
        # work. Borrow only the part of a lane's allowance it cannot use here;
        # other preparation stages must not keep these worker slots idle.
        if fill and len(selected_preferred) < preferred_slots:
            extra = min(preferred_slots - len(selected_preferred), max(0, capacity - len(result)))
            result.extend(explore[len(selected_explore):len(selected_explore) + extra])
        if (transfer or fill) and len(selected_explore) < explore_slots:
            extra = min(explore_slots - len(selected_explore), max(0, capacity - len(result)))
            result.extend(preferred[len(selected_preferred):len(selected_preferred) + extra])
        result = result[:capacity]
        return result
