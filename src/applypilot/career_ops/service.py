"""Executable career-ops workflows through the existing DSPy/RLM/LiteLLM route."""
from __future__ import annotations

import json
import re
from datetime import date

from jsonschema import Draft202012Validator

from applypilot import operations as ops
from applypilot.execution import ExecutionStopped
from applypilot.reasoning import workflow_json

from . import context, schemas, store

_TEXT_LIMITS = {'job_url': 4000, 'instructions': 16000, 'offer_text': 100000,
                'notes': 40000, 'answer': 20000, 'session_id': 80, 'interview_date': 10,
                'retry_report_id': 80, 'job_description': 100000, 'company_name': 300}
_MONEY_FIELDS = {f'{name}_{field}' for name in ('current', 'desired', 'advertised', 'offer')
                 for field in ('amount', 'currency', 'period')}
_NO_SPONSOR = re.compile(
    r'\b(?:no\s+(?:(?:visa|immigration|work(?:\s+visa)?)\s+)?sponsorship|'
    r'(?:cannot|can\s*not|do\s+not|don.t|will\s+not|won.t|unable\s+to)\s+'
    r'(?:provide\s+|offer\s+)?(?:(?:visa|immigration|employment|work\s+visa)\s+)?sponsor(?:ship)?|'
    r'sponsorship\s+(?:is\s+)?(?:not\s+(?:available|provided|offered)|unavailable)|'
    r'(?:must|only|required\s+to)\b[^.\n]{0,100}\bwithout\s+'
    r'(?:(?:visa|immigration)\s+)?sponsorship)\b', re.IGNORECASE)


class RequestError(ValueError):
    """User input that must change before this request can be retried."""


def _inputs(mode, data):
    if mode not in store.MODES:
        raise RequestError('Unknown career workflow')
    if not isinstance(data, dict) or set(data) - {*_TEXT_LIMITS, *_MONEY_FIELDS, 'minutes', 'session_revision', 'effort_minutes'}:
        raise RequestError('Unknown career workflow input')
    result = {}
    for key, value in data.items():
        if key in ('minutes', 'session_revision', 'effort_minutes'):
            if isinstance(value, str) and value.isdigit():
                value = int(value)
            lower, upper = {'minutes': (15, 720), 'session_revision': (0, None), 'effort_minutes': (0, None)}[key]
            if type(value) is not int or value < lower or (upper is not None and value > upper):
                raise RequestError({'minutes': 'Interview preparation must be between 15 and 720 minutes',
                    'effort_minutes': 'Practice effort must be a nonnegative number of minutes',
                    'session_revision': 'Use the revision shown for the current practice question'}[key])
        elif not isinstance(value, str) or len(value) > _TEXT_LIMITS.get(key, 50):
            raise RequestError(f'{key} exceeds its supported text limit')
        else:
            value = value.strip()
        result[key] = value
    if result.get('interview_date'):
        try:
            date.fromisoformat(result['interview_date'])
        except ValueError as exc:
            raise RequestError('Use YYYY-MM-DD for the interview date') from exc
    if mode == 'offer_review' and not result.get('offer_text') and not result.get('retry_report_id'):
        raise RequestError('Paste the offer or contract text for a clause reading')
    if mode == 'interview_debrief' and not (result.get('notes') or result.get('instructions')
                                          or result.get('retry_report_id')):
        raise RequestError('Add your actual interview notes or questions for the debrief')
    if mode == 'interview_practice' and result.get('session_id') and not result.get('answer'):
        raise RequestError('Answer the current practice question to continue this session')
    if mode == 'interview_practice' and result.get('session_id') and 'session_revision' not in result:
        raise RequestError('Include the session revision shown with the question you answered')
    if mode == 'interview_practice' and result.get('answer') and not result.get('session_id'):
        raise RequestError('Select the practice session whose question you are answering')
    try:
        context.salary_figures(result)
    except ValueError as exc:
        raise RequestError(str(exc)) from exc
    return result


def _check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise ExecutionStopped('Career workflow stopped')


def _citation(value, sources):
    source = sources.get(value['source_id'])
    quote = value['quote']
    if source is None or not quote.strip() or quote not in source['text']:
        raise ValueError('A career claim lacks an exact quote from its identified source')
    return source


def _numbers(value):
    return {match.replace(',', '') for match in re.findall(r'\b\d[\d,]*(?:\.\d+)?%?', value)}


def _validate_evidence(value, sources):
    if isinstance(value, list):
        for child in value:
            _validate_evidence(child, sources)
    elif isinstance(value, dict):
        if 'source_id' in value and 'quote' in value:
            _citation(value, sources)
        evidence = value.get('evidence')
        if evidence:
            allowed_numbers = set().union(*(_numbers(item['quote']) for item in evidence))
            for key in ('interpretation', 'draft'):
                if key in value and _numbers(value[key]) - allowed_numbers:
                    raise ValueError('A draft introduces a number missing from its cited evidence')
        for child in value.values():
            _validate_evidence(child, sources)


