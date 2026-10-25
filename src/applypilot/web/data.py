"""Bounded, parameterized, read-only views of the existing database.

No account/password tables, SQL console, raw mailbox bodies, or arbitrary files
are exposed. Each response reads a bounded, read-only PostgreSQL snapshot.
Missing optional tables are explicitly reported rather than migrated on a GET.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
import csv
import io
import json
import re
from applypilot import postgres
import time

UTC = timezone.utc
CONFIRMED_SUBMISSION_CONFIDENCE = ('explicit-proof', 'user-confirmed')
ATTENTION = ("manual_only", "verification_required", "verification_quarantined",
             "enrich_failed", "score_failed", "tailor_failed", "cover_failed", "apply_failed")
JOB_COLUMNS = ("url", "title", "company", "location", "site", "state", "fit_score", "salary",
               "discovered_at", "applied_at", "apply_status", "apply_attempts")
DETAIL_COLUMNS = JOB_COLUMNS + ("description", "full_description", "application_url", "score_reasoning",
    "scored_at", "detail_scraped_at", "tailored_at", "cover_letter_at", "apply_error", "detail_error",
    "score_error", "last_attempted_at", "verification_confidence", "eligibility", "salary_currency",
    "salary_p50", "salary_period", "tailored_resume_path", "cover_letter_path", "interview_brief_path")
DOCUMENT_COLUMNS = {"resume": "tailored_resume_path", "cover": "cover_letter_path",
                    "brief": "interview_brief_path"}
SORTS = {"newest": "discovered_at DESC", "oldest": "discovered_at ASC", "score": "fit_score DESC",
         "company": "LOWER(company) ASC", "applied": "applied_at DESC", "title": "LOWER(title) ASC"}


def now_iso():
    return datetime.now(UTC).isoformat()


def activity_sort_key(record):
    """Normalize UTC/offset timestamps when merging separate retained streams."""
    try:
        dt = datetime.fromisoformat(str(record.get("at", "")).replace("Z", "+00:00"))
        return dt.replace(tzinfo=dt.tzinfo or UTC).timestamp(), str(record.get("id", ""))
    except (ValueError, TypeError):
        return 0, str(record.get("id", ""))


def activity_details(raw):
    """Expose stored diagnostic evidence without credential or document payloads."""
    from applypilot.operations.runtime import diagnostic_details, sanitize
    value = safe_json(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        return {}
    result = diagnostic_details(value)
    for key in ("detail", "reason_code", "outcome", "severity", "attempt_id", "trace_id",
                "_operation_span_id", "operation_span_id", "source"):
        if isinstance(value.get(key), (str, int, float)):
            result[key] = sanitize(value[key], 2400 if key == "detail" else 200)
    return result


def integer(value, default, low, high):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError("Expected an integer")
    try:
        result = int(str(value))
    except (ValueError, TypeError):
        raise ValueError("Expected an integer") from None
    if not low <= result <= high:
        raise ValueError(f"Value must be between {low} and {high}")
    return result


def text(value, maximum=200):
    value = str(value or "").strip()
    if len(value) > maximum:
        raise ValueError(f"Text exceeds {maximum} characters")
    return value


def like(value):
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def tables(conn):
    return {r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname=current_schema()")} if conn else set()


def columns(conn, table):
    # Only internal constant table names reach this helper.
    return {r[1] for r in conn.table_info(table)}


def select_columns(conn, table, names, prefix=""):
    available = columns(conn, table)
    return ",".join(f'{prefix}"{name}"' if name in available else f'NULL AS "{name}"' for name in names)


def rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params)]


def safe_json(value, default=None):
    try:
        return json.loads(value) if value else ({} if default is None else default)
    except (ValueError, TypeError):
        return {} if default is None else default


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0
    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skip += 1
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3"} and not self.skip:
            self.parts.append("\n")
    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.skip:
            self.skip -= 1
    def handle_data(self, value):
        if not self.skip:
            self.parts.append(value)


def plain(value, maximum=40000):
    value = str(value or "")[:maximum]
    if re.search(r"</?(?:p|div|br|li|script|style|h[1-6]|span|ul)\b", value, re.I):
        parser = _PlainText()
        parser.feed(value)
        value = "".join(parser.parts)
    return value.strip()


class Store:
    def __init__(self, db_path: Path):
        self.path = Path(db_path).resolve()

    @contextmanager
    def read(self):
        if not self.path.exists():
            yield None
            return
        from applypilot.postgres_reads import connect_readonly
        conn = connect_readonly(self.path)
        try:
            conn.execute("BEGIN")
            yield conn
        finally:
            try:
                conn.rollback()
            finally:
                conn.close()

    def summary(self, days=30):
        days = integer(days, 30, 1, 365)
        today = datetime.now(UTC).date()
        start = today - timedelta(days=days - 1)
        series = {str(start + timedelta(days=i)): {"date": str(start + timedelta(days=i)),
                  "discovered": 0, "submitted": 0, "outreach": 0} for i in range(days)}
        value = {"at": now_iso(), "days": days, "timezone": "UTC", "database_available": False,
                 "totals": {"jobs": 0, "companies": 0, "scored": 0, "submitted": 0, "ready": 0,
                            "reported_unverified": 0,
                            "attention": 0, "average_score": None, "contacts": 0, "cases": 0,
                            "outreach_sent": 0, "outreach_attempted_today": 0, "outreach_sent_today": 0,
                            "submitted_today": 0, "incidents": 0, "pending_requests": 0},
                 "period": {"discovered": 0, "submitted": 0, "outreach": 0}, "stages": [],
                 "scores": [], "outcomes": [], "outcome_groups": {}, "series": [], "top_companies": [], "missing_tables": [], "pending_next_at": None}
        with self.read() as conn:
            ts = tables(conn)
            value["missing_tables"] = sorted({"jobs", "referral_outbox", "application_outcomes", "source_health"} - ts)
            if "jobs" not in ts:
                value["series"] = list(series.values())
                return value
            value["database_available"] = True
            confirmed = ("verification_confidence IN (" + ",".join("'" + value + "'" for value in CONFIRMED_SUBMISSION_CONFIDENCE) + ")"
                         if 'verification_confidence' in columns(conn, 'jobs') else 'FALSE')
            total = dict(conn.execute(f"""SELECT COUNT(*) jobs, COUNT(DISTINCT NULLIF(company,'')) companies,
                COUNT(fit_score) scored, AVG(fit_score)::double precision average_score,
                SUM(CASE WHEN NULLIF(applied_at,'') IS NOT NULL AND {confirmed} THEN 1 ELSE 0 END) submitted,
                SUM(CASE WHEN NULLIF(applied_at,'') IS NOT NULL AND NOT COALESCE({confirmed},FALSE) THEN 1 ELSE 0 END) reported_unverified,
                SUM(CASE WHEN state='ready_to_apply' THEN 1 ELSE 0 END) ready,
                SUM(CASE WHEN ap_date(applied_at)=? AND {confirmed} THEN 1 ELSE 0 END) submitted_today
                FROM jobs""", (str(today),)).fetchone())
            value["totals"].update({k: (v if v is not None or k == "average_score" else 0) for k, v in total.items()})
            value["stages"] = rows(conn, "SELECT COALESCE(NULLIF(state,''),'discovered') label, COUNT(*) value FROM jobs GROUP BY 1 ORDER BY value DESC")
            value["totals"]["attention"] = sum(r["value"] for r in value["stages"] if r["label"] in ATTENTION)
            value["scores"] = rows(conn, "SELECT fit_score label, COUNT(*) value FROM jobs WHERE fit_score IS NOT NULL GROUP BY fit_score ORDER BY fit_score")
            value["top_companies"] = rows(conn, f"""SELECT company label, COUNT(*) value,
                SUM(CASE WHEN NULLIF(applied_at,'') IS NOT NULL AND {confirmed} THEN 1 ELSE 0 END) submitted
                FROM jobs WHERE NULLIF(company,'') IS NOT NULL GROUP BY company ORDER BY value DESC,company LIMIT 8""")
            for field, measure in (("discovered_at", "discovered"), ("applied_at", "submitted")):
                evidence = f" AND {confirmed}" if measure == 'submitted' else ''
                for r in conn.execute(f"SELECT ap_date({field}) AS day,COUNT(*) n FROM jobs WHERE ap_date({field}) BETWEEN ? AND ?{evidence} GROUP BY 1", (str(start), str(today))):
                    if r["day"] in series:
                        series[r["day"]][measure] = r["n"]
            if "referral_outbox" in ts:
                outbound = conn.execute("""SELECT
                    SUM(CASE WHEN state IN ('sent','manual_sent') AND sent_at IS NOT NULL THEN 1 ELSE 0 END),
                    SUM(CASE WHEN ap_date(attempted_at)=? THEN 1 ELSE 0 END),
                    SUM(CASE WHEN ap_date(sent_at)=? AND state IN ('sent','manual_sent') THEN 1 ELSE 0 END)
                    FROM referral_outbox""", (str(today), str(today))).fetchone()
                value["totals"].update(outreach_sent=outbound[0] or 0, outreach_attempted_today=outbound[1] or 0,
                                       outreach_sent_today=outbound[2] or 0)
                for r in conn.execute("SELECT ap_date(sent_at) AS day,COUNT(*) n FROM referral_outbox WHERE state IN ('sent','manual_sent') AND ap_date(sent_at) BETWEEN ? AND ? GROUP BY 1", (str(start), str(today))):
                    if r["day"] in series:
                        series[r["day"]]["outreach"] = r["n"]
            if {"unattended_send_windows", "referral_auto_grants", "referral_contacts", "referral_cases", "referral_outbox"} <= ts:
                from applypilot.unattended.grace import pending
                waiting = pending(conn)
                value["totals"]["pending_requests"] = len(waiting)
                value["pending_next_at"] = waiting[0]["send_after"] if waiting else None
            for table, metric in (("referral_contacts", "contacts"), ("referral_cases", "cases")):
                if table in ts:
                    value["totals"][metric] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if "application_outcomes" in ts:
                valid = " WHERE voided_at IS NULL" if "voided_at" in columns(conn, "application_outcomes") else ""
                value["outcomes"] = rows(conn, "SELECT event_type label,COUNT(*) value,COUNT(DISTINCT job_url) jobs FROM application_outcomes" + valid + " GROUP BY event_type ORDER BY value DESC")
                groups = {
                    "responses": ("recruiter_response",),
                    "interviews": ("screen", "interview", "technical_interview", "interview_completed"),
                    "offers": ("offer",),
                }
                for group, event_types in groups.items():
                    condition = " AND " if valid else " WHERE "
                    placeholders = ",".join("?" for _ in event_types)
                    value["outcome_groups"][group] = conn.execute(
                        "SELECT COUNT(DISTINCT job_url) FROM application_outcomes" + valid + condition
                        + f"event_type IN ({placeholders})", event_types,
                    ).fetchone()[0]
            value["series"] = list(series.values())
            value["period"] = {key: sum(p[key] for p in series.values()) for key in ("discovered", "submitted", "outreach")}
            return value

    def facets(self):
        with self.read() as conn:
            if "jobs" not in tables(conn):
                return {"states": [], "sources": [], "companies": []}
            return {"states": [r[0] for r in conn.execute("SELECT DISTINCT state FROM jobs WHERE NULLIF(state,'') IS NOT NULL ORDER BY state")],
                    "sources": [r[0] for r in conn.execute("SELECT DISTINCT site FROM jobs WHERE NULLIF(site,'') IS NOT NULL ORDER BY site LIMIT 250")],
                    "companies": [r[0] for r in conn.execute("SELECT company FROM jobs WHERE NULLIF(TRIM(company),'') IS NOT NULL GROUP BY company ORDER BY LOWER(company),company")]}

    def job_filter(self, q):
        where, params = ["1=1"], []
        search = text(q.get("q"))
        if search:
            for term in search.split():
                where.append("(title ILIKE ? ESCAPE '\\' OR company ILIKE ? ESCAPE '\\' OR location ILIKE ? ESCAPE '\\')")
                params.extend([like(term)] * 3)
        for key, column in (("state", "state"), ("source", "site")):
            v = text(q.get(key))
            if v:
                where.append(f"{column}=?")
                params.append(v)
        company = text(q.get("company"))
        if company:
            where.append("company ILIKE ? ESCAPE '\\'")
            params.append(like(company))
        for key, op in (("min_score", ">="), ("max_score", "<=")):
            if q.get(key) not in (None, ""):
                where.append(f"fit_score {op} ?")
                params.append(integer(q[key], 0, 0, 10))
        view = text(q.get("view"))
        if view == "submitted":
            where.append("NULLIF(applied_at,'') IS NOT NULL")
        elif view == "attention":
            where.append("state IN (" + ",".join("?" for _ in ATTENTION) + ")")
            params.extend(ATTENTION)
        elif view == "ready":
            where.append("state='ready_to_apply'")
        elif view not in ("", "all"):
            raise ValueError("Unknown jobs view")
        for key, op in (("from", ">="), ("to", "<=")):
            if q.get(key):
                value = date.fromisoformat(text(q[key], 10)).isoformat()
                where.append(f"ap_date(discovered_at) {op} ?")
                params.append(value)
        if q.get("from") and q.get("to") and q["from"] > q["to"]:
            raise ValueError("Start date is after end date")
        sort = text(q.get("sort") or "newest")
        if sort not in SORTS:
            raise ValueError("Unknown sort order")
        return " AND ".join(where), params, SORTS[sort]

    def jobs(self, q, *, export=False):
        page = integer(q.get("page"), 1, 1, 1000000)
        size = 10000 if export else integer(q.get("page_size"), 25, 10, 100)
        where, params, sort = self.job_filter(q)
        with self.read() as conn:
            if "jobs" not in tables(conn):
                return {"items": [], "total": 0, "page": 1, "page_size": size, "truncated": False}
            total = conn.execute("SELECT COUNT(*) FROM jobs WHERE " + where, params).fetchone()[0]
            page = 1 if export else min(page, max(1, (total + size - 1) // size))
            result = rows(conn, "SELECT " + select_columns(conn, "jobs", JOB_COLUMNS) + " FROM jobs WHERE " + where +
                          " ORDER BY " + sort + ",url ASC LIMIT ? OFFSET ?", [*params, size, (page - 1) * size])
            return {"items": result, "total": total, "page": page, "page_size": size, "truncated": export and total > size}

    def detail(self, url):
        url = text(url, 4000)
        with self.read() as conn:
            ts = tables(conn)
            if "jobs" not in ts:
                raise LookupError("Job not found")
            raw = conn.execute("SELECT " + select_columns(conn, "jobs", DETAIL_COLUMNS) + " FROM jobs WHERE url=?", (url,)).fetchone()
            if raw is None:
                raise LookupError("Job not found")
            item = dict(raw)
            item["description"] = plain(item.get("full_description") or item.get("description"))
            item.pop("full_description", None)
            item["score_reasoning"] = plain(item.get("score_reasoning"), 16000)
            item["documents"] = [{"kind": k, "name": Path(str(item[col])).name}
                                 for k, col in DOCUMENT_COLUMNS.items() if item.get(col)]
            for col in DOCUMENT_COLUMNS.values():
                item.pop(col, None)
            timeline = []
            if "job_state_transitions" in ts:
                fields = select_columns(conn, "job_state_transitions", ("from_state", "to_state", "at", "reason", "metadata"))
                timeline = rows(conn, "SELECT " + fields + " FROM job_state_transitions WHERE job_url=? ORDER BY at DESC,id DESC LIMIT 100", (url,))
                for event in timeline:
                    event["details"] = activity_details(event.pop("metadata", None))
                    if event["details"].get("detail"):
                        event["reason"] = event["details"]["detail"]
                if (timeline and timeline[0].get("details", {}).get("attempt_id")
                        and timeline[0]["details"].get("detail") and item.get("apply_error")):
                    item["apply_error"] = timeline[0]["details"]["detail"]
            outcomes = []
            if "application_outcomes" in ts:
                valid = " AND voided_at IS NULL" if "voided_at" in columns(conn, "application_outcomes") else ""
                outcomes = rows(conn, "SELECT event_type,occurred_at,source,trust FROM application_outcomes WHERE job_url=?" + valid + " ORDER BY occurred_at DESC LIMIT 50", (url,))
            return {"job": item, "timeline": timeline, "outcomes": outcomes}

    def document(self, url, kind, report_id=None):
        if kind not in DOCUMENT_COLUMNS:
            raise ValueError("Unknown document kind")
        with self.read() as conn:
            if "jobs" not in tables(conn):
                raise LookupError("Document not found")
            col = DOCUMENT_COLUMNS[kind]
            if report_id:
                if kind != 'brief':
                    raise ValueError('Career report documents are interview briefs')
                report = conn.execute("""SELECT result_json FROM career_ops_reports
                    WHERE id=? AND job_url=? AND mode='interview_brief' AND state='complete'""",
                    (text(report_id, 80), text(url, 4000))).fetchone()
                row = (safe_json(report['result_json']).get('artifact'),) if report else None
            else:
                row = conn.execute(f"SELECT {col} FROM jobs WHERE url=?", (text(url, 4000),)).fetchone()
            if not row or not row[0]:
                raise LookupError("Document not found")
            candidate = Path(row[0]).expanduser()
            if not candidate.is_absolute():
                candidate = self.path.parent / candidate
            path = candidate.resolve()
            # Never serve the .env, credentials, arbitrary DB paths, or symlink escapes.
            roots = [self.path.parent / n for n in ("tailored_resumes", "cover_letters", "interview_briefs", "briefs")]
            suffixes = {'.pdf', '.docx', '.txt', '.md', '.tex'}
            if kind == 'brief':
                roots.append(self.path.parent / 'impact_briefs')
                suffixes.add('.json')
            if not any(not r.is_symlink() and path.is_relative_to(r.resolve()) for r in roots) or path.suffix.lower() not in suffixes:
                raise PermissionError("This document is outside the dashboard's allowed output folders")
            if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
                raise LookupError("Document is missing or exceeds 20 MB")
            return path

    def outreach_facets(self):
        with self.read() as conn:
            if "referral_outbox" not in tables(conn):
                return {"states": [], "channels": []}
            return {"states": [row[0] for row in conn.execute("SELECT DISTINCT state FROM referral_outbox WHERE NULLIF(state,'') IS NOT NULL ORDER BY state")],
                    "channels": [row[0] for row in conn.execute("SELECT DISTINCT channel FROM referral_outbox WHERE NULLIF(channel,'') IS NOT NULL ORDER BY channel")]}

    def outreach(self, q):
        page = integer(q.get("page"), 1, 1, 100000)
        size = 25
        where, params = ["1=1"], []
        for key in ("state", "channel"):
            if q.get(key):
                where.append(f"o.{key}=?")
                params.append(text(q[key], 50))
        if q.get("q"):
            where.append("(c.name LIKE ? ESCAPE '\\' OR COALESCE(j.company,c.company) LIKE ? ESCAPE '\\' OR COALESCE(j.title,rc.title) LIKE ? ESCAPE '\\')")
            params.extend([like(text(q["q"]))] * 3)
        with self.read() as conn:
            ts = tables(conn)
            if not {"referral_outbox", "referral_contacts", "referral_cases", "jobs"} <= ts:
                return {"items": [], "total": 0, "page": 1, "page_size": size, "counts": [], "pending": []}
            joins = " FROM referral_outbox o JOIN referral_contacts c ON c.id=o.contact_id JOIN referral_cases rc ON rc.id=o.case_id LEFT JOIN jobs j ON j.url=rc.job_url"
            counts = rows(conn, "SELECT state label,COUNT(*) value FROM referral_outbox GROUP BY state ORDER BY value DESC")
            total = conn.execute("SELECT COUNT(*)" + joins + " WHERE " + " AND ".join(where), params).fetchone()[0]
            page = min(page, max(1, (total + size - 1) // size))
            values = rows(conn, "SELECT o.id,o.state,o.channel,o.kind,o.created_at,o.sent_at,o.attempted_at,o.error_kind,c.name,c.relationship,c.role_function,COALESCE(j.company,c.company) company,COALESCE(j.title,rc.title) title,j.url job_url" + joins + " WHERE " + " AND ".join(where) + " ORDER BY o.created_at DESC,o.id LIMIT ? OFFSET ?", [*params, size, (page - 1) * size])
            pending = []
            if {"unattended_send_windows", "referral_auto_grants"} <= ts:
                # Use the same exact draft/hash/grant criteria as the send/cancel subsystem.
                from applypilot.unattended.grace import pending as pending_requests
                pending = pending_requests(conn)
            return {"items": values, "total": total, "page": page, "page_size": size, "counts": counts, "pending": pending}

    def outreach_detail(self, outbox_id):
        with self.read() as conn:
            if "referral_outbox" not in tables(conn):
                raise LookupError("Request not found")
            r = conn.execute("""SELECT o.id,o.state,o.channel,o.kind,o.payload_json,o.created_at,o.sent_at,o.attempted_at,
                o.error_kind,c.name,c.role_function,c.relationship,c.profile_url,COALESCE(j.title,rc.title) title,COALESCE(j.company,c.company) company,rc.job_url,
                rc.state case_state,rc.mechanism,rc.hold_until FROM referral_outbox o
                JOIN referral_contacts c ON c.id=o.contact_id JOIN referral_cases rc ON rc.id=o.case_id
                LEFT JOIN jobs j ON j.url=rc.job_url WHERE o.id=?""", (text(outbox_id, 150),)).fetchone()
            if r is None:
                raise LookupError("Request not found")
            result = dict(r)
            payload = safe_json(result.pop("payload_json"))
            result["payload"] = {k: payload.get(k) for k in ("to", "subject", "body", "phase")}
            attachment = payload.get("attachment") or {}
            result["payload"]["attachment"] = attachment.get("name") if isinstance(attachment, dict) else None
            result["cancellable"] = result["state"] in {"draft", "approved"} and not result["attempted_at"]
            return result

    def activity(self, q):
        """Read events without treating backfills or repeated checks as new work.

        Outcome corrections are an optional feedback migration. A GET must work
        both before and after that migration, and must never run it itself.
        """
        kind = text(q.get("kind") or "all", 30)
        query = text(q.get("q"))
        severity = text(q.get("severity"), 20)
        source = text(q.get("source"), 100)
        if severity not in {"", "info", "warning", "error", "critical"}:
            raise ValueError("Unknown activity severity")
        if kind not in {"all", "pipeline", "outcome", "outreach", "snapshot", "recheck"}:
            raise ValueError("Unknown activity type")
        value = {"items": [], "limit": 100, "at": now_iso(), "warnings": [],
                 "database_available": False,
                 "scope": "Recorded events, not current queue eligibility. Imported snapshots and repeated checks have separate filters."}
        with self.read() as conn:
            ts = tables(conn)
            if "jobs" not in ts:
                value["warnings"].append("Job history is not initialized; account/setup history is separate.")
                return value
            if not {"url", "title", "company"} <= columns(conn, "jobs"):
                value["warnings"].append("The jobs schema is incomplete. No job-history count is available.")
                return value
            value["database_available"] = True
            entries = []
            job_source = "COALESCE(NULLIF(j.site,''),'unknown')" if "site" in columns(conn, "jobs") else "'unknown'"

            def filters(reason, event_severity, *extra, title="j.title", company="j.company"):
                clause, params = "", []
                searchable = (title, company, reason, job_source, event_severity, "j.url", "t.id", *extra)
                for term in query.split():
                    clause += " AND (" + " OR ".join(
                        f"COALESCE(CAST({field} AS TEXT),'') ILIKE ? ESCAPE '\\'" for field in searchable) + ")"
                    params.extend([like(term)] * len(searchable))
                if source:
                    clause += f" AND {job_source}=?"
                    params.append(source)
                if severity:
                    if severity == "error":
                        clause += f" AND ({event_severity}) IN ('error','critical')"
                    else:
                        clause += f" AND ({event_severity})=?"
                        params.append(severity)
                return clause, params

            def ready(table, required):
                if table not in ts:
                    value["warnings"].append(table + " history is not initialized.")
                    return False
                if not set(required) <= columns(conn, table):
                    value["warnings"].append(table + " history has an incomplete schema; this section was not read.")
                    return False
                return True

            if kind in {"all", "pipeline", "snapshot", "recheck"} and ready(
                    "job_state_transitions", {"id", "at", "job_url", "to_state"}):
                available = columns(conn, "job_state_transitions")
                previous = "t.from_state" if "from_state" in available else "NULL"
                reason = "t.reason" if "reason" in available else "NULL"
                metadata = "t.metadata" if "metadata" in available else "NULL"
                event_severity = (f"COALESCE(CASE WHEN ap_json_valid({metadata}) THEN ap_json_text({metadata},'$.severity') END,"
                                  "CASE WHEN t.to_state LIKE '%failed' THEN 'error' WHEN t.to_state IN ('verification_required','verification_quarantined','manual_only') THEN 'warning' ELSE 'info' END)")
                searchable_reason = f"COALESCE(CASE WHEN ap_json_valid({metadata}) THEN ap_json_text({metadata},'$.detail') END,{reason})"
                search, params = filters(searchable_reason, event_severity, reason, metadata, "t.to_state", previous)
                imported = f"COALESCE({reason},'') LIKE 'migrated %'"
                repeated = f"({previous} IS NOT DISTINCT FROM t.to_state)"
                event_kind = f"CASE WHEN {imported} THEN 'snapshot' WHEN {repeated} AND ({event_severity})='info' THEN 'recheck' ELSE 'pipeline' END"
                selected = "pipeline" if kind == "all" else kind
                entries += rows(conn, f"""SELECT {event_kind} kind,t.id,t.at,t.to_state event,
                    {previous} from_state,{reason} reason,{metadata} metadata,{event_severity} severity,
                    {job_source} source,j.title,j.company,j.url job_url
                    FROM job_state_transitions t JOIN jobs j ON j.url=t.job_url
                    WHERE ({event_kind})=?""" + search + " ORDER BY t.at DESC,t.id DESC LIMIT 100",
                    [selected, *params])
            if kind in {"all", "outcome"} and ready(
                    "application_outcomes", {"id", "occurred_at", "job_url", "event_type"}):
                valid = "t.voided_at IS NULL" if "voided_at" in columns(conn, "application_outcomes") else "1=1"
                search, params = filters("t.event_type", "'info'")
                entries += rows(conn, f"""SELECT 'outcome' kind,t.id,t.occurred_at at,t.event_type event,
                    'info' severity,{job_source} source,j.title,j.company,j.url job_url FROM application_outcomes t
                    JOIN jobs j ON j.url=t.job_url WHERE """ + valid + search +
                    " ORDER BY t.occurred_at DESC,t.id DESC LIMIT 100", params)
            if kind in {"all", "outreach"} and ready(
                    "referral_outbox", {"id", "case_id", "updated_at", "state"}):
                if ready("referral_cases", {"id", "job_url"}):
                    reason = "t.error_kind" if "error_kind" in columns(conn, "referral_outbox") else "NULL"
                    event_severity = f"CASE WHEN NULLIF({reason},'') IS NOT NULL THEN 'error' WHEN t.state='deferred' THEN 'warning' ELSE 'info' END"
                    search, params = filters(reason, event_severity, "t.state",
                                             title="COALESCE(j.title,rc.title)", company="COALESCE(j.company,rc.company_key)")
                    entries += rows(conn, f"""SELECT 'outreach' kind,t.id,t.updated_at at,t.state event,
                        {reason} reason,{event_severity} severity,{job_source} source,
                        COALESCE(j.title,rc.title) title,COALESCE(j.company,rc.company_key) company,j.url job_url FROM referral_outbox t
                        JOIN referral_cases rc ON rc.id=t.case_id LEFT JOIN jobs j ON j.url=rc.job_url WHERE 1=1""" +
                        search + " ORDER BY t.updated_at DESC,t.id DESC LIMIT 100", params)
            for entry in entries:
                entry["details"] = activity_details(entry.pop("metadata", None))
                if entry["details"].get("detail"):
                    entry["reason"] = entry["details"]["detail"]
            entries.sort(key=activity_sort_key, reverse=True)
            value["items"] = entries[:100]
            return value

    def sources(self):
        with self.read() as conn:
            ts = tables(conn)
            health = rows(conn, "SELECT " + select_columns(conn, "source_health", (
                "source_name", "status", "last_run_at", "last_success_at", "jobs_found", "jobs_stored",
                "consecutive_failures", "last_error", "run_details_json")) + " FROM source_health ORDER BY source_name") if "source_health" in ts else []
            from applypilot.operations.runtime import sanitize
            for source in health:
                source["error"] = sanitize(source.pop("last_error", None), 2400)
                details = safe_json(source.pop("run_details_json", None))
                source["activity"] = details if isinstance(details, dict) else {}
                retry_at = source["activity"].get("retry_at")
                source["retry_at"] = (datetime.fromtimestamp(retry_at, UTC).isoformat()
                                      if isinstance(retry_at, (int, float)) and 0 < retry_at < 253402300799 else None)
                for error in source["activity"].get("reader_errors", []):
                    if isinstance(error, dict):
                        error["error"] = sanitize(error.get("error"), 1200)
            totals = rows(conn, "SELECT COALESCE(NULLIF(site,''),'unknown') source,COUNT(*) jobs,SUM(CASE WHEN applied_at IS NOT NULL AND applied_at<>'' THEN 1 ELSE 0 END) submitted,AVG(fit_score)::double precision average_score FROM jobs GROUP BY 1 ORDER BY jobs DESC") if "jobs" in ts else []
            return {"health": health, "totals": totals}

    def system(self):
        with self.read() as conn:
            ts = tables(conn)
            counts = {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                      for name in ("jobs", "referral_contacts", "referral_leads", "referral_cases", "referral_outbox",
                                   "application_attempts", "application_outcomes", "job_state_transitions") if name in ts}
            incidents = []
            tasks = []
            metrics = None
            if "agent_run_metrics" in ts:
                metrics = dict(conn.execute("SELECT COUNT(*) runs,COALESCE(SUM(input_tokens),0) input_tokens,COALESCE(SUM(output_tokens),0) output_tokens,COALESCE(SUM(tool_calls),0) tool_calls FROM agent_run_metrics").fetchone())
            size = int(conn.execute("""SELECT COALESCE(SUM(pg_total_relation_size(c.oid)),0)
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname=current_schema() AND c.relkind IN ('r','m')""").fetchone()[0])
        return {"database": {"name": self.path.name, "engine": "postgresql", "bytes": size,
                    "wal_bytes": None, "wal_scope": "cluster-wide; not attributed to this store",
                    "tables": counts}, "incidents": incidents, "tasks": tasks, "metrics": metrics}

    def export(self, q):
        result = self.jobs(q, export=True)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(JOB_COLUMNS)
        for item in result["items"]:
            record = []
            for key in JOB_COLUMNS:
                value = str(item.get(key) if item.get(key) is not None else "")
                if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
                    value = "'" + value
                record.append(value)
            writer.writerow(record)
        return output.getvalue(), result
