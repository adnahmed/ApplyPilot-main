"""Read-only, per-job reconciliation of high-fit preparation/application queues."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from applypilot import postgres
import time

from applypilot import config


def _first_reasons(conn, checks, threshold, *, routes=False, stage="prepare"):
    from applypilot.apply.selection_diagnostics import _admission_reason
    clauses, values = [], []
    for reason, sql, params in checks:
        expression = _admission_reason(stage) if reason == "current_discovery_admission" else "'" + reason + "'"
        clauses.append(f"WHEN NOT COALESCE(({sql}),FALSE) THEN {expression}")
        values.extend(params)
    expression = "CASE " + " ".join(clauses) + " ELSE 'passes_checks' END"
    join = " LEFT JOIN application_routes r ON r.job_url=j.url" if routes else ""
    rows = conn.execute(f"SELECT j.url,{expression} AS reason FROM jobs j{join} WHERE j.fit_score>=?",
                        (*values, threshold)).fetchall()
    return {row["url"]: row["reason"] for row in rows}


def _stage_result(reasons, selected, *, eligible_queue_count=None):
    mismatches = []
    for url in selected:
        if reasons.get(url) not in {"passes_checks", "ready"}:
            mismatches.append(url)
        reasons[url] = "ready"
    counts = dict(sorted(Counter(reasons.values()).items()))
    return {"first_blockers": counts, "selectable_preview": len(selected),
            "eligible_queue_count": eligible_queue_count,
            "accounted_jobs": sum(counts.values()), "selector_mismatches": len(mismatches)}


def inspect_blockers(db_path, *, min_score=None, max_age_days=None, doc_format="pdf",
                     route_kind=None) -> dict:
    """Use one read transaction; never initialize/migrate, claim, or spend credit.

    Each stage has its OWN partition of the same cohort. Preview dispatch budget
    is the cohort size; queue depth and fractional dispatch allowance are distinct.
    Process-local in-flight reservations are not present in an offline database.
    Missing schema or failed storage returns unavailable, never a fabricated zero.
    """
    threshold = config.DEFAULTS["min_score"] if min_score is None else min_score
    age = config.DEFAULTS["max_job_age_days"] if max_age_days is None else max_age_days
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not 0 <= threshold <= 10:
        raise ValueError("min_score must be between 0 and 10")
    if type(age) is not int or age < 0:
        raise ValueError("max_age_days must be a nonnegative integer")
    if doc_format not in {"pdf", "docx"}:
        raise ValueError("doc_format must be pdf or docx")
    started = time.monotonic()
    report = {"available": False, "read_only": True, "format_version": 1,
              "captured_at": datetime.now(timezone.utc).isoformat(),
              "policy": {"min_score": threshold, "max_age_days": age,
                         "doc_format": doc_format, "route_kind": route_kind},
              "scope": "All stored jobs at or above min_score, including archived/applied/ineligible jobs",
              "snapshot_not_live_dispatch": True,
              "in_memory_reservations_included": False}
    conn = None
    try:
        path = Path(db_path).resolve(strict=True)
        conn = postgres.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.2)
        conn.row_factory = postgres.Row
        conn.raw.execute("SET default_transaction_read_only=on")
        conn.execute("BEGIN")
        cohort = [dict(row) for row in conn.execute(
            "SELECT j.*,r.target AS route_target "
            "FROM jobs j LEFT JOIN application_routes r ON r.job_url=j.url WHERE fit_score>=? ORDER BY j.url", (threshold,))]
        total = len(cohort)
        per_job = {row["url"]: {"url": row["url"], "title": row["title"],
                                "company": row["company"], "fit_score": row["fit_score"]} for row in cohort}
        stages = {}
        from applypilot.database import _jobs_by_stage, preparation_checks
        from applypilot.discovery.admission import select_jobs, count_candidates
        for stage in ("tailor", "cover"):
            reasons = _first_reasons(conn, preparation_checks(stage, threshold, age), threshold)
            query, params, _ = _jobs_by_stage(conn, stage, threshold, age, query_only=True)
            capped = {row[0] for row in conn.execute(f"SELECT url FROM ({query})", params)}
            selected = {row["url"] for row in select_jobs(conn, query, params, limit=total,
                        lane="prepare", stage="prepare", consume=False)} if total else set()
            queue_count = count_candidates(conn, query, params, lane="prepare", stage="prepare")
            for url, reason in list(reasons.items()):
                if reason == "passes_checks" and url not in capped:
                    reasons[url] = "company_document_cap"
                elif reason == "passes_checks" and url not in selected:
                    reasons[url] = "admission_allocation_wait"
            stages[stage] = _stage_result(reasons, selected, eligible_queue_count=queue_count)
            for url, reason in reasons.items():
                per_job[url][stage] = reason

        from applypilot.apply.selection_diagnostics import _ordered_checks
        from applypilot.apply.queue import company_capacity, list_actionable_jobs
        from applypilot.apply.guard import upload_artifact_paths, artifact_snapshot
        from applypilot.apply.readiness import present
        under_cap = company_capacity(conn)
        def documents(resume, cover):
            try:
                upload_artifact_paths(resume, cover, doc_format)
                return 1
            except (ValueError, OSError):
                return 0
        checks = _ordered_checks({"min_score": threshold, "max_age_days": age, "route_kind": route_kind})
        reasons = _first_reasons(conn, checks, threshold, routes=True, stage="apply")
        for row in cohort:
            if reasons.get(row["url"]) != "passes_checks":
                continue
            if not under_cap(row["company"], row["site"], row["strategy"], row["route_target"], row["url"]):
                reasons[row["url"]] = "company_capacity"
            elif not documents(row["tailored_resume_path"], row["cover_letter_path"]):
                reasons[row["url"]] = "document_files"
        selected = {job["url"] for job in list_actionable_jobs(conn, min_score=threshold,
                    max_age_days=age, route_kind=route_kind, doc_format=doc_format, limit=total)} if total else set()
        projected = {row[0] for row in conn.execute("SELECT job_url FROM apply_readiness")} if present(conn) else None
        jobs = {row["url"]: row for row in cohort}
        for url, reason in list(reasons.items()):
            if reason != "passes_checks" or url in selected:
                continue
            if projected is not None and url not in projected:
                reasons[url] = "readiness_projection_missing"
                continue
            row = jobs[url]
            try:
                artifact_snapshot(row["tailored_resume_path"], row["cover_letter_path"], doc_format)
            except (ValueError, OSError):
                reasons[url] = "artifact_verification"
            else:
                reasons[url] = "admission_allocation_wait"
        stages["apply"] = _stage_result(reasons, selected)
        for url, reason in reasons.items():
            per_job[url]["apply"] = reason
        reconciled = all(s["accounted_jobs"] == total and not s["selector_mismatches"] for s in stages.values())
        if conn.total_changes:
            raise RuntimeError("Read-only diagnostic unexpectedly changed database state")
        report.update(available=True, cohort_count=total, preview_dispatch_limit=total,
                      inventory={"has_resume": sum(bool((row["tailored_resume_path"] or "").strip()) for row in cohort),
                                 "has_cover": sum(bool((row["cover_letter_path"] or "").strip()) for row in cohort)},
                      stages=stages, jobs=list(per_job.values()), reconciled=reconciled)
    except Exception as error:
        # No partial counts masquerade as a complete inventory.
        report.update(available=False, reason="diagnostic_unavailable",
                      error_type=type(error).__name__,
                      action="Run normal startup migrations on the updated source, then inspect schema/path and retry.")
    finally:
        if conn is not None:
            conn.rollback()
            conn.close()
        report["elapsed_seconds"] = round(time.monotonic()-started, 3)
    return report


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=config.DB_PATH)
    parser.add_argument("--min-score", type=int, default=None)
    parser.add_argument("--max-age-days", type=int, default=None)
    parser.add_argument("--doc-format", choices=("pdf", "docx"), default="pdf")
    parser.add_argument("--route-kind", choices=("external", "linkedin-easy-apply", "email"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = inspect_blockers(args.db, min_score=args.min_score, max_age_days=args.max_age_days,
                              doc_format=args.doc_format, route_kind=args.route_kind)
    payload = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        if args.output.resolve() == args.db.resolve():
            parser.error("Output cannot overwrite the database")
        # Exclusive create protects existing reports (and their symlink targets).
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(payload)
        summary = {k: v for k, v in report.items() if k != "jobs"}
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(payload, end="")
    return 0 if report.get("available") and report.get("reconciled") else 2


if __name__ == "__main__":
    raise SystemExit(main())