def _candidate_citations(evidence, sources):
    if not evidence or any(_citation(item, sources)['kind'] != 'candidate' for item in evidence):
        raise ValueError('Candidate claims require original candidate evidence, not JD or generated text')


def _stories(result, sources):
    for story in result.get('stories', []):
        for field in ('situation', 'task', 'action', 'result'):
            _candidate_citations(story[field]['evidence'], sources)
        story['status'] = 'draft'
        story['reflection_status'] = 'awaiting_candidate'
        story['canonical_evidence_updated'] = False


def _evaluation(result, payload):
    sources = payload['sources']
    jd = sources['job:description']['text']
    for row in result['B_match']['requirements']:
        if row['jd_quote'] not in jd or not row['jd_quote'].strip():
            raise ValueError('Every requirement must quote this job description exactly')
        if row['provenance'] == 'estimate' and row['importance'] in ('critical', 'high'):
            raise ValueError('Estimated requirement importance cannot occupy either top band')
        if row['match'] in ('demonstrated', 'partial') or row['candidate_evidence']:
            _candidate_citations(row['candidate_evidence'], sources)
    for citation in result['work_authorization']['no_sponsorship_quotes']:
        if citation['source_id'] != 'job:description' or not _NO_SPONSOR.search(citation['quote']):
            raise ValueError('No-sponsorship claims require an explicit prohibition in the job description')
    quotes = result['work_authorization']['no_sponsorship_quotes']
    scoped = payload['work_authorization']
    # A missed explicit phrase cannot silently turn a sponsorship-dependent role
    # into an unqualified recommendation. Exact context remains visible to review.
    if not quotes:
        for line in jd.splitlines():
            if _NO_SPONSOR.search(line):
                quotes.append({'source_id': 'job:description', 'quote': line})
    status = ('hard_blocker' if scoped.get('needs_sponsorship') is True else
              'no_sponsorship_stated' if scoped.get('needs_sponsorship') is False else
              'needs_clarification') if quotes else 'not_established'
    result['work_authorization'].update({'status': status, 'candidate_scope': scoped})
    if status == 'hard_blocker':
        result['recommendation'] = 'skip'
    elif status == 'needs_clarification' and result['recommendation'] == 'pursue':
        result['recommendation'] = 'review'
    if result['global_score'] < 4.5:
        result['H_application_answers'] = []
    result['job_description_archive'] = jd


def _practice(result, payload, session):
    if session is None:
        if result['feedback']:
            raise ValueError('A first practice question cannot grade an answer that has not been given')
        return
    answer = payload['request']['answer']
    if not result['feedback']:
        raise ValueError('A practice answer needs grounded feedback before the next question')
    for item in result['feedback']:
        if item['answer_quote'] not in answer or not item['answer_quote'].strip():
            raise ValueError('Practice feedback must quote the submitted answer exactly')
        if not any(c['source_id'] == 'user:answer' for c in item['evidence']):
            raise ValueError('Practice feedback must cite the current answer')
    previous = [turn['question'].strip().casefold() for turn in session['turns']]
    if result['next_question'].strip().casefold() in [*previous, session['next_question'].strip().casefold()]:
        raise ValueError('The next practice question repeats a question already asked')
    result['answered_question'] = session['next_question']
    result['answer'] = answer


def _offer(result, payload):
    text = payload['request']['offer_text']
    position = -1
    for clause in result['clauses']:
        found = text.find(clause['quote'], position + 1)
        if found < 0:
            raise ValueError('Offer clauses must be exact excerpts in document order')
        position = found
        if not clause['lawyer_questions'] and any(topic in clause['topics'] for topic in
                ('termination', 'restrictions', 'IP', 'repayment', 'disputes', 'classification')):
            raise ValueError('Jurisdiction-dependent clauses require a lawyer question')
        if re.search(r'\b(?:illegal|unenforceable|enforceable|statutory minimum|violates? the law)\b',
                     clause['plain_meaning'], re.IGNORECASE):
            raise ValueError('Offer readings must ask legal questions instead of asserting legal conclusions')
        for question in [*clause['lawyer_questions'], *clause['employer_questions']]:
            if not question.endswith('?'):
                raise ValueError('Offer action items must be questions')
    for delta in result['differences']:
        if delta['offer_quote'] not in text:
            raise ValueError('An offer difference must quote the offer exactly')
    result['scope'] = 'AI-generated document reading and questions; not a legal opinion or signing recommendation.'
    result['offer_archive'] = text


