"""Company conversations reuse the existing contacts, outbox, and delivery guards."""
from __future__ import annotations

from uuid import uuid4

from applypilot.referrals import store as referrals
from . import store


def open_company_case(conn, target_id):
    target = store.get(conn, target_id)
    if target['state'] != 'active' or target['action'] not in {'outreach', 'both'}:
        raise ValueError('Target does not request outreach')
    if not target['company']:
        raise ValueError('Resolve the company before outreach')
    with referrals.transaction(conn):
        existing = conn.execute("SELECT * FROM referral_cases WHERE target_id=? AND case_type='company'", (target_id,)).fetchone()
        cid = existing['id'] if existing else uuid4().hex
        plan = referrals.obj(existing['plan_json']) if existing else {}
        plan.update(target_revision=target['revision'], owner_instructions=target['instructions'])
        conn.execute("""INSERT INTO referral_cases
            (id,posting_key,job_url,company_key,title,base_target,state,plan_json,created_at,updated_at,case_type,target_id)
            VALUES(?,?,NULL,?,?,?,'research',?,?,?,'company',?) ON CONFLICT(id) DO UPDATE SET
            company_key=excluded.company_key,title=excluded.title,base_target=excluded.base_target,
            plan_json=excluded.plan_json,updated_at=excluded.updated_at""",
            (cid, 'company-target:' + target_id, referrals.company_key(target['company']),
             ', '.join(target['roles']) or 'Company opportunities', target['website'], referrals.dump(plan),
             store.now(), store.now(), target_id))
        supplied = conn.execute("""SELECT c.id FROM company_target_contacts tc JOIN referral_contacts c ON c.id=tc.contact_id
            WHERE tc.target_id=? AND tc.supplied=1 AND (c.email=? OR c.profile_url=?)""",
            (target_id, target['email'] or None, target['profile_url'] or None)).fetchone()
        if (target['email'] or target['profile_url']) and not supplied:
            # The owner explicitly supplied this recipient for this company's outreach.
            contact = referrals.add_contact(conn, {
                'name': target['contact_name'] or target['contact_role'] or 'Hiring team',
                'company': target['company'], 'role_function': target['contact_role'],
                'kind': 'hiring_manager', 'email': target['email'], 'profile_url': target['profile_url'],
                'source': 'owner-target', 'source_note': 'Recipient supplied for target ' + target_id,
                'email_allowed': bool(target['email']), 'email_basis': 'public_professional',
                'permission_note': 'Owner-directed professional outreach for target ' + target_id,
            })
            contact_id = contact['id']
            conn.execute("INSERT INTO company_target_contacts VALUES(?,?,1,?) ON CONFLICT(target_id,contact_id) DO NOTHING",
                         (target_id, contact_id, store.now()))
        contacts = [dict(r) for r in conn.execute("""SELECT c.* FROM referral_contacts c
            LEFT JOIN company_target_contacts tc ON tc.contact_id=c.id AND tc.target_id=?
            WHERE c.company_key=? AND c.blocked_at IS NULL
            ORDER BY COALESCE(tc.supplied,0) DESC,c.verified_at DESC,c.id""", (target_id, referrals.company_key(target['company'])))]
        if target['email'] or target['profile_url']:
            contacts = [c for c in contacts if (target['email'] and c.get('email') == target['email']) or
                        (target['profile_url'] and c.get('profile_url') == target['profile_url'])]
        plan['candidates'] = [{'contact_id': c['id'], 'alignment': 'owner_directed_company', 'hold_eligible': False} for c in contacts[:1]]
        for contact in contacts[:1]:
            conn.execute("INSERT INTO company_target_contacts VALUES(?,?,0,?) ON CONFLICT(target_id,contact_id) DO NOTHING",
                         (target_id, contact['id'], store.now()))
        conn.execute('UPDATE referral_cases SET plan_json=? WHERE id=?', (referrals.dump(plan), cid))
    return referrals.require_case(conn, cid)


def prepare(conn, profile, cfg):
    from applypilot.referrals.planner import create_initial_drafts
    from applypilot.targets.priority import select
    rows = [store.decode(row) for row in conn.execute("""SELECT * FROM company_targets
        WHERE state='active' AND action IN ('outreach','both') AND research_state IN ('complete','watching')""")]
    rows = select(conn, rows, max(0, cfg['research_jobs_per_cycle']), lane='company-outreach',
                  priority=lambda row: row['priority'], consume=True)
    for target in rows:
        case = open_company_case(conn, target['id'])
        try:
            store.require_outreach(conn, case)
            create_initial_drafts(conn, [case], profile)
        except ValueError as exc:
            with referrals.transaction(conn):
                conn.execute('UPDATE company_targets SET blocker=? WHERE id=?', (str(exc), target['id']))
