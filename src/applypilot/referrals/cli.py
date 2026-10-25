"""Opt-in networking commands. Ordinary feedback/track remain read-only Gmail clients."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

import typer
from applypilot.referrals import store as s
from applypilot.tracking.feedback_cli import connection, output

app = typer.Typer(help='Selective networking with reviewed or standing-authorized Gmail and exact application linkage.', no_args_is_help=True)


def db(write=False):
    conn = connection(write)
    try:
        conn.execute('SELECT version FROM referral_schema').fetchone()
    except Exception as exc:
        raise typer.BadParameter('run referrals configure after migrating this patch') from exc
    return conn


@app.command('configure')
def configure(mode: str = typer.Option('draft', '--mode'), setting: Optional[list[str]] = typer.Option(None, '--set')):
    values = {'mode': mode}
    for item in setting or []:
        key, sep, value = item.partition('=')
        if not sep:
            raise typer.BadParameter('use --set key=value')
        try:
            values[key] = json.loads(value)
        except ValueError:
            values[key] = value
    output(s.configure(db(True), **values))


@app.command('import-contacts')
def import_contacts(path: Path, reviewed: bool = typer.Option(False, '--reviewed', help='Confirm these professional contacts and relationship notes were reviewed.')):
    if not reviewed:
        raise typer.BadParameter('review the contact file, then use --reviewed')
    rows = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(rows, list) or len(rows) > 1000:
        raise typer.BadParameter('expected a bounded JSON list of contacts')
    conn = db(True)
    with s.transaction(conn):
        ids = [s.add_contact(conn, row)['id'] for row in rows]
    output({'imported_ids': ids, 'sent': 0})


@app.command('contacts')
def contacts():
    conn = db()
    try:
        output([dict(r) for r in conn.execute('SELECT id,name,company,role_function,relationship,source,verified_at,email_allowed,blocked_at FROM referral_contacts ORDER BY company,name')])
    finally:
        conn.close()


@app.command('suppress')
def suppress(contact_id: str, reason: str = typer.Option(..., '--reason')):
    s.block_contact(db(True), contact_id, reason)
    output({'suppressed': contact_id})


@app.command('plan')
def plan(limit: Optional[int] = typer.Option(None, '--limit', min=0, max=100)):
    from applypilot.orchestration.utility_client import run_utility
    output(run_utility('referral_plan', {'limit': limit}))


@app.command('open')
def open_case(job_url: str, job_function: Optional[str] = typer.Option(None, '--function'),
              function_evidence: Optional[str] = typer.Option(None, '--function-evidence'),
              team: Optional[str] = None, team_evidence: Optional[str] = None,
              job_value: Optional[float] = typer.Option(None, min=0, max=1), value_evidence: Optional[str] = None,
              requisition_id: Optional[str] = None, requisition_evidence: Optional[str] = None,
              deadline: Optional[str] = None):
    from .planner import open_case as create
    output(create(db(True), job_url, job_function=job_function, function_evidence=function_evidence,
                  job_team=team, team_evidence=team_evidence, job_value=job_value, value_evidence=value_evidence,
                  requisition_id=requisition_id, requisition_evidence=requisition_evidence, deadline=deadline))


@app.command('draft')
def draft(case_id: str, contact_id: str, channel: Optional[str] = typer.Option(None, '--channel'),
          kind: str = typer.Option('request', '--kind'), reply_to: Optional[str] = typer.Option(None, '--reply-to'),
          text_file: Optional[Path] = typer.Option(None, '--text-file'),
          attach_resume: bool = typer.Option(True, '--attach-resume/--no-attachment')):
    from .outreach import compose
    from applypilot import config
    row = compose(db(True), case_id, contact_id, config.load_profile(), channel=channel, kind=kind,
                  reply_to=reply_to, text=text_file.read_text(encoding='utf-8') if text_file else None,
                  include_resume=attach_resume)
    output(dict(row, payload_json=s.obj(row['payload_json'])))


@app.command('outbox')
def outbox():
    conn = db()
    try:
        output([dict(r, payload_json=s.obj(r['payload_json'])) for r in conn.execute('SELECT * FROM referral_outbox ORDER BY created_at')])
    finally:
        conn.close()


@app.command('revise')
def revise(outbox_id: str, text_file: Path = typer.Option(..., '--text-file')):
    from .outreach import revise as update
    output(update(db(True), outbox_id, text_file.read_text(encoding='utf-8')))


@app.command('approve')
def approve(outbox_id: str, payload_hash: str = typer.Option(..., '--hash'), actor: str = typer.Option('operator', '--actor')):
    from .outreach import approve as approve_one
    from applypilot import config
    output(approve_one(db(True), outbox_id, payload_hash, actor=actor, profile=config.load_profile()))


@app.command('manual-sent')
def manual_sent(outbox_id: str, payload_hash: str = typer.Option(..., '--hash'), reference: str = typer.Option(..., '--reference')):
    from .outreach import record_manual_send
    record_manual_send(db(True), outbox_id, payload_hash, reference=reference)
    output({'recorded': outbox_id})


@app.command('wait')
def wait(case_id: str, contact_id: str, hours: int = typer.Option(12, '--hours', min=1, max=24),
         reason: str = typer.Option(..., '--reason'), deadline: Optional[str] = typer.Option(None, '--deadline')):
    output({'hold_until': s.wait_for_referral(db(True), case_id, contact_id, hours, reason=reason, deadline=deadline)})


@app.command('release')
def release(case_id: str, reason: str = typer.Option(..., '--reason')):
    s.release(db(True), case_id, reason)
    output({'released': case_id})


@app.command('review')
def review():
    conn = db()
    try:
        output([dict(r, links_json=s.obj(r['links_json'], [])) for r in conn.execute("SELECT * FROM referral_messages WHERE state='review' ORDER BY received_at")])
    finally:
        conn.close()


@app.command('confirm')
def confirm(message_key: str, kind: str = typer.Option(..., '--kind'), quote: str = typer.Option(..., '--quote'),
            case_id: Optional[str] = typer.Option(None, '--case'), contact_id: Optional[str] = typer.Option(None, '--contact')):
    from .inbox import review_message
    output(review_message(db(True), message_key, kind=kind, quote=quote, case_id=case_id, contact_id=contact_id))


@app.command('record')
def record(case_id: str, kind: str, evidence: str = typer.Option(..., '--evidence'),
           contact_id: Optional[str] = typer.Option(None, '--contact'), at: Optional[str] = typer.Option(None, '--at')):
    output(s.event(db(True), case_id, kind, contact_id=contact_id, quote=evidence, at=at))


@app.command('retract')
def retract(event_id: str, reason: str = typer.Option(..., '--reason')):
    s.retract(db(True), event_id, reason)
    output({'retracted': event_id})


@app.command('use-route')
def use_route(case_id: str, mechanism: str = typer.Option(..., '--mechanism'), evidence: str = typer.Option(..., '--evidence'),
              url: Optional[str] = typer.Option(None, '--url'), contact_id: Optional[str] = typer.Option(None, '--contact'),
              name_permission: bool = typer.Option(False, '--name-permission'), same_posting: bool = typer.Option(False, '--verified-same-posting')):
    s.approve_route(db(True), case_id, target=url, mechanism=mechanism, evidence=evidence,
                    contact_id=contact_id, name_permission=name_permission, confirmed_same_posting=same_posting)
    output({'updated': case_id})


@app.command('application-outcome')
def application_outcome(message_key: str, submission_id: str, kind: str, quote: str = typer.Option(..., '--quote')):
    from .inbox import link_application_outcome
    output(link_application_outcome(db(True), message_key, submission_id, kind, quote))


@app.command('doctor')
def doctor():
    """Inspect the referral funnel, saved permissions and Gmail prerequisites; no network or writes."""
    from .health import diagnose
    conn = db()
    try:
        output(diagnose(conn))
    finally:
        conn.close()


@app.command('retry-research')
def retry_research(limit: int = typer.Option(20, '--limit', min=1, max=100)):
    """Requeue eligible empty/failed research only. Never retry messages or applications."""
    from applypilot.orchestration.client import run_work
    output(asyncio.run(run_work('network_research', options={'limit': limit})))


@app.command('report')
def report():
    from .report import report as make_report
    conn = db()
    try:
        output(make_report(conn))
    finally:
        conn.close()


@app.command('authorize-unattended')
def authorize_unattended(email: str = typer.Option(...,'--email'),
                         daily_limit: int = typer.Option(-1,'--daily-limit',min=-1),
                         actor: str = typer.Option('owner','--actor'),
                         include_known_contacts: bool = typer.Option(False,'--include-known-contacts'),
                         followups: bool = typer.Option(True,'--followups/--no-followups'),
                         replies: bool = typer.Option(True,'--replies/--no-replies'),
                         routes: bool = typer.Option(True,'--same-posting-routes/--no-route-changes')):
    """Grant routine Gmail permission once. This can enable later unattended sends."""
    from .autonomy import authorize, contact_allowed
    from applypilot import config
    profile = config.load_profile()
    if s.email_address(email) != s.email_address(profile.get('personal',{}).get('email')):
        raise typer.BadParameter('authorize the actual profile sender account')
    conn=db(True)
    try:
        with s.transaction(conn):
            result=authorize(conn,email,actor=actor,daily_limit=daily_limit,followups=followups,replies=replies,routes=routes)
            if include_known_contacts:
                # This is a one-time explicit owner's selection, not inferred consent.
                conn.execute("""UPDATE referral_contacts SET email_basis='existing_relationship'
                    WHERE relationship IN ('knows_work','professional','acquaintance')
                    AND length(trim(relationship_note))>0 AND email_allowed=1
                    AND blocked_at IS NULL AND email_basis='manual_only'""")
            result['eligible_contacts']=sum(contact_allowed(dict(r)) for r in conn.execute('SELECT * FROM referral_contacts'))
        output(result)
    finally:
        conn.close()


@app.command('digest')
def morning_digest(hours: int = typer.Option(24,'--hours',min=1,max=720)):
    """Local recent-activity summary: sent work, uncertain delivery, and exceptions."""
    from .autonomy import digest
    conn=db()
    try:
        output(digest(conn,hours=hours))
    finally:
        conn.close()


@app.command('capabilities')
def capabilities():
    output({'gmail': 'routine sending/replies after account-scoped standing authorization',
            'linkedin': 'configured account actions through Temporal account pacing and provider adapters',
            'x': 'configured XActions adapter with operation identities and provider receipts',
            'browseros': 'owned application pages with evidence and configured challenge handling',
            'exceptions': 'Temporal retries and submission/message evidence reconciliation'})


@app.command('watch')
def watch():
    """Resume durable networking Schedules."""
    from applypilot.orchestration.client import change_schedules
    output(asyncio.run(change_schedules('resume', names=['network_prepare','network_research','network_delivery'])))


@app.command('run')
def run(dry_run: bool = typer.Option(False, '--dry-run')):
    """Request finite networking entity discovery and delivery."""
    if dry_run:
        from .health import diagnose
        conn = db()
        try:
            output(diagnose(conn))
        finally:
            conn.close()
        return
    from applypilot.orchestration.client import run_work
    async def submit():
        return [await run_work('network_prepare'), await run_work('network_delivery')]
    output(asyncio.run(submit()))


@app.command('reconcile')
def reconcile(outbox_id: str):
    from applypilot.orchestration.utility_client import run_utility
    output(run_utility('referral_reconcile', {'outbox_id': outbox_id}))


@app.command('github-observation')
def github_observation(username: str):
    """One public profile observation, not automatic contact approval or an employee directory."""
    from applypilot.orchestration.utility_client import run_utility
    output(run_utility('github_person', {'username': username}))


@app.command('restore-route')
def restore_route(case_id: str, reason: str = typer.Option(..., '--reason')):
    s.restore_route(db(True), case_id, reason)
    output({'restored': case_id})


@app.command('ai-draft')
def ai_draft(outbox_id: str):
    """Rewrite an existing unsent draft with your model; requires fresh human approval."""
    from applypilot.orchestration.utility_client import run_utility
    output(run_utility('referral_revise', {'outbox_id': outbox_id}))


@app.command('research')
def research(job_url: str, output_file: Path):
    """Research an existing job's employer; contacts are not imported or contacted."""
    from applypilot import config
    from applypilot.integrations.research import research_job
    from applypilot.integrations.io import write_json
    from applypilot.integrations.types import SourceError
    config.load_env()
    try:
        report = research_job(job_url)
        write_json(output_file, report)
        output({'report': str(output_file), 'status': report['status'], 'sent': 0})
        if report['status'] != 'complete':
            raise typer.Exit(2)
    except SourceError as exc:
        raise typer.BadParameter(str(exc)) from None