def _patterns(result, payload):
    for item in result['learning_plan']:
        grounded = False
        for citation in item['evidence']:
            source = payload['sources'][citation['source_id']]
            if source['kind'] != 'explicit_rejection':
                continue
            note = json.loads(source['text']).get('notes') or ''
            terms = {term.casefold() for term in re.findall(r'[\w+#.-]{3,}', item['skill'])}
            if citation['quote'] in note and len(citation['quote'].split()) >= 3:
                grounded = bool(terms & {term.casefold() for term in re.findall(r'[\w+#.-]{3,}', citation['quote'])})
                if grounded:
                    break
        if not grounded:
            raise ValueError('An upskilling recommendation needs an explicit rejection excerpt naming that skill')
    result['cohorts'] = payload['history']['cohorts']
    result['observation_coverage'] = payload['history']['observation_coverage']
    result['interpretation_limit'] = payload['history']['interpretation']


def _validate(mode, result, payload, practice):
    Draft202012Validator(schemas.SCHEMAS[mode]).validate(result)
    _validate_evidence(result, payload['sources'])
    if mode == 'evaluate':
        _evaluation(result, payload)
    elif mode == 'interview_plan':
        if sum(block['minutes'] for block in result['blocks']) > payload['request'].get('minutes', 120):
            raise ValueError('Interview preparation exceeds the requested time budget')
    elif mode == 'interview_practice':
        _practice(result, payload, practice)
    elif mode == 'interview_debrief':
        notes = payload['request'].get('notes', '') + '\n' + payload['request'].get('instructions', '')
        for item in result['questions']:
            if item['question_quote'] not in notes:
                raise ValueError('Debrief questions must quote the actual interview notes')
    elif mode == 'offer_review':
        _offer(result, payload)
    elif mode == 'patterns':
        _patterns(result, payload)
    _stories(result, payload['sources'])
    result['compensation'] = payload['compensation']
    result['sources'] = payload['sources']
    result['input_revisions'] = payload['input_revisions']
    result['generated_at'] = store.now()
    result['draft_only'] = True
    return result


def run(conn, mode, data, *, cancel_event=None, on_started=None, report_id=None):
    """Generate and persist one report; failed model/invariant rounds remain retryable."""
    if report_id:
        saved = store.get(conn, report_id)
        if saved['state'] == 'complete':
            return saved
    if mode == 'interview_brief':
        raise ValueError('Interview briefs are owned by the Temporal JobWorkflow brief operation')
    if conn.in_transaction:
        raise ValueError('Career workflows require a connection outside a transaction before inference')
    data = _inputs(mode, data)
    if len(store.dump(data).encode('utf-8')) > 300000:
        raise RequestError('The combined career workflow input exceeds 300 KB')
    if data.get('retry_report_id'):
        prior = store.get(conn, data['retry_report_id'])
        if 'job_url' in data and data['job_url'] != prior['job_url']:
            raise RequestError('Continue this report with its original opportunity')
        merged = {**prior['input'], **data}
        merged.pop('retry_report_id', None)
        data = {**_inputs(mode, merged), 'retry_report_id': prior['id']}
    practice = store.session(conn, data['session_id']) if data.get('session_id') else None
    if practice:
        if data.get('session_revision') != practice['revision']:
            raise RequestError('This answer belongs to an earlier practice question; reload the session')
        for key, value in practice['context'].items():
            if key not in _TEXT_LIMITS:
                continue
            if key in ('job_url', 'job_description', 'company_name') and data.get(key) and data[key] != value:
                raise RequestError('Continue practice using the original session job')
            if not data.get(key):
                data[key] = value
    report = store.start(conn, mode, data, session_revision=practice['revision'] if practice else None,
                         report_id=report_id)
    if report['state'] == 'complete':
        return report
    try:
        if on_started is not None:
            on_started(report['id'])
        _check_cancel(cancel_event)
        if practice is not None:
            if mode != 'interview_practice':
                raise RequestError('Session IDs are only used for interview practice')
            if practice['job_url'] != data.get('job_url', ''):
                raise RequestError('The practice session belongs to a different job')
            if report['session_revision'] != practice['revision']:
                raise RequestError('This answer belongs to an earlier practice question; reload the session')
        try:
            payload = context.build(conn, mode, data, invitation=report['source'])
        except (ValueError, LookupError) as exc:
            raise RequestError(str(exc)) from exc
        if practice:
            payload['session'] = practice
        _check_cancel(cancel_event)
        result = workflow_json('company_research', schemas.BASE + '\n' + schemas.PROMPTS[mode], payload,
                               schema=schemas.SCHEMAS[mode], max_tokens=12000, cancel_event=cancel_event)
        _check_cancel(cancel_event)
        result = _validate(mode, result, payload, practice)
        return store.finish(conn, report, result, practice=practice, answer=data.get('answer', ''))
    except ExecutionStopped as exc:
        store.fail(conn, report, exc, cancelled=True)
        raise
    except (SystemExit, KeyboardInterrupt):
        store.cancel(conn, report['id'])
        raise
    except Exception as exc:  # noqa: BLE001 -- The workflow boundary records exhausted provider and validation rounds.
        ops.capture(exc, component_name='career_ops', operation=mode)
        store.fail(conn, report, exc)
        raise
