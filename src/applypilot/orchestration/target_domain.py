"""Finite owner-target observations and atomic business publication."""
from dataclasses import replace
from . import store as inputs
from .contracts import digest
from .company_domain import _put, _vacancy_page


def _pages(conn, target, report):
    from applypilot.discovery.company_targets import _facts
    observed = {url for source in report.get('sources', []) for url in source.get('links', [])}
    urls = ([target['job_url'] or target['careers_url']] if target['job_url'] or target['careers_url'] else
            [url for url in _facts(report, 'careers_url') if url in observed] or [target['website']])
    entries = ([{'board': board} for board in target['jobseek_boards']] if target['jobseek_boards'] else
               [{'url': url} for url in dict.fromkeys(urls)])
    return [_vacancy_page(conn, entry) for entry in entries]


def _read(crawler, url):
    from applypilot.integrations.types import SourceError
    try:
        return crawler.read(url, fresh=True)
    except SourceError as exc:
        if getattr(exc, 'challenge_url', None) and crawler.backend != 'flaresolverr':
            return None
        raise


def execute(request, check):
    from applypilot.database import get_connection
    from applypilot.targets import store as targets
    from applypilot.discovery import repository
    from applypilot.discovery.models import DiscoveryPage, SourceTarget
    from applypilot.discovery.company_targets import _facts, published, vacancy_step
    from applypilot.company_discovery.source_policy import target_research_allowed
    from applypilot.integrations.crawl import CrawlService
    conn = get_connection()
    value = inputs.get(conn, request.reference)
    root = inputs.get(conn, value['snapshot'])
    raw_source = value.get('source_target', value['target'])
    source = SourceTarget(**{**raw_source,
        'request_params': tuple(tuple(pair) for pair in raw_source.get('request_params', ())),
        'origins': tuple(raw_source.get('origins', ())),
        'native_targets': tuple(raw_source.get('native_targets', ()))})
    current = targets.get(conn, source.scope)
    if current['state'] != 'active' or current['revision'] != dict(source.request_params)['revision']:
        return {'state': 'superseded'}
    if not target_research_allowed(conn, current):
        return {'state': 'paused'}
    check()
    context = inputs.context_from(root)
    context = replace(context, reserve_budget=lambda *args, **kwargs: check() or True)
    phase = request.phase
    if phase == 'target_prepare':
        target = {**current, 'jobseek_boards': targets.boards(conn, current['id'])}
        known = bool(target['company'] and target['website'] and
                     (target['careers_url'] or target['job_url'] or target['jobseek_boards']))
        needs_research = not known or target['action'] == 'outreach'
        query = (target['website'] or target['company'] or target['job_url']) + ' official company careers'
        query_reference = inputs.put(conn, {'query': query})
        homepage = [inputs.put(conn, {'url': target['website']})] if target['website'] else []
        pages = [] if needs_research else _pages(conn, target, {})
        reference = _put(conn, {'snapshot': value['snapshot'], 'source_target': raw_source,
                               'target': target, 'report': {}})
        return {'reference': reference, 'query_reference': query_reference,
                'read_references': homepage, 'needs_research': needs_research, 'pages': pages}
    target = value['target']
    if phase == 'target_search':
        from applypilot.integrations.research import _source
        from applypilot.integrations.search import SearchService
        query = inputs.get(conn, request.options['query_reference'])['query']
        hits = SearchService(request.options['provider'], before_request=check).search(query, limit=4)
        sources = [inputs.put(conn, _source(hit.url, hit.title, hit.snippet, 'search_snippet',
            fetched_at=hit.fetched_at, provider=hit.provider)) for hit in hits]
        pages = [inputs.put(conn, {'url': hit.url}) for hit in hits]
        conn.commit()
        return {'sources': sources, 'pages': list(dict.fromkeys(pages))}
    if phase == 'target_read':
        from applypilot.integrations.research import _source
        url = inputs.get(conn, request.options['page_reference'])['url']
        document = _read(CrawlService(request.options['backend'], before_request=check), url)
        if document is None:
            return {'challenge': True}
        source_reference = inputs.put(conn, _source(document.url, document.title, document.markdown,
            'page', document.links, document.fetched_at, provider=document.provider, truncated=document.truncated))
        final_reference = _put(conn, {'url': document.url})
        return {'source_reference': source_reference, 'final_reference': final_reference}
    if phase == 'target_synthesize':
        from applypilot.integrations.research import Researcher
        sources = [inputs.get(conn, reference) for reference in request.options['sources']]
        report = Researcher().enrich(target['website'] or target['company'] or target['job_url'], sources,
            fields=('company_name', 'website', 'description', 'industry', 'careers_url'))
        if not report.get('sources') or report.get('errors'):
            raise ValueError('Company synthesis did not produce supported research')
        for field, destination in (('company_name', 'company'), ('website', 'website')):
            if not target[destination]:
                facts = _facts(report, field)
                if len(facts) != 1:
                    raise ValueError('Company identity is not supported by unique evidence')
                target[destination] = targets._url(facts[0]) if destination == 'website' else facts[0]
        with repository.transaction(conn, resource='company-targets'):
            current = targets.get(conn, target['id'])
            if current['state'] != 'active' or current['revision'] != target['revision']:
                return {'state': 'superseded'}
            target.update(company=current['company'] or target['company'], website=current['website'] or target['website'])
            conn.execute('UPDATE company_targets SET company=?,website=?,config_json=? WHERE id=?',
                (target['company'], target['website'], targets.dump({key: target[key] for key in targets.FIELDS}), target['id']))
        pages = [] if target['action'] == 'outreach' else _pages(conn, target, report)
        reference = _put(conn, {**value, 'target': target, 'report': report})
        return {'reference': reference, 'pages': pages, 'outreach_only': target['action'] == 'outreach'}
    if phase == 'target_vacancy':
        from applypilot.integrations.types import SourceError
        entry = inputs.get(conn, request.options['page_reference'])
        entry['depth'] = request.options['depth']
        crawler = CrawlService(request.options['backend'], before_request=check)
        try:
            rows, observations, traversal = vacancy_step({'pending': [entry], 'visited': []}, target, context, crawler)
        except SourceError as exc:
            if getattr(exc, 'challenge_url', None) and crawler.backend != 'flaresolverr':
                return {'challenge': True}
            raise
        for row in rows:
            row['url'] = targets._url(row['url'])
            row['application_url'] = targets._url(row.get('application_url') or row['url'])
        if target['job_url']:
            requested = target['job_url'].rstrip('/')
            rows = [row for row in rows if requested in {row['url'].rstrip('/'), row['application_url'].rstrip('/')}]
            if not rows and not request.options['found'] and request.options['last_page'] and not traversal['pending']:
                raise ValueError('The supplied job URL did not expose a verifiable vacancy')
        pages = [_vacancy_page(conn, following) for following in traversal['pending']]
        reference = _put(conn, {'rows': rows, 'observations': observations})
        clean = all(item.get('complete', True) and not item.get('partial') and not item.get('capped') for item in observations)
        return {'reference': reference, 'pages': pages, 'found': len(rows), 'coverage_complete': clean}
    if phase in {'target_publish', 'target_complete'}:
        complete = phase == 'target_complete'
        references = request.options['observations'] if complete else [request.options['reference']]
        receipt_key = digest(('owner-target', request.options['observation'], source.request_key(), phase, references))
        with repository.transaction(conn, resource='temporal-target:' + receipt_key):
            prior = inputs.receipt(conn, receipt_key)
            if prior is not None:
                return prior
            evidence = [inputs.get(conn, reference) for reference in references]
            observations = [observation for item in evidence for observation in item['observations']]
            report = {**value['report'], 'owner_target_id': target['id'], 'target_revision': target['revision'],
                      'careers_observations': observations, 'careers_coverage': 'observed_pages' if complete else 'in_progress'}
            rows = [] if complete else evidence[0]['rows']
            counts = repository.publish_records(rows, source.provider, context=context, tenant=source.scope,
                origins=source.origins, api_region=dict(source.request_params).get('api_region'), conn=conn)
            published(conn, DiscoveryPage(digest((request.options['observation'], source.request_key())),
                records=tuple(rows), exhausted=complete,
                request_metadata={'target_observation_id': receipt_key, 'target_evidence': report, 'authoritative': False}))
            if complete and request.options['coverage_clean']:
                from applypilot.discovery.freshness import record_complete
                record_complete(conn, root['context_key'], source.request_key(), receipt_key)
            result = ({'state': 'exhausted' if request.options['coverage_clean'] else 'coverage_partial'}
                      if complete else {'new': counts.new, 'found': counts.fetched})
            inputs.save_receipt(conn, receipt_key, result)
        return result
    raise ValueError('Unknown owner-target observation')
