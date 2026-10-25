"""Durable cancel-before-send windows, independent of application admission.

Only initial standing-authorized requests wait. The exact draft, attachment hash,
recipient/context hash and grant are bound to the clock. Delivery transports must
check the clock again inside their atomic reservation; a successful owner cancel
and a successful send reservation therefore cannot both win.
"""
from __future__ import annotations

from applypilot.unattended.budgets import exhausted

from datetime import timedelta

from applypilot import config
from applypilot.referrals import store as s


class GracePending(ValueError):
    """An eligible request is not yet due; keep its approval and original clock."""


def schedule(conn, row, cfg, grant_id):
    if (row['kind'] != 'request' or row['state'] != 'approved' or
            row.get('attempted_at') or row.get('approved_by') != 'standing:' + grant_id):
        return False
    seconds = cfg['send_grace_seconds']
    with s.transaction(conn):
        old = conn.execute('SELECT * FROM unattended_send_windows WHERE outbox_id=?', (row['id'],)).fetchone()
        identity = (row['payload_hash'], row['basis_hash'], grant_id, seconds)
        if old and (old['payload_hash'], old['basis_hash'], old['grant_id'], old['grace_seconds']) == identity:
            return False  # A restart, reapproval or budget deferral never renews it.
        now = s.utc()
        conn.execute('''INSERT INTO unattended_send_windows
            (outbox_id,payload_hash,basis_hash,grant_id,grace_seconds,opened_at,send_after,notified_at)
            VALUES(?,?,?,?,?,?,?,NULL) ON CONFLICT(outbox_id) DO UPDATE SET
            payload_hash=excluded.payload_hash,basis_hash=excluded.basis_hash,
            grant_id=excluded.grant_id,grace_seconds=excluded.grace_seconds,
            opened_at=excluded.opened_at,send_after=excluded.send_after,notified_at=NULL''',
            (row['id'], *identity, s.iso(now), s.iso(now + timedelta(seconds=seconds))))
    return True


def arm_approved(conn, cfg):
    """Also cover drafts approved before the upgrade; no external side effects."""
    from .policy import active
    grant = active(conn, cfg)
    if not grant:
        return 0
    count = 0
    with s.transaction(conn):
        for raw in conn.execute("SELECT * FROM referral_outbox WHERE state='approved' AND kind='request' AND attempted_at IS NULL").fetchall():
            count += int(schedule(conn, dict(raw), cfg, grant['id']))
    return count


def require_due(conn, row, cfg, grant_id):
    """Mandatory for both Gmail and social sends; never infer a delivered request."""
    if row['kind'] != 'request' or cfg['send_grace_seconds'] == 0:
        return
    window = conn.execute('SELECT * FROM unattended_send_windows WHERE outbox_id=?', (row['id'],)).fetchone()
    if (not window or window['payload_hash'] != row['payload_hash'] or
            window['basis_hash'] != row['basis_hash'] or window['grant_id'] != grant_id or
            window['grace_seconds'] != cfg['send_grace_seconds']):
        raise GracePending('Request needs its own cancel-before-send window')
    if s.utc(window['send_after']) > s.utc():
        raise GracePending('Owner cancel window runs until ' + window['send_after'])


def pending(conn, *, include_body=False):
    rows = conn.execute('''SELECT w.*,o.channel,o.payload_json,c.name,j.title,j.company
        FROM unattended_send_windows w JOIN referral_outbox o ON o.id=w.outbox_id
        JOIN referral_contacts c ON c.id=o.contact_id
        JOIN referral_cases rc ON rc.id=o.case_id JOIN jobs j ON j.url=rc.job_url
        JOIN referral_auto_grants g ON g.id=w.grant_id AND g.revoked_at IS NULL
        WHERE o.state='approved' AND o.attempted_at IS NULL AND o.kind='request'
        AND o.approved_by=('standing:' || w.grant_id)
        AND w.payload_hash=o.payload_hash AND w.basis_hash=o.basis_hash
        AND w.grace_seconds>0 ORDER BY w.send_after,w.outbox_id LIMIT 100''').fetchall()
    values = []
    for r in rows:
        item = {'outbox_id': r['outbox_id'], 'channel': r['channel'],
                'name': r['name'], 'company': r['company'], 'title': r['title'],
                'opened_at': r['opened_at'], 'send_after': r['send_after'],
                'status': 'cancel_window' if s.utc(r['send_after']) > s.utc() else 'eligible_to_send',
                'cancel_command': 'applypilot autopilot --cancel-outreach ' + r['outbox_id']}
        if include_body:
            payload = s.obj(r['payload_json'])
            item.update(recipient=payload.get('to'), subject=payload.get('subject'), body=payload.get('body'),
                        attachment=(payload.get('attachment') or {}).get('name'), phase=payload.get('phase'))
        values.append(item)
    return values


def publish(conn):
    from .maintenance import _write
    path = config.APP_DIR / 'outreach-pending.json'
    _write(path, {'at': s.iso(), 'requests': pending(conn, include_body=True),
                  'note': 'Private exact-draft preview, not an application hold or a delivery receipt.'})
    return path


def cancel(conn, outbox_id):
    """Explicit owner veto. A successful cancellation cannot subsequently send."""
    from applypilot.referrals.outreach import _outbox
    with s.transaction(conn):
        row = _outbox(conn, outbox_id)
        if row['state'] == 'cancelled':
            return {'status': 'already_cancelled', 'outbox_id': outbox_id}
        if row['state'] not in {'draft', 'approved'} or row['attempted_at']:
            raise ValueError('Dispatch has already started or finished; this request cannot be recalled here')
        conn.execute("UPDATE referral_outbox SET state='cancelled',approved_hash=NULL,error_kind='owner_veto',updated_at=? WHERE id=?",
                     (s.iso(), outbox_id))
        s.release(conn, row['case_id'], 'Owner cancelled this outreach; applications should continue normally.')
    preview_updated = True
    try:
        publish(conn)
    except OSError:
        preview_updated = False  # Cancellation is already durable; report it accurately.
    return {'status': 'cancelled', 'outbox_id': outbox_id, 'applications': 'continue',
            'preview_updated': preview_updated,
            'note': 'This draft will not be recreated automatically. Already submitted applications are unaffected.'}
