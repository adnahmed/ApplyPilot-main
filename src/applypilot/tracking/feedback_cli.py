"""Operator controls for the existing tracker and local outcome learner."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(help='Durable application outcomes, review and optional learned ordering.', no_args_is_help=True)


def connection(write=False):
    from applypilot import config, database
    if write:
        config.load_env()
        config.ensure_dirs()
        return database.init_db()
    if not config.DB_PATH.exists():
        raise typer.BadParameter('application database does not exist; run your normal initialization first')
    from applypilot import postgres
    conn = postgres.connect(config.DB_PATH.resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = postgres.Row
    try:
        conn.execute('SELECT version FROM feedback_schema').fetchone()
    except postgres.OperationalError as exc:
        conn.close()
        raise typer.BadParameter('feedback schema not initialized; run feedback configure once') from exc
    return conn


def output(data):
    typer.echo(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False))


@app.command('configure')
def configure_cmd(mode: Optional[str] = typer.Option(None, '--mode'),
                  setting: Optional[list[str]] = typer.Option(None, '--set', help='Validated setting=value; may repeat.')):
    from .feedback_store import configure
    values = {}
    if mode is not None:
        values['mode'] = mode
    for item in setting or []:
        if '=' not in item:
            raise typer.BadParameter('--set requires setting=value')
        key, raw = item.split('=',1)
        try:
            values[key] = json.loads(raw)
        except ValueError:
            values[key] = raw
    conn=connection(True)
    try:
        output(configure(conn, **values))
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command('status')
def status_cmd():
    from .feedback_store import policy
    from .feedback_learning import report
    conn=connection()
    try:
        data=report(conn)
        mailbox=[dict(r) for r in conn.execute('SELECT mailbox_key,scan_after,last_complete_at,coverage_start,coverage_end,last_error FROM feedback_mailboxes')]
        output({'policy':policy(conn),'summary':data['summary'],'mailboxes':mailbox,
                'model_id':data['model']['model_id'] if data['model'] else None,
                'model_ready':bool(data['model'] and data['model']['summary']['ready'])})
    finally:
        conn.close()


@app.command('sync')
def sync_cmd(days: int = typer.Option(90,'--days',min=1),
             since: Optional[str] = typer.Option(None,'--since',help='Start a historical scan once; subsequent runs resume it.'),
             dry_run: bool = typer.Option(False,'--dry-run')):
    from applypilot.tracking import run_tracking
    if not dry_run:
        connection(True)
    data=run_tracking(days=days,since=since,dry_run=dry_run)
    if data.get('errors'):
        raise typer.Exit(1)


@app.command('train')
def train_cmd():
    from .feedback_learning import build_model
    output(build_model(connection(True)))


@app.command('report')
def report_cmd():
    from .feedback_learning import report
    conn=connection()
    try:
        output(report(conn))
    finally:
        conn.close()


@app.command('review')
def review_cmd(limit: int = typer.Option(50,'--limit',min=1,max=1000),
               show_content: bool = typer.Option(False,'--show-content')):
    from .feedback_store import obj
    conn=connection()
    try:
        rows=[]
        for row in conn.execute("SELECT * FROM feedback_messages WHERE state IN ('review','retry') ORDER BY created_at LIMIT ?",(limit,)):
            data=dict(row)
            result={key:data[key] for key in ('message_key','state','received_at','submission_id','link_reason','last_error','attempts')}
            result['candidates']=obj(data['candidates_json'],[])
            result['classification']=obj(data['result_json'])
            if show_content:
                result['message']=obj(data['payload_json'])
            rows.append(result)
        output(rows)
    finally:
        conn.close()


@app.command('resolve')
def resolve_cmd(message_key: str, submission_id: Optional[str] = typer.Option(None,'--submission-id'),
                event: Optional[str] = typer.Option(None,'--event'),
                ignore: bool = typer.Option(False,'--ignore'),
                reason: str = typer.Option(...,'--reason'),
                occurred_at: Optional[str] = typer.Option(None,'--occurred-at')):
    from .feedback_inbox import resolve_message
    from .feedback_learning import build_model
    conn=connection(True)
    try:
        resolve_message(conn,message_key,submission_id=submission_id,event_type=event,ignore=ignore,
                        reason=reason,occurred_at=occurred_at)
        model=build_model(conn)
        output({'resolved':message_key,'model_id':model['model_id']})
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command('record')
def record_cmd(event: str, job_url: str = typer.Option(...,'--job-url'),
               submission_id: Optional[str] = typer.Option(None,'--submission-id'),
               occurred_at: Optional[str] = typer.Option(None,'--occurred-at'),
               notes: str = typer.Option('','--notes'),
               supersedes: Optional[int] = typer.Option(None,'--supersedes')):
    from applypilot.database import record_application_outcome
    from .feedback_learning import build_model
    conn=connection(True)
    try:
        result=record_application_outcome(job_url,event,submission_id=submission_id,occurred_at=occurred_at,
                                          notes=notes,supersedes_id=supersedes,conn=conn)
        build_model(conn)
        output(result)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command('retract')
def retract_cmd(event_id: int, reason: str = typer.Option(...,'--reason')):
    from .feedback_store import retract_event
    from .feedback_learning import build_model
    conn=connection(True)
    try:
        retract_event(conn,event_id,reason=reason)
        build_model(conn)
        output({'retracted':event_id})
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command('observe')
def observe_cmd(submission_id: str, through: str = typer.Option(...,'--through'),
                notes: str = typer.Option(...,'--notes')):
    """Confirm outcomes checked through a date (including non-email channels)."""
    from .feedback_store import observe_through
    from .feedback_learning import build_model
    conn=connection(True)
    try:
        observe_through(conn,submission_id,through,notes=notes)
        build_model(conn)
        output({'submission_id':submission_id,'observed_through':through})
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command('submission')
def submission_cmd(job_url: str = typer.Option(...,'--job-url'),
                   occurred_at: str = typer.Option(...,'--occurred-at'),
                   reference: str = typer.Option(...,'--reference')):
    """Record an off-platform send you actually made; never sends an application."""
    from .feedback_store import manual_submission
    try:
        output(manual_submission(connection(True),job_url,occurred_at,reference=reference))
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command('explain')
def explain_cmd(job_url: str = typer.Option(...,'--job-url')):
    from .feedback_learning import decision
    conn=connection()
    try:
        row=conn.execute('SELECT * FROM jobs WHERE url=?',(job_url,)).fetchone()
        if not row:
            raise typer.BadParameter('job not found')
        output(decision(conn,dict(row)))
    finally:
        conn.close()


@app.command('export')
def export_cmd(path: Path, include_identifiers: bool = typer.Option(False,'--include-identifiers')):
    """Export reproducible JSONL training/audit rows. Raw mail is never exported."""
    from .feedback_learning import dataset
    from .feedback_store import digest, dump, iso, policy
    conn=connection()
    try:
        data,summary=dataset(conn)
        # Refuse overwrite to avoid accidental loss of previous experimental records.
        with path.open('x',encoding='utf-8') as stream:
            stream.write(dump({'record_type':'manifest','schema_version':1,'exported_at':iso(),
                               'policy':policy(conn),'summary':summary,'identifiers':include_identifiers})+'\n')
            for row in data:
                r=dict(row)
                features=dict(r.pop('features'))
                r['events']=[{k:e.get(k) for k in ('id','event_type','occurred_at','created_at','source','trust')} for e in r['events']]
                r['within_events']=[{k:e.get(k) for k in ('id','event_type','occurred_at','source')} for e in r['within_events']]
                features.pop('target',None);features.pop('manual_reference',None)
                if not include_identifiers:
                    r['job_url']=digest(r['job_url']);r['submission_id']=digest(r['submission_id'])
                    features.pop('job_url',None);features.pop('title',None)
                    features['company']=digest(features.get('company',''))
                    features.pop('artifact_hashes',None)
                    # Explanations can contain employer names; retain policy identity, not those values.
                    selection=features.get('selection') or {}
                    features['selection']={k:selection.get(k) for k in ('mode','model_id','base_score','adjustment','shadow_adjustment','propensity','policy_kind')}
                stream.write(dump({'record_type':'submission',**r,'features':features})+'\n')
        try:
            path.chmod(0o600)
        except OSError:
            pass
        output({'path':str(path),'submissions':len(data),'raw_email_included':False})
    except FileExistsError as exc:
        raise typer.BadParameter('export file exists; choose a new path') from exc
    finally:
        conn.close()
