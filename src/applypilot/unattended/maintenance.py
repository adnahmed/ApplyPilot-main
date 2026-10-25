"""Local daily snapshots and redacted owner alerts, independent of Gmail health."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import urllib.request
from applypilot import config
from applypilot.referrals import store as s


def _write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    # The owner cancel command and networking worker can publish concurrently.
    fd,tmp=tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(value,f,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
        from applypilot.atomic_io import replace_with_retry
        replace_with_retry(tmp,path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def digest(conn):
    rows=conn.execute('SELECT apply_status,COUNT(*) n FROM jobs GROUP BY apply_status').fetchall()
    states={str(r[0] or 'not_applied'):r[1] for r in rows}
    outreach={r[0]:r[1] for r in conn.execute('SELECT state,COUNT(*) FROM referral_outbox GROUP BY state')}
    incidents=[]
    outcomes={r[0]:r[1] for r in conn.execute("SELECT event_type,COUNT(*) FROM application_outcomes WHERE voided_at IS NULL GROUP BY event_type")}
    from .grace import pending
    from applypilot.opportunities import next_actions
    # The owner opens the local opportunity to read correspondence and proof.
    # Webhook summaries never copy message bodies, addresses or application answers.
    labels = {'employer_response': 'Respond to employer engagement',
              'relationship_response': 'Respond to a professional contact',
              'message_review': 'Resolve an employer message',
              'unlinked_message': 'Identify and respond to an incoming message',
              'employer_requirement': 'Supply the requested application material',
              'practice': 'Complete the next practice answer'}
    actions = [{key: action.get(key) for key in ('id', 'kind', 'actor', 'company', 'role',
                'deadline', 'deadline_basis', 'deadline_label', 'state', 'target_url')}
               | {'title': labels.get(action['kind'], 'Open the next opportunity action')}
               for action in next_actions(conn, limit=5)]
    return {'pending_outreach':pending(conn),'at':s.iso(),'applications':states,'outcomes':outcomes,'outreach':outreach,'incidents':incidents,
            'next_action': actions[0] if actions else None, 'next_actions': actions,
            'note':'Owner actions come from saved engagement and preparation records. Counts are not proof of a hire or ATS referral.'}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise ValueError('Alert webhook redirects are not followed')


def notify(conn, cfg, *, notification_id):
    """Publish one owner digest; its stable receipt prevents duplicate webhooks."""
    from applypilot.orchestration import store
    value = digest(conn)
    _write(config.APP_DIR / 'unattended-status.json', value)
    url = os.environ.get(cfg['alerts_webhook_env'], '').strip()
    if not url:
        return {'state': 'local_file'}
    receipt_id = 'network-notification:' + notification_id
    with s.transaction(conn):
        if store.receipt(conn, receipt_id):
            return {'state': 'already_attempted'}
        store.save_receipt(conn, receipt_id, {'state': 'attempted'})
    from urllib.parse import urlsplit
    s.public_url(url)
    if urlsplit(url).scheme != 'https':
        raise ValueError('Use an HTTPS owner notification webhook')
    request = urllib.request.Request(url,
        data=json.dumps({'text': 'ApplyPilot: ' + json.dumps(value, ensure_ascii=False)}).encode(),
        headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
        if not 200 <= response.status < 300:
            raise ValueError('Webhook rejected the notification')
        response.read(4096)
    return {'state': 'delivered'}
