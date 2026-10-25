"""Read-only referral demand and prerequisite inventory."""
from __future__ import annotations


def preparation_policy_hash(conn):
    from applypilot.referrals import store
    from applypilot.unattended.settings import load
    return store.digest(store.dump({'referrals':store.policy(conn),'networking':load()}))


def pending_case_rows(conn, min_score, *, limit=None):
    """Viable selected prerequisites; completed empty research is not backlog."""
    from applypilot.discovery.admission import current_admission_sql
    checked = "ap_json_text(c.plan_json,'$.preparation.checked_at')"
    useful = ("AND (" + checked + " IS NULL "
              "OR ap_json_text(c.plan_json,'$.preparation.policy_hash')!=? "
              "OR EXISTS(SELECT 1 FROM referral_contacts rc WHERE rc.company_key=c.company_key "
              "AND ap_day(rc.updated_at)>ap_day(" + checked + ")) "
              "OR EXISTS(SELECT 1 FROM referral_outbox changed WHERE changed.case_id=c.id "
              "AND ap_day(changed.updated_at)>ap_day(" + checked + ")) "
              "OR EXISTS(SELECT 1 FROM application_outcomes ao JOIN referral_job_links links ON links.job_url=ao.job_url "
              "WHERE links.case_id=c.id AND ap_day(ao.created_at)>ap_day(" + checked + "))) ")
    sql = ("SELECT c.* FROM referral_cases c LEFT JOIN jobs j ON j.url=c.job_url " +
           " WHERE c.state IN ('research','draft') AND COALESCE(j.state,'')!='archived' "
           "AND (c.deadline_at IS NULL OR ap_day(c.deadline_at)>ap_day('now')) "
           "AND ((c.case_type='posting' AND ((j.fit_score>=? AND " + current_admission_sql(stage="prepare") +
           ") OR EXISTS(SELECT 1 FROM application_attempts aa WHERE aa.job_url=j.url AND aa.status='submitted') "
           "OR EXISTS(SELECT 1 FROM feedback_submissions fs WHERE fs.job_url=j.url))) "
           "OR (c.case_type='company' AND EXISTS(SELECT 1 FROM company_targets ct WHERE ct.id=c.target_id "
           "AND ct.state='active' AND ct.action IN ('outreach','both')))) " + useful +
           " AND NOT EXISTS(SELECT 1 FROM referral_outbox old WHERE old.case_id=c.id "
           "AND (old.attempted_at IS NOT NULL OR old.dispatch_token IS NOT NULL OR old.state!='cancelled')) "
           "ORDER BY c.updated_at,c.id")
    params = [min_score, preparation_policy_hash(conn)]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(max(0, int(limit)))
    return conn.execute(sql, params).fetchall()


def referral_inventory(conn, *, profile=None, channels=None):
    from applypilot import config
    from applypilot.referrals import outreach, store
    from applypilot.referrals.autonomy import validate_automatic
    tables = {row[0] for row in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname=current_schema()")}
    if not {"referral_outbox", "referral_cases", "referral_settings"} <= tables:
        return {"stock": 0, "preparation_stock": 0, "runnable": 0, "ready_ids": [], "available": True}
    cfg = store.policy(conn)
    if cfg["mode"] == "off":
        return {"stock": 0, "preparation_stock": 0, "runnable": 0, "ready_ids": [], "available": True}
    profile = config.load_profile() if profile is None else profile
    stock, ready, deferred = set(), [], []
    rows = conn.execute("SELECT * FROM referral_outbox WHERE state IN ('draft','approved','sending') "
                        "AND channel IN ('email','linkedin','x') ORDER BY created_at,id")
    for record in rows:
        row = dict(record)
        if channels is not None and row["channel"] not in channels:
            continue
        try:
            payload, person, case = outreach._validate_current(conn, row, profile=profile)
            stock.add(row["case_id"])
            if row["state"] != "approved" or row["approved_hash"] != row["payload_hash"]:
                deferred.append({'outbox_id':row['id'], 'reason_code':'awaiting_authorization_or_reconciliation',
                                 'reason':'Outreach is not approved for dispatch'})
                continue
            if cfg["mode"] not in {"active", "unattended"}:
                continue
            if row["channel"] != "email" or str(row.get("approved_by") or "").startswith("standing:"):
                validate_automatic(conn, row, profile=profile, for_send=True)
            outreach._check_outreach_limits(conn, row, person, case)
            ready.append(row["id"])
        except (ValueError, OSError) as exc:
            deferred.append({'outbox_id':row['id'], **getattr(exc,'decision',{}), 'reason':str(exc)})
            continue
    pending_cases = pending_case_rows(conn, cfg['min_fit_score'])
    from applypilot.targets.priority import select, case_priority
    replies, introductions = [], []
    for identifier in ready:
        row = conn.execute('SELECT id,case_id,kind FROM referral_outbox WHERE id=?', (identifier,)).fetchone()
        (replies if row['kind'].startswith('reply:') else introductions).append(row)
    introductions = select(conn, introductions, len(introductions), lane='outreach-delivery',
                           priority=lambda row: case_priority(conn,row['case_id']))
    ready = [row['id'] for row in (*replies, *introductions)]
    preparation_stock = sum(row['id'] not in stock for row in pending_cases)
    return {"stock": len(stock), "preparation_stock": preparation_stock,
            "runnable": len(ready), "ready_ids": ready, "deferred":deferred, "available": True}
