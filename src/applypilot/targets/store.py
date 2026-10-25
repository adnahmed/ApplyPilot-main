"""Persistent owner intent. Network and model calls never run in these transactions."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import uuid4

from applypilot.discovery.repository import transaction

ACTIONS = {
    "watch": "Watch using search preferences",
    "find": "Find jobs only", "apply": "Find and apply",
    "outreach": "Outreach only", "both": "Apply plus outreach",
}
PRIORITIES = {"high": 4, "normal": 2, "low": 1}
CADENCES = {"daily": 1, "weekly": 7, "once": None}
FIELDS = ("company", "website", "careers_url", "job_url", "contact_name", "contact_role",
          "email", "profile_url", "instructions", "action", "priority", "cadence",
          "roles", "employment_types", "locations", "work_arrangements")
LIST_FIELDS = ("roles", "employment_types", "locations", "work_arrangements")


def now():
    return datetime.now(timezone.utc).isoformat()


def dump(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def ensure_schema(conn):
    for sql in (
        """CREATE TABLE IF NOT EXISTS company_targets (
            id TEXT PRIMARY KEY, identity_key TEXT UNIQUE NOT NULL, company TEXT NOT NULL,
            website TEXT NOT NULL DEFAULT '', config_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL DEFAULT 'active', priority TEXT NOT NULL DEFAULT 'high',
            action TEXT NOT NULL, last_checked_at TEXT,
            research_state TEXT NOT NULL DEFAULT 'queued', blocker TEXT NOT NULL DEFAULT '',
            evidence_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS company_target_jobs (
            target_id TEXT NOT NULL REFERENCES company_targets(id), job_url TEXT NOT NULL REFERENCES jobs(url),
            revision INTEGER NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(target_id,job_url))""",
        """CREATE TABLE IF NOT EXISTS company_target_contacts (
            target_id TEXT NOT NULL REFERENCES company_targets(id), contact_id TEXT NOT NULL,
            supplied INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, PRIMARY KEY(target_id,contact_id))""",
        """CREATE TABLE IF NOT EXISTS company_target_observations (
            observation_id TEXT PRIMARY KEY, target_id TEXT NOT NULL REFERENCES company_targets(id),
            revision INTEGER NOT NULL, observed_at TEXT NOT NULL, evidence_json TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS company_target_imports (
            id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, rows_json TEXT NOT NULL,
            created_at TEXT NOT NULL, committed_at TEXT, result_json TEXT)""",
        "CREATE INDEX IF NOT EXISTS idx_target_jobs_url ON company_target_jobs(job_url)",
        "ALTER TABLE company_targets ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'operator'",
        """CREATE TABLE IF NOT EXISTS company_target_boards (
            target_id TEXT NOT NULL REFERENCES company_targets(id), board_slug TEXT NOT NULL,
            config_json TEXT NOT NULL, config_hash TEXT NOT NULL,
            updated_at TEXT NOT NULL, PRIMARY KEY(target_id,board_slug))""",
    ):
        conn.execute(sql)


def available(conn):
    return conn is not None and bool(conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema=current_schema() AND table_name='company_targets'"
    ).fetchone())


def _url(value):
    value = str(value or "").strip()
    if not value:
        return ""
    from applypilot.integrations.net import public_url
    from applypilot.integrations.types import SourceError
    if "://" not in value:
        value = "https://" + value
    try:
        return public_url(value, resolve=False)
    except SourceError as exc:
        raise ValueError(str(exc)) from exc


def validate(data):
    if not isinstance(data, dict) or set(data) - {*FIELDS, "revision", "id"}:
        raise ValueError("Unknown target field")
    value = {key: str(data.get(key) or "").strip() for key in FIELDS if key not in LIST_FIELDS}
    for key in ("website", "careers_url", "job_url", "profile_url"):
        value[key] = _url(value[key])
    if not (value["company"] or value["website"] or value["job_url"]):
        raise ValueError("Enter a company name, website, or job URL")
    value["priority"] = value["priority"] or "high"
    value["cadence"] = value["cadence"] or "daily"
    if value["action"] not in ACTIONS:
        raise ValueError("Choose what ApplyPilot should do for this target")
    if value["priority"] not in PRIORITIES or value["cadence"] not in CADENCES:
        raise ValueError("Invalid target priority or checking schedule")
    if value["email"]:
        from applypilot.referrals.store import email_address
        value["email"] = email_address(value["email"])
    if value["profile_url"]:
        from applypilot.referrals.identity import profile_key
        if not profile_key(value["profile_url"]):
            raise ValueError("Use a supported professional profile URL")
    for key in LIST_FIELDS:
        raw = data.get(key) or []
        if isinstance(raw, str):
            raw = re.split(r"[,;\n]", raw)
        if not isinstance(raw, list) or any(not isinstance(x, str) for x in raw):
            raise ValueError(f"{key} must be a list of values")
        value[key] = list(dict.fromkeys(x.strip() for x in raw if x.strip()))
    from applypilot.discovery.models import EmploymentType, WorkMode
    if set(value["employment_types"]) - {x.value for x in EmploymentType if x.value != "unknown"}:
        raise ValueError("Invalid employment type")
    if set(value["work_arrangements"]) - {x.value for x in WorkMode if x.value != "unknown"}:
        raise ValueError("Invalid work arrangement")
    if any(len(x) > (16000 if key == "instructions" else 2000) for key, x in value.items() if isinstance(x, str)):
        raise ValueError("A target field exceeds its maximum length")
    return value


def identity(value):
    from applypilot.referrals.store import company_key
    key = ("job:" + value["job_url"] if value.get("job_url") else
           "site:" + (urlsplit(value["website"]).hostname or "").removeprefix("www.") if value.get("website") else
           "company:" + company_key(value.get("company")))
    return hashlib.sha256(key.casefold().encode()).hexdigest()


def decode(row):
    value = dict(row)
    value.update(json.loads(value.pop("config_json")))
    value["evidence"] = json.loads(value.pop("evidence_json", "{}"))
    return value


def get(conn, target_id):
    row = conn.execute("SELECT * FROM company_targets WHERE id=?", (target_id,)).fetchone()
    if not row:
        raise LookupError("Target not found")
    return decode(row)


def list_targets(conn, q=None):
    if not available(conn):
        return {"items": [], "total": 0}
    q = q or {}
    rows = [decode(row) for row in conn.execute("SELECT * FROM company_targets ORDER BY created_at DESC,id")]
    if q.get("q"):
        needle = str(q["q"]).casefold()
        rows = [x for x in rows if needle in (x["company"] + " " + x["website"] + " " + x["instructions"]).casefold()]
    if q.get("state"):
        rows = [x for x in rows if x["state"] == q["state"]]
    else:
        rows = [x for x in rows if x["state"] != "archived"]
    for row in rows:
        row.update(progress(conn, row))
    return {"items": rows, "total": len(rows)}


def progress(conn, target):
    jobs = [dict(r) for r in conn.execute("""SELECT j.url,j.title,j.company,j.state,j.apply_status,
        j.fit_score,j.discovery_disposition FROM company_target_jobs l JOIN jobs j ON j.url=l.job_url
        WHERE l.target_id=? ORDER BY j.fit_score DESC NULLS LAST,j.url""", (target["id"],))]
    outbox = [dict(r) for r in conn.execute("""SELECT o.id,o.state,o.channel,o.sent_at,o.error_kind
        FROM referral_outbox o JOIN referral_cases c ON c.id=o.case_id
        WHERE c.target_id=? ORDER BY o.created_at DESC""", (target["id"],))]
    return {"jobs": jobs, "outbox": outbox, "openings": len(jobs),
            "application_status": application_state(conn, target),
            "outreach_status": outbox[0]["state"] if outbox else
                ("waiting" if target["action"] in {"outreach", "both"} else "not_requested")}


def _invalidate(conn, target_id):
    conn.execute("""UPDATE jobs SET discovery_assessed_content_hash=NULL,
        discovery_prepare_allowed=0,discovery_apply_allowed=0
        WHERE url IN (SELECT job_url FROM company_target_jobs WHERE target_id=?)""", (target_id,))
    conn.execute("""UPDATE referral_outbox SET state='draft',approved_hash=NULL,approved_at=NULL,
        approved_by=NULL,updated_at=? WHERE attempted_at IS NULL AND state IN ('draft','approved')
        AND case_id IN (SELECT id FROM referral_cases WHERE target_id=?)""", (now(), target_id))


def save(conn, data, target_id=None, *, origin='operator'):
    if origin not in {'operator', 'company_discovery'}:
        raise ValueError('Invalid target origin')
    value = validate(data)
    with transaction(conn, resource="company-targets"):
        old = get(conn, target_id) if target_id else None
        if old and str(data.get("revision")) != str(old["revision"]):
            raise ValueError("This target changed. Reload before saving your edits")
        existing = conn.execute("SELECT id FROM company_targets WHERE identity_key=?", (identity(value),)).fetchone()
        if existing and existing[0] != target_id:
            if origin == 'operator':
                conn.execute("UPDATE company_targets SET origin='operator' WHERE id=?", (existing[0],))
            return {"target": get(conn, existing[0]), "duplicate": True}
        target_id = target_id or uuid4().hex
        revision = old["revision"] + 1 if old else 1
        stamp = now()
        conn.execute("""INSERT INTO company_targets
            (id,identity_key,company,website,config_json,revision,priority,action,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            identity_key=excluded.identity_key,company=excluded.company,website=excluded.website,
            config_json=excluded.config_json,revision=excluded.revision,priority=excluded.priority,
            action=excluded.action,research_state='queued',blocker='',updated_at=excluded.updated_at""",
            (target_id, identity(value), value["company"], value["website"], dump(value), revision,
             value["priority"], value["action"], stamp, stamp))
        conn.execute('UPDATE company_targets SET origin=? WHERE id=?', (origin, target_id))
        if old:
            _invalidate(conn, target_id)
        return {"target": get(conn, target_id), "duplicate": False}


def control(conn, target_id, action, revision):
    if action not in {"pause", "resume", "archive", "run"}:
        raise ValueError("Unknown target action")
    with transaction(conn, resource="company-targets"):
        target = get(conn, target_id)
        if str(revision) != str(target["revision"]):
            raise ValueError("This target changed. Reload before continuing")
        state = {"pause": "paused", "archive": "archived"}.get(action, "active")
        if action in {'resume', 'run'}:
            conn.execute("UPDATE company_targets SET origin='operator' WHERE id=?", (target_id,))
        conn.execute("""UPDATE company_targets SET state=?,revision=revision+1,
            research_state=?,blocker='',updated_at=? WHERE id=?""",
            (state, "queued" if state == "active" else state, now(), target_id))
        _invalidate(conn, target_id)
        return get(conn, target_id)


def snapshot(conn):
    if not available(conn):
        return "[]"
    return dump([{key: row[key] for key in ("id", "revision", "state", "company", "website", "job_url", "action", *LIST_FIELDS)}
                 for row in (decode(r) for r in conn.execute("SELECT * FROM company_targets ORDER BY id"))])


def job_targets(conn, url):
    return [decode(r) for r in conn.execute("""SELECT t.* FROM company_targets t
        JOIN company_target_jobs l ON l.target_id=t.id WHERE l.job_url=?
        ORDER BY CASE t.priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,t.created_at,t.id""", (url,))]


def permit_sql(alias="j", action="apply"):
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z_0-9]*", alias):
        raise ValueError("Invalid SQL alias")
    allowed = "'watch','apply','both'" if action == "apply" else "'watch','outreach','both'"
    return f"""NOT EXISTS (SELECT 1 FROM company_target_jobs tl JOIN company_targets ct ON ct.id=tl.target_id
        WHERE tl.job_url={alias}.url AND (ct.state!='active' OR ct.action NOT IN ({allowed}) OR tl.revision!=ct.revision))"""


def application_state(conn, target):
    if target['action'] == 'watch':
        return 'following_search_preferences'
    if target["action"] not in {"apply", "both"}:
        return "not_requested"
    submitted = conn.execute("""SELECT j.url,j.title FROM company_target_jobs l JOIN jobs j ON j.url=l.job_url
        WHERE l.target_id=? AND (EXISTS (SELECT 1 FROM application_attempts a WHERE a.job_url=j.url AND a.status='submitted')
        OR EXISTS (SELECT 1 FROM feedback_submissions f WHERE f.job_url=j.url)) LIMIT 1""", (target["id"],)).fetchone()
    if submitted:
        return "submitted"
    jobs = conn.execute("""SELECT j.* FROM jobs j JOIN company_target_jobs l ON l.job_url=j.url WHERE l.target_id=?""",
                        (target["id"],)).fetchall()
    if conn.execute("""SELECT 1 FROM application_attempts a JOIN company_target_jobs l ON l.job_url=a.job_url
        WHERE l.target_id=? AND a.status IN ('prepared','side_effect_intent','verification_required','verification_quarantined')""",
                    (target["id"],)).fetchone():
        return "uncertain"
    if target["research_state"] not in {"complete", "watching"}:
        return "pending"
    terminal = {"rejected", "failed", "archived", "withdrawn", "expired", "closed", "skipped", "ineligible"}
    for item in jobs:
        job = dict(item)
        if job.get("agent_id") or job.get("apply_status") == "in_progress":
            return "pending"
        if job.get("state") not in terminal and job.get("apply_status") not in terminal:
            if job.get("discovery_disposition") != "blocked" and job.get("discovery_source_status") != "closed":
                return "pending"
    return "no_match" if not jobs else "finished_without_submission"


def require_outreach(conn, case):
    target_id = case.get("target_id")
    if not target_id:
        if case.get("job_url") and not conn.execute(
            f"SELECT 1 FROM jobs j WHERE j.url=? AND {permit_sql(action='outreach')}", (case["job_url"],)).fetchone():
            raise ValueError("Target instructions do not permit outreach")
        return None
    target = get(conn, target_id)
    if target["state"] != "active" or target["action"] not in {"outreach", "both"}:
        raise ValueError("Target outreach is paused or not requested")
    if target["action"] == "both" and application_state(conn, target) in {"pending", "uncertain"}:
        raise ValueError("Waiting for the application outcome before outreach")
    return target


def boards(conn, target_id):
    return [json.loads(row['config_json'])
            for row in conn.execute('SELECT * FROM company_target_boards WHERE target_id=? ORDER BY updated_at,board_slug',
                                    (target_id,))]


def attach_boards(conn, target_id, values):
    """Keep distinct boards and replace changed configurations atomically."""
    from applypilot.discovery.jobseek import normalize_board
    from applypilot.integrations.types import stable_id
    normalized = {board['board_slug']: board for board in map(normalize_board, values)}
    changed = False
    with transaction(conn, resource='company-targets'):
        get(conn, target_id)
        for slug, board in normalized.items():
            digest = stable_id(board)
            old = conn.execute('SELECT config_hash FROM company_target_boards WHERE target_id=? AND board_slug=?',
                               (target_id, slug)).fetchone()
            if old and old['config_hash'] == digest:
                continue
            conn.execute("""INSERT INTO company_target_boards(target_id,board_slug,config_json,config_hash,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(target_id,board_slug) DO UPDATE SET config_json=excluded.config_json,
                config_hash=excluded.config_hash,updated_at=excluded.updated_at""",
                (target_id, slug, dump(board), digest, now()))
            changed = True
        if changed:
            conn.execute("""UPDATE company_targets SET revision=revision+1,research_state='queued',
                blocker='',updated_at=? WHERE id=?""", (now(), target_id))
            _invalidate(conn, target_id)
    return changed


def record_observation(conn, target_id, revision, observation_id, evidence):
    """Publish immutable research evidence; Temporal owns checking cadence."""
    if not isinstance(observation_id, str) or not re.fullmatch(r"[a-f0-9]{64}", observation_id):
        raise ValueError("An immutable target observation identity is required")
    with transaction(conn, resource="company-targets"):
        target = get(conn, target_id)
        if target["revision"] != revision or target["state"] != "active":
            raise ValueError("Target changed before publication")
        prior = conn.execute("SELECT * FROM company_target_observations WHERE observation_id=?",
                             (observation_id,)).fetchone()
        if prior is not None:
            if (prior["target_id"] != target_id or prior["revision"] != revision
                    or prior["evidence_json"] != dump(evidence)):
                raise ValueError("Target observation identity was reused for different evidence")
            return
        stamp = now()
        conn.execute("""INSERT INTO company_target_observations
            (observation_id,target_id,revision,observed_at,evidence_json) VALUES(?,?,?,?,?)""",
            (observation_id, target_id, revision, stamp, dump(evidence)))
        days = CADENCES[target["cadence"]]
        conn.execute("""UPDATE company_targets SET research_state=?,blocker='',last_checked_at=?,
            evidence_json=?,updated_at=? WHERE id=?""",
            ("researching" if evidence.get("careers_coverage") == "in_progress" else "watching" if days else "complete", stamp, dump(evidence), stamp, target_id))
