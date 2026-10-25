"""One semantic employer/posting research operation per Activity."""
from dataclasses import replace
import json

from . import store as inputs
from .company_domain import _put, _context, _vacancy_page
from .contracts import digest


def execute(request, value, conn, check):
    from applypilot.integrations import research
    from applypilot.integrations.state import SourceState
    from applypilot.integrations.types import SourceError, stable_id
    from applypilot.discovery.repository import hydrate_job, transaction
    from applypilot.database import preparation_stock_sql
    phase = request.phase
    if phase == 'company_report_prepare':
        options = value['options']['company_report']
        target = options['target']
        tails = ['official company employer careers business model',
                 'employment contractor client project talent pool recruiting terms',
                 'recruitment complaints hiring freeze layoffs fees evidence',
                 'official engineering team hiring roles', 'funding investors announcement', ' '.join(options['fields'])]
        queries = [inputs.put(conn, {'query': '"' + target + '" ' + tail})
                   for tail in tails[:options['queries']]]
        urls = []
        if target.startswith(('https://', 'http://')):
            urls.append(target)
        elif research.re.fullmatch(r'[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', target):
            urls.append('https://' + target)
        payload = {'snapshot': request.reference, 'general': True, 'job': {'company': target, 'url': ''},
                   'sources': [], 'ai': options['ai'], 'fields': options['fields']}
        pages = [inputs.put(conn, {'url': url}) for url in urls]
        return {'reference': _put(conn, payload), 'queries': queries, 'pages': pages,
                'search_provider': options['search'], 'max_pages': options['max_pages'], 'crawler': options['crawler']}
    if phase == 'posting_research_prepare':
        root = inputs.get(conn, value['snapshot']) if 'snapshot' in value else value
        job_url = value['job_url']
        row = conn.execute('SELECT j.* FROM jobs j WHERE j.url=? AND ' + preparation_stock_sql('j'), (job_url,)).fetchone()
        if not row or not row['company']:
            return {'state': 'not_applicable'}
        job = hydrate_job(conn, row)
        if research.posting_suitability(job)['status'] != 'pending':
            return {'state': 'complete'}
        previous = SourceState().report('job:' + job_url) or {}
        feedback = ([error['error'] for error in previous.get('errors', [])
                     if error.get('phase') == 'posting_suitability' and error.get('error')]
                    if previous.get('posting_basis') == research.posting_research_key(job) else [])
        tails = ('official company employer careers business model',
                 'employment contractor client project talent pool recruiting terms',
                 'recruitment complaints hiring freeze layoffs fees evidence', 'official engineering team hiring roles')
        queries = [inputs.put(conn, {'query': '"' + job['company'] + '" ' + tail}) for tail in tails]
        pages = [inputs.put(conn, {'url': url}) for url in dict.fromkeys(
            url for url in (job.get('application_url'), job.get('url'))
            if url and url.startswith(('http://', 'https://')))]
        payload = {'snapshot': value.get('snapshot', request.reference), 'job': job, 'feedback': feedback,
                   'sources': [], 'ai': root.get('options', {}).get('company_ai', True)}
        return {'reference': _put(conn, payload), 'queries': queries, 'pages': pages,
                'search_provider': 'auto', 'max_pages': None, 'crawler': 'http'}
    if phase == 'posting_research_search':
        from applypilot.integrations.search import SearchService
        spec = inputs.get(conn, request.options['query_reference'])
        provider = request.options['provider']
        if provider not in {'searxng', 'ddgs'}:
            raise ValueError('Posting search requires one explicit provider')
        hits = SearchService(provider, before_request=check).search(spec['query'], limit=4)
        value['sources'].extend(research._source(hit.url, hit.title, hit.title + '\n' + hit.snippet,
            'search_snippet', fetched_at=hit.fetched_at, provider=hit.provider) for hit in hits)
        pages = [inputs.put(conn, {'url': hit.url}) for hit in hits]
        return {'reference': _put(conn, value), 'pages': pages}
    if phase == 'posting_research_read':
        url = inputs.get(conn, request.options['page_reference'])['url']
        backend = request.options['backend']
        if backend not in {'http', 'playwright', 'browseros', 'flaresolverr'}:
            raise ValueError('Posting read requires one explicit backend')
        from applypilot.integrations.crawl import CrawlService
        try:
            doc = CrawlService(backend, before_request=check, allow_browseros=backend == 'browseros').read(url, fresh=True)
        except SourceError as exc:
            if getattr(exc, 'challenge_url', None) and backend != 'flaresolverr':
                return {'reference': request.reference,
                        'challenge_reference': _put(conn, {'url': exc.challenge_url})}
            raise
        value['sources'].append(research._source(doc.url, doc.title, doc.markdown, 'page', doc.links,
            doc.fetched_at, provider=doc.provider, requested_url=url, truncated=doc.truncated))
        thin = len(doc.markdown.strip()) < 600 or bool(research.re.search(
            r'javascript (?:is )?(?:required|disabled)', doc.markdown, research.re.I))
        return {'reference': _put(conn, value), 'thin': thin}
    job = value['job']
    if phase == 'posting_research_synthesize':
        report = research.Researcher().enrich(job['company'], value['sources'], ai=value['ai'],
                                            fields=value.get('fields', research.FIELDS))
        if report.get('errors'):
            raise ValueError('Employer synthesis did not produce validated evidence')
        report.update(company_revision=report['id'], job={key: job.get(key) for key in ('url', 'title', 'company', 'site')})
        value['report'] = report
        signal = job.get('site') in research._SIGNAL_SITES and not value.get('general')
        pages = []
        if signal:
            from applypilot.discovery.signal_research import stored_signal, _official_routes
            record = stored_signal(conn, job)
            if record:
                websites, careers = _official_routes(report, record['company'])
                pages = [_vacancy_page(conn, {'url': url}) for url in careers or websites]
        return {'reference': _put(conn, value), 'signal': signal, 'pages': pages}
    if phase == 'posting_research_signal':
        if request.options.get('finalize'):
            result = value['report'].get('signal_research')
            if result and 'coverage' in result:
                result['coverage'] = 'observed_pages'
            return {'reference': _put(conn, value), 'signal': False}
        from applypilot.discovery.signal_research import resolve_signal
        context = replace(_context(conn, value), reserve_budget=lambda *args, **kwargs: check() or True)
        backend = request.options['backend']
        if backend not in {'http', 'playwright', 'browseros', 'flaresolverr'}:
            raise ValueError('Signal vacancy read requires one explicit backend')
        service = research.Researcher(crawler=backend, before_request=check)
        page_reference = request.options.get('page_reference')
        entry = inputs.get(conn, page_reference) if page_reference else None
        if entry is not None:
            entry['depth'] = request.options['depth']
        try:
            result = resolve_signal(job, value['report'], service, context=context,
                                    traversal={'pending': [entry] if entry is not None else [], 'visited': []})
        except SourceError as exc:
            if entry is not None and getattr(exc, 'challenge_url', None) and backend != 'flaresolverr':
                challenge = {**entry, 'url': exc.challenge_url}
                challenge.pop('depth', None)
                return {'reference': request.reference, 'challenge_reference': _put(conn, challenge)}
            raise
        more = result.pop('traversal', None) if result else None
        pages = [_vacancy_page(conn, page) for page in more['pending']] if more else []
        previous = value['report'].get('signal_research') or {}
        if result:
            result['resolved_postings'] = [*previous.get('resolved_postings', []), *result['resolved_postings']]
            result['observations'] = [*previous.get('observations', []), *result.get('observations', [])]
            if result['resolved_postings']:
                result['status'] = 'resolved'
                result.pop('reason_code', None)
            value['report']['signal_research'] = result
        return {'reference': _put(conn, value), 'pages': pages}
    if phase == 'posting_research_assess':
        report = value['report']
        signal = report.get('signal_research')
        if not value['ai'] or signal and signal['status'] != 'resolved':
            value['state'] = 'needs_input'
            return {'reference': _put(conn, value)}
        row = conn.execute('SELECT * FROM jobs WHERE url=?', (job['url'],)).fetchone()
        if row is None:
            value['state'] = 'not_applicable'
            return {'reference': _put(conn, value)}
        current = hydrate_job(conn, row)
        if research.posting_research_key(current) != research.posting_research_key(job) or current.get('company') != job.get('company'):
            value['state'] = 'superseded'
            return {'reference': _put(conn, value)}
        context_row = conn.execute('SELECT intent_json FROM discovery_contexts WHERE context_key=?',
                                   (current.get('discovery_context_key'),)).fetchone()
        if not current.get('full_description') or context_row is None:
            value['state'] = 'needs_input'
            return {'reference': _put(conn, value)}
        current['company_research_revision'] = report['company_revision']
        decision = research._assess_posting(current, report, json.loads(context_row[0]),
                                            history=research._posting_history(conn, job['url']), feedback=value['feedback'])
        if current.get('site') in research._SIGNAL_SITES:
            decision['signal_research_version'] = research._SIGNAL_RESEARCH_VERSION
            decision['signal_research_status'] = signal['status'] if signal else 'not_signal'
        decision['revision'] = stable_id({key: item for key, item in decision.items() if key != 'revision'})
        value.update(current=current, decision=decision, state='complete' if decision['status'] != 'pending' else 'needs_input')
        report['posting_suitability'] = decision
        return {'reference': _put(conn, value)}
    if phase == 'posting_research_publish':
        report = value['report']
        receipt = digest(('posting-research', request.reference))
        prior = inputs.receipt(conn, receipt)
        if prior:
            report.update(status='complete' if prior['state'] == 'complete' else 'partial',
                          posting_basis=research.posting_research_key(job))
            SourceState().save_report('job:' + job['url'], report)
            return prior
        with transaction(conn, resource=receipt):
            if value.get('decision'):
                current = value['current']
                written = conn.execute("""UPDATE jobs SET company_research_revision=?,posting_suitability_json=?
                    WHERE url=? AND discovery_content_hash IS NOT DISTINCT FROM ?
                    AND discovery_source_revision IS NOT DISTINCT FROM ? AND discovery_context_key IS NOT DISTINCT FROM ?
                    AND applied_at IS NULL AND (apply_status IS NULL OR apply_status='failed')
                    AND NOT EXISTS (SELECT 1 FROM application_attempts a WHERE a.job_url=jobs.url
                        AND a.status IN ('prepared','side_effect_intent','verification_required','verification_quarantined','submitted'))""",
                    (report['company_revision'], json.dumps(value['decision']), current['url'], current.get('discovery_content_hash'),
                     current.get('discovery_source_revision'), current.get('discovery_context_key')))
                if written.rowcount != 1:
                    return {'state': 'superseded'}
            result = {'state': value.get('state', 'complete'),
                      'detail': {'suitability': value.get('decision', {}).get('status', 'pending')}}
            inputs.save_receipt(conn, receipt, result)
        report.update(status='complete' if result['state'] == 'complete' else 'partial',
                      posting_basis=research.posting_research_key(job))
        SourceState().save_report('job:' + job['url'], report)
        return result
    raise ValueError('Unknown posting research operation')