@app.command('prepare')
def prepare(limit: Optional[int] = typer.Option(None, min=0, max=100)):
    """Local planning + drafts + opted-in TTLs; no mailbox, source reads or sends."""
    from applypilot.orchestration.utility_client import run_utility
    output(run_utility('referral_prepare', {'limit': limit}))


@app.command('import-network')
def import_network(path: Path, provider: str = 'linkedin_export'):
    """Stage a Connections.csv/structured JSON as unverified leads, not contacts."""
    from .network import read_network_file, stage_people
    output(stage_people(db(True), read_network_file(path), provider=provider,
                        operation='connections' if provider == 'linkedin_export' else 'people'))


@app.command('leads')
def leads(company: Optional[str] = None, state: str = 'review', limit: int = typer.Option(100, min=1, max=1000)):
    from .network import list_leads
    conn = db()
    try:
        output(list_leads(conn, company=company, state=state, limit=limit))
    finally:
        conn.close()


@app.command('promote-lead')
def promote_lead(lead_id: str, company: str = typer.Option(...), function: str = typer.Option(...),
                 evidence: str = typer.Option(...), reviewed: bool = typer.Option(False, '--reviewed'),
                 relationship: str = 'cold', relationship_note: str = '', team: str = '',
                 kind: str = 'employee', name: Optional[str] = None):
    """Review identity/current employer/function; source degree never implies warmth."""
    from .network import promote_lead as promote
    output(promote(db(True), lead_id, reviewed=reviewed, company=company, role_function=function,
                   evidence=evidence, relationship=relationship, relationship_note=relationship_note,
                   team=team, kind=kind, name=name))


