"""Owner-requested, local-only diagnostic ZIP. Never runs an application or probe.

Each namespace has separate read-only transactions for facts and row samples.
Databases and log files have independent, labeled capture windows. Secrets are redacted; this is NOT an anonymization or backup format.
Every omission, error and limit is reported in manifest.json.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import importlib.metadata
from itertools import zip_longest
import json
import os
from pathlib import Path
import platform
import re
import shutil
from applypilot import postgres
from applypilot.runtime_policy import DiagnosticPolicy, load_policy
import stat
import tempfile
import threading
import sys
import zipfile

_LOCK = threading.Lock()
_SECRET = re.compile(r"password|secret|(?:^|_)token(?:$|_)|credential|api.?key|authorization|cookie", re.I)
# These contain business facts rather than credentials. Keep their structure.
_FACT_KEYS = {"authorization_required", "authorization_countries", "authorization_requirements",
              "authorization_by_country", "authorized_countries", "legally_authorized_to_work",
              "require_sponsorship", "authorization", "authorized_sources", "token_usage",
              "prompt_tokens", "completion_tokens", "total_tokens", "max_tokens", "output_tokens",
              "input_tokens", "max_output_tokens", "max_completion_tokens", "work_authorization",
              "authorization_scope", "foreign_authorization_policy", "work_authorization_policy"}
_EXCLUDED_TABLES = {"accounts", "application_answers", "qa_knowledge", "tracking_emails",
                    "tracking_people", "feedback_messages", "referral_messages", "referral_contacts",
                    "referral_generated", "referral_auto_messages", "temporal_inputs", "temporal_publication_receipts",
                    "document_inputs", "document_artifacts"}
_MAIN_TABLES = {"jobs", "job_state_transitions", "job_structured_facts", "source_health",
                "live_status", "recovery_work", "recovery_meta", "recovery_budget_credits",
                "application_attempts", "application_routes", "application_outcomes",
                "agent_run_metrics", "domain_circuit_breakers", "linkedin_governor_state",
                "linkedin_activity", "company_registry"}
_MAIN_PREFIXES = ("discovery_", "referral_", "unattended_", "feedback_")
_OP_TABLES = {"events", "processes", "active", "runs", "incidents", "gauges", "counters", "items", "meta", "journal_batches"}
_LEARNING_TABLES = {"episodes", "tools", "tool_calls", "rounds", "revisions", "examples",
                    "policy_artifacts", "learning_artifacts", "learning_evaluations"}
# Export operational coordinates, not cached source bodies, model results or tokens.
_AUX_PROJECTIONS = {
    "sources": {
        "cache": "key,expires", "gates": "provider,next_allowed,reason",
        "slots": "name,expires", "reports": "id,target,created",
    },
    "signals": {
        "signal_revisions_v1": "provider,signal_id,revision,first_seen,last_seen",
        "signal_extractions_v1": "provider,signal_id,revision,extractor,state,lease_until,attempt_count,retry_at,failure_json",
        "signal_publications_v1": "provider,signal_id,revision,extractor,context_key,created_at",
        "signal_snapshots_v1": "id", "signal_window_pages_v1": "window_id,page_id,lane",
        "signal_window_seen_v1": "window_id,lane,signal_id",
    },
}
_CONFIG_FILES = ("autopilot-options.json", "discovery_runtime.yaml", "searches.yaml", "runtime_policy.yaml",
                 "unattended.json", "unattended-status.json",
                 "autopilot-status.json", "autopilot.lock",
                 "apply-runtime.lock", "company_limits.yaml", "operations/settings.json",
                 "resource-capacity.json", "learning/scheduler-status.json",
                 "temporal/status.json", "temporal/install.json")
_BODY_COLUMNS = {"body", "body_text", "body_html", "html", "email_body", "resume_text", "cover_letter_text"}


def _now():
    return datetime.now(timezone.utc).isoformat()


class Redactor:
    """Defense in depth, not a guarantee against arbitrary secrets in prose."""
    def __init__(self, root: Path):
        self.root = str(root)
        self.salt = os.urandom(32)
        secrets = []
        for key, value in os.environ.items():
            if _SECRET.search(key) and len(value) >= 6:
                secrets.append(value)
        # Read only the workspace .env, never export it or arbitrary referenced files.
        path = root / ".env"
        try:
            if not path.is_symlink() and path.is_file() and path.stat().st_size < 2_000_000:
                for line in path.read_text(encoding="utf-8").splitlines():
                    match = re.match(r"\s*(?:export\s+)?([\w]+)\s*=\s*(.*)", line)
                    if match and _SECRET.search(match[1]):
                        value = match[2].strip().strip('\"\'')
                        if len(value) >= 6:
                            secrets.append(value)
        except (OSError, UnicodeError):
            pass
        self.secrets = sorted(set(secrets), key=len, reverse=True)

    def pseudonym(self, value):
        return "redacted:" + hashlib.sha256(self.salt + str(value).encode()).hexdigest()[:16]

    def text(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "[secret removed]")
        value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
                       "[private key removed]", value, flags=re.S)
        value = re.sub(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9_./+\-=]+", "[authorization removed]", value, flags=re.I)
        value = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{15,}|github_pat_[A-Za-z0-9_]+)\b",
                       "[secret removed]", value)
        value = re.sub(r"(?i)([\"']?(?:[\w.-]*(?:password|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret)[\w.-]*)[\"']?\s*[:=]\s*)([\"'])(.*?)(\2)",
                       r"\1\2[secret removed]\2", value)
        value = re.sub(r"(?i)((?:password|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|token)\s*=\s*)[^\s&\"'<>]+",
                       r"\1[secret removed]", value)
        value = re.sub(r"(?im)^\s*(?:set-cookie|cookie|authorization|x-api-key)\s*:.*$",
                       "[sensitive header removed]", value)
        value = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[credentials removed]@", value)
        value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email removed]", value)
        value = value.replace(self.root, "[workspace]")
        value = re.sub(r"(?i)[A-Z]:[\\/]Users[\\/][^\\/\s\"']+", "[user home]", value)
        value = re.sub(r"/(?:home|Users)/[^/\s\"']+", "[user home]", value)
        return value

    def value(self, value, key="", depth=0):
        if depth > 40:
            return "[nested value omitted]"
        if key.lower() == "authorization" and isinstance(value, str):
            return self.pseudonym(value)
        if key not in _FACT_KEYS and _SECRET.search(key):
            return None if value is None else self.pseudonym(value)
        if key.lower() in _BODY_COLUMNS:
            return None if value is None else "[personal document/message body omitted]"
        if isinstance(value, dict):
            return {str(k): self.value(v, str(k), depth + 1) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(v, "", depth + 1) for v in value]
        if isinstance(value, bytes):
            return {"blob_bytes": len(value), "omitted": True}
        if isinstance(value, str):
            if value.lstrip().startswith(("{", "[")):
                try:
                    parsed = json.loads(value)
                except (ValueError, RecursionError):
                    pass
                else:
                    return json.dumps(self.value(parsed, "", depth + 1), ensure_ascii=False)
            return self.text(value)
        return value


def _safe(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
        for part in (path, *path.parents):
            if part == root:
                break
            if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
                return False
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


class Writer:
    def __init__(self, archive, redactor, limits):
        self.archive, self.redactor, self.limits = archive, redactor, limits
        self.used = 0
        self.entries = []

    def omitted(self, name, reason, **details):
        self.entries.append({"path": name, "status": "omitted", "reason": reason, **details})

    def failure(self, name, exc):
        from .runtime import exception_details
        details = exception_details(exc)
        self.omitted(name, type(exc).__name__, error=self.redactor.text(details['exception_chain'][0]['message']),
                     **self.redactor.value(details))

    def data(self, name, value):
        payload = (json.dumps(self.redactor.value(value), ensure_ascii=False, indent=2, default=str) + "\n").encode()
        if self.used + len(payload) > self.limits.total_bytes:
            self.omitted(name, "total_size_limit")
            return
        self.archive.writestr(name, payload)
        self.used += len(payload)
        self.entries.append({"path": name, "status": "complete", "bytes": len(payload),
                             "sha256": hashlib.sha256(payload).hexdigest()})

    def rows(self, name, cursor, total_rows):
        digest = hashlib.sha256()
        written = count = 0
        status, reason = "complete", None
        columns = [d[0] for d in cursor.description]
        entry = {"path": name, "status": "complete", "total_rows": total_rows, "columns": columns}
        if total_rows is None:
            entry["total_rows_exact"] = False
        if self.used >= self.limits.total_bytes:
            self.omitted(name, "total_size_limit", total_rows=total_rows)
            return
        with self.archive.open(name, "w", force_zip64=True) as handle:
            try:
                for row in cursor:
                    payload = (json.dumps(self.redactor.value(dict(zip(columns, row))), ensure_ascii=False, default=str) + "\n").encode()
                    if count >= self.limits.table_rows:
                        status, reason = "partial", "row_limit"
                        break
                    if written + len(payload) > self.limits.table_bytes or self.used + len(payload) > self.limits.total_bytes:
                        status, reason = "partial", "size_limit"
                        break
                    handle.write(payload)
                    digest.update(payload)
                    count += 1
                    written += len(payload)
                    self.used += len(payload)
            except (postgres.Error, ValueError, RecursionError) as exc:
                status, reason = "partial", type(exc).__name__
                from .runtime import exception_details
                details = exception_details(exc)
                entry.update(error=self.redactor.text(details['exception_chain'][0]['message']),
                             **self.redactor.value(details))
        entry.update(status=status, reason=reason, rows=count, bytes=written, sha256=digest.hexdigest())
        self.entries.append(entry)

    def log(self, root, path):
        name = path.relative_to(root).as_posix()
        if not _safe(root, path):
            self.omitted(name, "symlink_or_redirect")
            return
        try:
            before = path.stat()
            if not stat.S_ISREG(before.st_mode):
                self.omitted(name, "not_regular_file")
                return
            if self.used >= self.limits.total_bytes:
                self.omitted(name, "total_size_limit")
                return
            cap = min(self.limits.file_bytes, self.limits.total_bytes - self.used)
            with path.open("rb") as handle:
                if before.st_size > cap:
                    handle.seek(before.st_size - cap)
                    handle.readline(cap)  # discard potentially split log record / UTF-8 prefix
                raw = handle.read(cap)
            # Binary artifacts, compressed archives, screenshots and browser profiles are never bundled.
            if b"\x00" in raw:
                self.omitted(name, "binary_content")
                return
            text = raw.decode("utf-8", errors="replace")
            if re.search(r"\.json(?:\.\d+)?$", path.name):
                try:
                    text = json.dumps(self.redactor.value(json.loads(text)), ensure_ascii=False, indent=2)
                except (ValueError, RecursionError):
                    text = self.redactor.text(text)
            elif re.search(r"\.jsonl(?:\.\d+)?$", path.name):
                lines = []
                for line in text.splitlines():
                    try:
                        lines.append(json.dumps(self.redactor.value(json.loads(line)), ensure_ascii=False))
                    except (ValueError, RecursionError):
                        lines.append(self.redactor.text(line))
                text = "\n".join(lines) + "\n"
            else:
                text = self.redactor.text(text)
            payload = text.encode()
            if self.used + len(payload) > self.limits.total_bytes:
                self.omitted(name, "redacted_output_exceeds_size_limit")
                return
            self.archive.writestr(name, payload)
            self.used += len(payload)
            self.entries.append({"path": name, "status": "partial" if before.st_size > cap else "complete",
                                 "reason": "tail_only" if before.st_size > cap else None,
                                 "source_bytes": before.st_size, "source_mtime": before.st_mtime,
                                 "changed_during_read": path.stat().st_mtime_ns != before.st_mtime_ns,
                                 "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
        except (OSError, ValueError) as exc:
            self.failure(name, exc)


def _query(writer, conn, name, sql):
    try:
        with conn:
            cursor = conn.execute(sql)
            columns = [d[0] for d in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor]
        writer.data(name, rows)
    except (postgres.Error, ValueError) as exc:
        writer.failure(name, exc)


def _database_storage(writer, conn, path, database_name, tables):
    """Native catalog estimates; no whole-table counts or cluster file reads."""
    try:
        with conn:
            facts = [dict(row) for row in conn.execute("""
                SELECT c.relname AS name, c.oid AS table_oid,
                       pg_total_relation_size(c.oid) AS allocated_bytes,
                       GREATEST(c.reltuples,0)::bigint AS estimated_rows,
                       s.last_analyze, s.last_autoanalyze
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                LEFT JOIN pg_stat_user_tables s ON s.relid=c.oid
                WHERE n.nspname=current_schema() AND c.relkind='r'
                ORDER BY c.relname LIMIT 2000
            """)]
        writer.data(f"databases/{database_name}/storage.json", {
            "captured_at": _now(), "engine": "postgresql",
            "allocated_bytes": sum(row["allocated_bytes"] for row in facts),
            "note": "Relation sizes can change concurrently. WAL belongs to the server, not an individual namespace."})
        writer.data(f"databases/{database_name}/table_inventory.json", {
            "tables": facts, "estimates_only": True,
            "note": "Planner row estimates may be stale; no payload bodies were scanned."})
    except (postgres.Error, OSError, ValueError) as exc:
        writer.failure(f"databases/{database_name}/table_inventory.json", exc)

    if database_name != "main" or not any(row[0] == "discovery_payloads" for row in tables):
        return
    # The compressed store keeps cached maintenance facts separately. Export
    # those first, then aggregate only payload headers.
    if any(row[0] == "discovery_storage_meta" for row in tables):
        _query(writer, conn, "summaries/discovery_storage_meta.json",
               "SELECT key,value FROM discovery_storage_meta ORDER BY key LIMIT 200")
    _query(writer, conn, "summaries/discovery_payload_retention.json", """
        SELECT COUNT(*) AS payloads,COALESCE(SUM(raw_bytes),0) AS raw_bytes,
               COALESCE(SUM(LENGTH(data)),0) AS stored_bytes,
               MIN(created_at) AS oldest_created_at,MAX(created_at) AS newest_created_at
        FROM discovery_payloads
    """)


def _summaries(writer, conn, root, database_name):
    if database_name not in {"main", "operations"}:
        return
    from .bundle_queries import MAIN_QUERIES, OPERATIONS_QUERIES
    queries = MAIN_QUERIES if database_name == "main" else OPERATIONS_QUERIES
    if database_name != "main":
        for name, sql in queries.items():
            _query(writer, conn, "summaries/" + name + ".json", sql)
        return
    try:
        from applypilot.discovery.diagnostics import scoring_breakdown
        with conn:
            scoring = scoring_breakdown(conn)
        writer.data("summaries/score_queue_diagnostics.json", scoring)
    except (postgres.Error, ValueError) as exc:
        writer.failure("summaries/score_queue_diagnostics.json", exc)
    policy = None
    minimum = None
    try:
        from applypilot.database import count_jobs_by_stage
        from applypilot import config
        options = {}
        path = root / "autopilot-options.json"
        if _safe(root, path) and path.is_file() and path.stat().st_size <= 2_000_000:
            options = json.loads(path.read_text(encoding="utf-8"))
        minimum = options.get("min_score", config.DEFAULTS["min_score"])
        from applypilot.onboarding.integrations import effective_policy
        policy = effective_policy(root)
        age = options.get("max_age_days", config.DEFAULTS["max_job_age_days"])
        queues = {}
        failures = {}
        for stage in ("enrich", "score", "tailor", "cover", "brief", "apply"):
            try:
                with conn:
                    queues[stage] = count_jobs_by_stage(
                        conn, stage=stage, min_score=minimum, max_age_days=age)
            except (postgres.Error, ValueError) as exc:
                from .runtime import exception_details
                queues[stage] = None
                failures[stage] = {"error_type": type(exc).__name__, **exception_details(exc)}
        writer.data("summaries/admitted_queues.json", {
            "definition": "Shared production admission/lifecycle/freshness counts before fractional batch allocation, filesystem checks, final reservation and live browser checks. Not a promise of dispatch or worker occupancy.",
            "predicate_source": "applypilot.database.count_jobs_by_stage",
            "min_score": minimum, "max_age_days": age, "counts": queues,
            "unavailable": failures})
    except Exception as exc:
        writer.failure("summaries/admitted_queues.json", exc)
    if policy is not None:
        try:
            from applypilot.discovery.buffer import snapshot as buffer_snapshot
            with conn:
                buffer = buffer_snapshot(conn, root, policy=policy, min_score=minimum)
            writer.data("summaries/actionable_buffer.json", buffer)
        except Exception as exc:
            writer.failure("summaries/actionable_buffer.json", exc)
    for name, sql in queries.items():
        _query(writer, conn, "summaries/" + name + ".json", sql)



def _database_diagnostics(writer, conn, name):
    """Metadata only: SQL text, connection strings and bind values are excluded."""
    queries = {
        "activity": """SELECT pid,backend_type,application_name,state,wait_event_type,wait_event,
            backend_start,xact_start,query_start,state_change,
            EXTRACT(EPOCH FROM (clock_timestamp()-xact_start)) AS transaction_age_seconds,
            pg_blocking_pids(pid) AS blocking_pids
            FROM pg_stat_activity WHERE datname=current_database() ORDER BY pid LIMIT 2000""",
        "locks": """SELECT l.pid,l.locktype,l.mode,l.granted,l.relation,l.page,l.tuple,
            l.virtualxid,l.transactionid,l.classid,l.objid,l.objsubid,l.fastpath,
            n.nspname AS schema,c.relname AS relation_name
            FROM pg_locks l LEFT JOIN pg_class c ON c.oid=l.relation
            LEFT JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE l.database IS NULL OR l.database=(SELECT oid FROM pg_database WHERE datname=current_database())
            ORDER BY l.granted,l.pid,l.locktype LIMIT 20000""",
        "settings": """SELECT name,setting,unit,source,pending_restart FROM pg_settings
            WHERE name IN ('max_connections','superuser_reserved_connections','reserved_connections',
                'statement_timeout','lock_timeout','idle_in_transaction_session_timeout',
                'deadlock_timeout','shared_buffers','work_mem','maintenance_work_mem',
                'max_wal_size','checkpoint_timeout','transaction_isolation','default_transaction_read_only')
            ORDER BY name""",
        "database_stats": """SELECT numbackends,xact_commit,xact_rollback,blks_read,blks_hit,
            tup_returned,tup_fetched,tup_inserted,tup_updated,tup_deleted,conflicts,
            temp_files,temp_bytes,deadlocks,blk_read_time,blk_write_time,stats_reset
            FROM pg_stat_database WHERE datname=current_database()""",
        "index_stats": """SELECT schemaname,relname,indexrelname,idx_scan,idx_tup_read,idx_tup_fetch,
            pg_relation_size(indexrelid) AS index_bytes FROM pg_stat_user_indexes
            WHERE schemaname=current_schema() ORDER BY relname,indexrelname LIMIT 2000""",
        "table_stats": """SELECT relname,seq_scan,seq_tup_read,idx_scan,n_live_tup,n_dead_tup,
            n_mod_since_analyze,last_vacuum,last_autovacuum,last_analyze,last_autoanalyze
            FROM pg_stat_user_tables WHERE schemaname=current_schema() ORDER BY relname LIMIT 2000""",
    }
    for category, sql in queries.items():
        _query(writer, conn, f"diagnostics/postgres/{name}/{category}.json", sql)


def _runtime_diagnostics(writer, root):
    # Export-thread and worker-process facts must not be confused. Worker-owned
    # status is captured from live_status in the database; these are LOCAL only.
    writer.data("diagnostics/runtime_policy.json", load_policy(root).model_dump(mode="json"))
    writer.data("diagnostics/local_pools.json", {"pid": os.getpid(), "pools": postgres.pool_stats(),
                "scope": "exporting_process_only", "captured_at": _now()})
    try:
        from applypilot.reasoning.telemetry import diagnostic_snapshot
        writer.data("diagnostics/local_telemetry.json", diagnostic_snapshot())
    except Exception as exc:
        writer.failure("diagnostics/local_telemetry.json", exc)
    frames = sys._current_frames()
    threads = sorted(threading.enumerate(), key=lambda t: t.ident or 0)
    stacks = []
    try:
        for thread in threads[:writer.limits.threads]:
            frame = frames.get(thread.ident)
            stack = []
            while frame is not None and len(stack) < writer.limits.stack_frames:
                stack.append({"file": frame.f_code.co_filename, "function": frame.f_code.co_name,
                              "line": frame.f_lineno})
                frame = frame.f_back
            stacks.append({"id": thread.ident, "name": thread.name, "daemon": thread.daemon,
                           "frames": stack, "truncated": frame is not None})
        writer.data("diagnostics/local_threads.json", {"pid": os.getpid(), "captured_at": _now(),
            "scope": "exporting_process_only; no locals, source text, or other-process stacks",
            "threads": stacks, "omitted_threads": max(0, len(threads)-len(stacks))})
    finally:
        frames.clear()
        frame = None


def _database(writer, root, relative, name, *, sample_rows=False):
    path = root / relative
    if not _safe(root, path) or not path.is_dir():
        writer.omitted("databases/" + name, "missing_or_redirected_database")
        return
    started = _now()
    try:
        with closing(postgres.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
            conn.row_factory = postgres.Row
            conn.raw.execute("SET default_transaction_read_only=on")
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            with conn:
                tables = conn.execute("""SELECT table_name AS name,
                    string_agg(column_name || ' ' || data_type, ', ' ORDER BY ordinal_position) AS columns,
                    to_regclass(quote_ident(table_schema)||'.'||quote_ident(table_name))::oid AS table_oid
                    FROM information_schema.columns WHERE table_schema=current_schema()
                    GROUP BY table_schema,table_name ORDER BY table_name""").fetchall()
                server_version = conn.execute("SELECT version()").fetchone()[0]
            public_tables = tables
            if not sample_rows:
                _database_storage(writer, conn, path, name, tables)
                writer.data(f"databases/{name}/schema.json", [dict(r) for r in public_tables])
                _database_diagnostics(writer, conn, name)
                _summaries(writer, conn, root, name)
            if name == "operations":
                first = ("processes", "active", "incidents", "runs", "events")
            elif name == "learning":
                first = ("tool_calls", "episodes", "tools", "rounds", "revisions")
            else:
                first = ("live_status", "recovery_work", "recovery_meta", "discovery_active_context",
                         "discovery_contexts", "source_health", "application_attempts", "application_routes",
                         "domain_circuit_breakers", "linkedin_governor_state", "discovery_tasks", "jobs")
            priority = {table: index for index, table in enumerate(first)}
            for table, _, _ in sorted(public_tables if sample_rows else (), key=lambda row: (priority.get(row[0], 100), row[0])):
                filename = f"databases/{name}/{table}.jsonl"
                if name == "main" and table == "discovery_payloads":
                    writer.omitted(filename, "compressed_payload_bodies_excluded",
                                   stats="summaries/discovery_payload_retention.json")
                    continue
                permitted = (table in _AUX_PROJECTIONS[name] if name in _AUX_PROJECTIONS else
                             table in _OP_TABLES if name == "operations" else table in _LEARNING_TABLES if name == "learning"
                             else name == "main" and (table in _MAIN_TABLES or table.startswith(_MAIN_PREFIXES)))
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table) or not permitted or table in _EXCLUDED_TABLES:
                    writer.omitted(filename, "private_or_non_operational_table")
                    continue
                try:
                    # Catalog estimates are already in table_inventory.json.
                    # Exact COUNT(*) is not needed to obtain a finite row sample.
                    total = None
                    order = " ORDER BY id DESC" if (name == "operations" and table == "events") or table == "tool_calls" else ""
                    if name == "learning" and table in {"episodes", "revisions", "examples"}:
                        order = " ORDER BY created DESC"
                    elif name == "learning" and table == "rounds":
                        order = " ORDER BY started DESC"
                    projection = _AUX_PROJECTIONS.get(name, {}).get(table, "*")
                    if name == "learning" and table == "episodes":
                        projection = "trace_id,group_id,attempt_id,model,revision,state,verified,infrastructure,created,finished"
                    elif name == "learning" and table == "examples":
                        projection = "id,task,schema_hash,group_id,checks,provenance,params,trace_id,created,split,consumed"
                    elif name == "learning" and table == "tool_calls":
                        projection = ("id,trace_id,server,tool,source,is_error,created,request_started,"
                                      "CASE WHEN lower(server)='gmail' THEN '[Gmail arguments omitted]' ELSE arguments END AS arguments,"
                                      "CASE WHEN lower(server)='gmail' THEN '[Gmail message payload omitted]' ELSE observation END AS observation")
                    with conn:
                        cursor = conn.execute(f'SELECT {projection} FROM "{table}"' + order + ' LIMIT ?', (writer.limits.table_rows + 1,))
                        writer.rows(filename, cursor, total)
                except postgres.Error as exc:
                    writer.failure(filename, exc)
            conn.rollback()
        capture = "rows_capture.json" if sample_rows else "capture.json"
        writer.data(f"databases/{name}/{capture}", {"started_at": started, "finished_at": _now(), "postgres_version": server_version, "read_transaction": True})
    except (postgres.Error, OSError) as exc:
        writer.failure("databases/" + name, exc)


def _selected_run(writer, root, run_id, progress):
    from applypilot.web.work_export import build_report

    path = None
    preferred = set()
    try:
        path, _ = build_report(root, root / "main.pgstore", run_id, progress=progress)
        report = json.loads(path.read_text(encoding="utf-8"))
        writer.data("selected_run/report.json", report)
        for attempt in report.get("committed_records", {}).get("application_attempts", []):
            identifier = str(attempt.get("attempt_id") or "")
            if identifier and re.fullmatch(r"[A-Za-z0-9_.:-]+", identifier):
                preferred.add("logs/application-" + identifier + ".log")
        for link in report.get("application_trace_links", {}).get("links", []):
            trace_id = str(link.get("trace_id") or "")
            if re.fullmatch(r"[a-f0-9]{32}", trace_id):
                preferred.add("logs/agent-traces/" + trace_id + "-")
    except Exception as exc:
        writer.failure("selected_run/report.json", exc)
    finally:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                writer.failure("selected_run/report_cleanup", exc)
    return preferred


def _logs(writer, root, preferred):
    candidates = []
    scanned = 0
    sources = ("logs", "operations/emergency", "operations/selection-snapshots",
               "operations/worker-snapshots", "apply-workers", "temporal")
    for relative in sources:
        folder = root / relative
        if not _safe(root, folder):
            writer.omitted(relative, "symlink_or_redirect")
            continue
        if not folder.is_dir():
            writer.omitted(relative, "missing_directory")
            continue
        if scanned >= writer.limits.scanned_files:
            writer.omitted(relative, "log_scan_limit", inspected_files=scanned)
            continue

        def walk_error(exc):
            writer.failure(relative, exc)

        for directory, dirs, names in os.walk(folder, followlinks=False, onerror=walk_error):
            kept = []
            for name in sorted(dirs):
                path = Path(directory) / name
                if name in {"node_modules", ".git", "chrome-profile", "browser-profile"}:
                    continue
                if _safe(root, path):
                    kept.append(name)
                else:
                    writer.omitted(path.relative_to(root).as_posix(), "symlink_or_redirect")
            dirs[:] = kept
            for name in sorted(names):
                if scanned >= writer.limits.scanned_files:
                    break
                scanned += 1
                extensions = r"\.(?:log|jsonl)(?:\.\d+)?$" if relative in {"apply-workers", "temporal"} else r"\.(?:log|txt|jsonl|json)(?:\.\d+)?$"
                if not re.search(extensions, name, re.I) or _SECRET.search(name):
                    continue
                path = Path(directory) / name
                if not _safe(root, path):
                    writer.omitted(path.relative_to(root).as_posix(), "symlink_or_redirect")
                    continue
                try:
                    info = path.stat()
                    if stat.S_ISREG(info.st_mode):
                        candidates.append((info.st_mtime, path))
                except OSError as exc:
                    writer.failure(path.relative_to(root).as_posix(), exc)
            if scanned >= writer.limits.scanned_files:
                writer.omitted(relative, "log_scan_limit", inspected_files=scanned)
                break
    candidates.sort(key=lambda item: (-item[0], str(item[1])))
    groups = {key: [] for key in ("selected", "workers", "agent", "browser", "rlm", "learning", "models", "diagnostics", "other")}
    for candidate in candidates:
        relative = candidate[1].relative_to(root).as_posix()
        if any(relative == item or (item.endswith("-") and relative.startswith(item)) for item in preferred):
            group = "selected"
        elif relative.startswith(("logs/worker", "logs/application-", "logs/autopilot-worker", "logs/apply-worker", "apply-workers/")):
            group = "workers"
        elif relative.startswith("logs/agent-traces/"):
            match = re.search(r"-(agent|browser|aleph|learning|rlm)\.jsonl", relative)
            group = match[1] if match and match[1] != "aleph" else "agent"
        elif relative.startswith("logs/claude_"):
            group = "agent"
        elif relative.startswith("logs/llm-responses/"):
            group = "models"
        elif relative.startswith(("operations/", "temporal/")):
            group = "diagnostics"
        else:
            group = "other"
        groups[group].append(candidate)
    prioritized = [item for batch in zip_longest(*groups.values()) for item in batch if item is not None]
    for _, path in prioritized[:writer.limits.files]:
        writer.log(root, path)
    for _, path in prioritized[writer.limits.files:]:
        writer.omitted(path.relative_to(root).as_posix(), "file_count_limit")


def build_bundle(root: Path, *, run_id="", limits: DiagnosticPolicy | None = None, progress=None) -> Path:
    """Return a validated, byte/row-bounded ZIP with no query/export deadline."""
    root = Path(root).absolute()
    limits = limits or load_policy(root).diagnostics
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Use an existing, non-symlinked workspace")
    if not _LOCK.acquire(blocking=False):
        raise ValueError("A support export is already being built. Let that request finish before retrying.")
    target = None
    try:
        if shutil.disk_usage(tempfile.gettempdir()).free < min(limits.total_bytes, 128 * 1024 * 1024):
            raise ValueError("Insufficient temporary disk space for a support ZIP")
        fd, filename = tempfile.mkstemp(prefix="applypilot-support-", suffix=".zip")
        os.close(fd)
        target = Path(filename)
        redactor = Redactor(root)
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
            writer = Writer(archive, redactor, limits)
            def phase(name):
                if progress is not None:
                    progress(name)
            phase("configuration")
            started = _now()
            versions = {}
            for package in ("applypilot", "httpx", "starlette", "playwright", "python-jobspy", "openai", "mcp", "dspy", "gepa",
                            "psycopg", "psycopg-pool", "redis", "pydantic"):
                try:
                    versions[package] = importlib.metadata.version(package)
                except importlib.metadata.PackageNotFoundError:
                    versions[package] = None
            writer.data("environment.json", {"captured_at": started, "python": platform.python_version(),
                "os": platform.system(), "os_release": platform.release(), "database_engine": "postgresql",
                "cpu_count": os.cpu_count(), "disk_free_bytes": shutil.disk_usage(root).free, "packages": versions,
                "configured_environment": {key: value for key, value in os.environ.items() if key in {
                    "APPLYPILOT_RESOURCE_WORKERS", "APPLYPILOT_BROWSER_WORKERS", "APPLYPILOT_MIN_SCORE"}},
                "environment_values_not_dumped": True})
            for relative in _CONFIG_FILES:
                path = root / relative
                if not _safe(root, path) or not path.is_file():
                    writer.omitted("configuration/" + relative, "missing_or_redirected")
                    continue
                try:
                    if path.stat().st_size > 2_000_000:
                        raise ValueError("configuration_size_limit")
                    text = path.read_text(encoding="utf-8")
                    if path.suffix in {".yaml", ".yml"}:
                        import yaml
                        value = yaml.safe_load(text)
                    else:
                        value = json.loads(text)
                    writer.data("configuration/" + relative + ("" if relative.endswith(".json") else ".json"), value)
                except (ValueError, OSError, UnicodeError) as exc:
                    writer.failure("configuration/" + relative, exc)
                except Exception as exc:
                    writer.failure("configuration/" + relative, exc)
            from .support import snapshot
            try:
                writer.data("saved_connection_checks.json", snapshot(root))
            except Exception as exc:
                writer.failure("saved_connection_checks.json", exc)
            phase("worker_history")
            from applypilot.web.worker_history import history
            try:
                writer.data("summaries/worker_outcomes.json", history(root, limit=limits.history_files))
            except Exception as exc:
                writer.failure("summaries/worker_outcomes.json", exc)
            phase("runtime_diagnostics")
            _runtime_diagnostics(writer, root)
            code = Path(__file__).resolve().parents[1]
            hashes = {path.relative_to(code).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in sorted(code.rglob("*.py")) if not path.is_symlink()}
            writer.data("code_hashes.json", hashes)
            # Capture small evidence from EVERY namespace before large samples.
            databases = (("operations/events.pgstore", "operations"),
                         ("main.pgstore", "main"), ("learning/learning.pgstore", "learning"),
                         ("free_sources/sources.pgstore", "sources"), ("signals.pgstore", "signals"))
            for relative, database in databases:
                phase(database + "_database_facts")
                _database(writer, root, relative, database)
            preferred = set()
            if run_id:
                phase("selected_run")
                preferred = _selected_run(writer, root, run_id, phase)
            phase("learning_database_rows")
            _database(writer, root, "learning/learning.pgstore", "learning", sample_rows=True)
            phase("recent_logs")
            _logs(writer, root, preferred)
            for relative, database in databases:
                if database == "learning":
                    continue
                phase(database + "_database_rows")
                _database(writer, root, relative, database, sample_rows=True)
            phase("finalizing")
            manifest = {"format": "applypilot_support_bundle", "format_version": 4,
                "scope": "Workspace diagnostics with an optional selected-run report: application and worker logs, tool traces, model and learning diagnostics, saved reports, and database records within declared limits.",
                "selected_run_id": run_id or None,
                "started_at": started, "finished_at": _now(), "limits": limits.model_dump(),
                "uncompressed_export_bytes": writer.used,
                "complete_within_declared_scope": all(e["status"] == "complete" for e in writer.entries),
                "capture_semantics": "Independent read-only transactions per namespace for facts and row samples; snapshots are not globally atomic. Each capture has timestamps. Logs are live file reads. No worker, model, browser, login, send, migration or repair was launched.",
                "privacy": "Credentials and common secret patterns are redacted; private message/contact/answer tables and credential files are excluded. Logs and job prose may still contain personal or sensitive information. Review before sharing. Not a restorable backup.",
                "entries": writer.entries}
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            archive.writestr("README.txt", "ApplyPilot diagnostic support ZIP\n\nStart with manifest.json: errors, redactions and truncation are explicit.\nlogs/: application output, apply workers, agent/browser tool calls, RLM and model responses.\noperations/: saved worker reports, selection diagnostics and emergency records.\ndatabases/: operational records, tool calls, learning evaluations and schema metadata.\ndiagnostics/: runtime policy, PostgreSQL state, local threads and telemetry.\nselected_run/report.json: selected run's journal, outcomes and persisted trace links, when requested.\nThe run selection adds context; workspace logs remain included.\nDatabases are schema + newline-delimited JSON, not restorable PostgreSQL backups.\nDiagnostic rows and recent log tails are sampled within declared limits; inspect omissions in manifest.json.\nReview for sensitive information before sharing.\n")
        # Publishing before ZipFile.close() leaves no central directory. Never
        # advertise a ready artifact until it has a nonempty, readable manifest.
        if target.stat().st_size <= 22:
            raise ValueError("Support export produced an empty archive")
        with zipfile.ZipFile(target) as check:
            manifest = json.loads(check.read("manifest.json"))
            if manifest.get("format") != "applypilot_support_bundle" or check.testzip() is not None:
                raise ValueError("Support archive validation failed")
        return target
    except BaseException:
        if target is not None:
            target.unlink(missing_ok=True)
        raise
    finally:
        _LOCK.release()