@app.command('reject-lead')
def reject_lead(lead_id: str, reason: str = typer.Option(...)):
    from .network import reject_lead as reject
    reject(db(True), lead_id, reason=reason)
    output({'rejected': lead_id})


@app.command('discover')
def discover(case_id: str, provider: str = 'web', limit: int = typer.Option(10, min=1, max=20),
             authorized: bool = typer.Option(False, '--authorized'), start: int = typer.Option(0, min=0, max=5000),
             account: Optional[str] = None, network: Optional[str] = None, current_company: Optional[str] = None):
    """Bounded free-source lookup -> review leads. Does not message or grant consent."""
    from applypilot.orchestration.utility_client import run_utility
    inputs = {'start': start, 'account': account, 'current_company': current_company}
    if network is not None:
        inputs['network'] = [x.strip() for x in network.split(',')]
    output(run_utility('referral_discovery', {'case_id': case_id, 'provider': provider,
        'limit': limit, 'authorized': authorized, 'inputs': inputs}))


@app.command('packet')
def packet(case_id: str):
    """Exact role/req/link, approved fit evidence and available tailored resume."""
    from .outreach import referral_packet
    from applypilot.config import load_profile
    conn = db()
    try:
        output(referral_packet(conn, case_id, load_profile()))
    finally:
        conn.close()


@app.command('refresh-draft')
def refresh_draft(outbox_id: str):
    """Refresh an unsent generated request after tailoring/context changes; approval resets."""
    from .outreach import _outbox, compose
    from applypilot.config import load_profile
    conn = db(True)
    row = _outbox(conn, outbox_id)
    if row['kind'] != 'request' or row['attempted_at'] or row['state'] not in {'draft', 'approved'}:
        raise typer.BadParameter('only an unattempted generated request can be refreshed')
    output(compose(conn, row['case_id'], row['contact_id'], load_profile(),
                   channel=row['channel'], _refresh=True))
