"""Finite predictive research operations retaining provenance and review semantics."""
from dataclasses import asdict
from urllib.parse import urlsplit
import json

from .company_domain import _put, _context, _vacancy_page
from .contracts import digest
from . import store as inputs


def _document(company, document, *, home=False):
    from applypilot.company_discovery import predictive_research as research
    from applypilot.integrations.types import stable_id
    origin, name = research._host(company['website']), company['company']
    own = research._under(research._host(document.url), origin)
    if home and (not own or not research._mentions(document.markdown, name)):
        raise ValueError('Official homepage does not establish company identity')
    if not own and (not any(research._under(research._host(document.url), host) for host in research.PRIMARY_HOSTS)
                    or not research._mentions(document.markdown, name) or not research._affiliation(document, name)):
        return None
    if not home and research._index(document.url):
        return None
    published, article = research._publication(document)
    grant = research._grant_date(document) if not published else None
    parts = [part for part in urlsplit(document.url).path.split('/') if part]
    authored = bool(published and not home and not research._index(document.url) and not document.truncated
        and ((own and (article or (research.CONTENT.search(urlsplit(document.url).path) and len(parts) >= 2)))
             or (not own and research._affiliation(document, name, authored=True))))
    return {'id': stable_id(['company-research-source', document.url]), 'url': document.url,
            'title': document.title, 'text': document.markdown, 'event_date': published or grant,
            'observed_at': document.fetched_at, 'authored': authored,
            'date_kind': 'publication' if published else 'grant_award' if grant else 'unknown',
            'primary': True, 'truncated': bool(document.truncated)}


def execute(request, value, conn, check):
    from applypilot.company_discovery import predictive as business, predictive_store as store
    from applypilot.company_discovery import predictive_research as research, predictive_analytics as analytics
    from applypilot.company_discovery.store import settings
    phase = request.phase
    context = _context(conn, value)
    if phase == 'prediction_select':
        receipt = digest(('prediction-draft-selection', request.reference))
        prior = inputs.receipt(conn, receipt)
        if prior:
            return prior
        eligible = store.eligible_companies(conn)
        candidates, values = [], {}
        for row in conn.execute("SELECT * FROM company_predictions WHERE status='audited' AND outcome NOT IN ('contacted','followed_up','replied','rejected','closed')"):
            report = json.loads(row['report_json'])
            if row['website'] in eligible and report.get('research_reference') and report.get('audit', {}).get('alignment', 0) >= .45:
                candidates.append({'website': row['website'], **report['analytics']})
                values[row['website']] = (dict(row), report)
        outcomes = [{'website': row['website'], 'outcome': row['outcome'], 'observed_at': row['observed_at'],
                     'report': json.loads(row['report_json'])} for row in conn.execute('SELECT * FROM company_prediction_outcomes ORDER BY observed_at')]
        candidates, calibration = analytics.calibrate(candidates, outcomes)
        selected = analytics.select_diverse(candidates, limit=settings(conn)['predict_drafts_per_cycle'],
                                            exploration_slots=1 if len(candidates) > 1 else 0)
        items = []
        for chosen in selected:
            company, report = values[chosen['website']]
            payload = inputs.get(conn, report['research_reference'])
            payload['company']['revision'] = company['revision']
            payload['report'] = report
            report['selected_by'] = chosen.get('chosen_by', 'score')
            report['calibration'] = calibration
            report['analytics'].update({key: chosen[key] for key in
                ('prescore', 'score_basis', 'heuristic_prescore', 'calibrated_reply_probability') if key in chosen})
            items.append({'subject': company['website'], 'reference': inputs.put(conn, payload)})
        from applypilot.discovery.repository import transaction
        with transaction(conn, resource=receipt):
            prior = inputs.receipt(conn, receipt)
            if prior:
                return prior
            result = {'items': items}
            inputs.save_receipt(conn, receipt, result)
        return result
    company = value['company']
    if phase == 'prediction_prepare':
        if not store.enabled(conn, company['website']) or company['status'] == 'paused' or company['outcome'] in store.HANDS_OFF:
            return {'state': 'paused'}
        profile, facets, fingerprint = business.candidate_profile(context)
        company['careers_url'] = business._careers(conn, company)
        company['search_preferences'] = {'roles': [asdict(q) for q in context.intent.active_queries()],
                                         'markets': [asdict(m) for m in context.intent.active_markets()]}
        store.save_documents(conn, company['website'], business._signal_documents(conn, company))
        prepared = {**value, 'profile': profile, 'facets': facets, 'fingerprint': fingerprint,
                    'report': {'profile_fingerprint': fingerprint, 'implementation': 'job-swarm-native-v1'}}
        pages = ([{'board': board} for board in company.get('jobseek_boards', [])] or
                 ([{'url': company['careers_url']}] if company.get('careers_url') else []))
        homepage = inputs.put(conn, {'url': company['website']})
        vacancies = [_vacancy_page(conn, page) for page in pages]
        return {'reference': _put(conn, prepared), 'homepage': homepage, 'vacancies': vacancies}
    if phase == 'prediction_read':
        from applypilot.integrations.crawl import CrawlService
        from applypilot.integrations.types import SourceError
        url = inputs.get(conn, request.options['page_reference'])['url']
        request_url = (inputs.get(conn, request.options['request_reference'])['url']
                       if request.options.get('request_reference') else url)
        backend = request.options['backend']
        try:
            document = CrawlService(backend, before_request=check).read(request_url)
        except SourceError as exc:
            if getattr(exc, 'challenge_url', None) and backend != 'flaresolverr':
                return {'challenge_reference': _put(conn, {'url': exc.challenge_url})}
            raise
        home = request.options['home']
        doc = _document(company, document, home=home)
        if doc:
            store.save_documents(conn, company['website'], [doc])
        links, queries = [], []
        origin = research._host(company['website'])
        if research._under(research._host(document.url), origin):
            known = {row['url'] for row in store.documents(conn, company['website'])}
            links = [link for link in document.links if research._under(research._host(link), origin)
                     and research.CONTENT.search(urlsplit(link).path)]
            links.sort(key=lambda link: (link in known, research._index(link), link))
        elif research._under(research._host(document.url), 'arxiv.org') and doc is None:
            links = [link for link in document.links if research._under(research._host(link), 'arxiv.org')
                     and urlsplit(link).path.startswith('/html/')]
        if home:
            queries = [inputs.put(conn, {'query': query}) for query in research._queries(company, document)]
        result = {'identities': [inputs.put(conn, {'url': item}) for item in (url, document.url)],
                  'pages': [inputs.put(conn, {'url': link}) for link in dict.fromkeys(links)],
                  'queries': queries, 'document': doc is not None}
        conn.commit()
        return result
    if phase == 'prediction_search':
        from applypilot.integrations.search import SearchService
        query = inputs.get(conn, request.options['query_reference'])['query']
        hits = SearchService(request.options['provider'], before_request=check).search(query, limit=4)
        origin = research._host(company['website'])
        links = [hit.url for hit in hits if (
            research._under(research._host(hit.url), origin) or any(
                research._under(research._host(hit.url), host) for host in research.PRIMARY_HOSTS))]
        pages = [inputs.put(conn, {'url': link}) for link in dict.fromkeys(links)]
        conn.commit()
        return {'pages': pages}
    if phase == 'prediction_vacancy':
        from dataclasses import replace
        from applypilot.integrations.crawl import CrawlService
        from applypilot.integrations.types import SourceError
        from applypilot.discovery.company_targets import vacancy_step
        context = replace(context, reserve_budget=lambda *args, **kwargs: check() or True)
        entry = inputs.get(conn, request.options['page_reference'])
        if request.options.get('request_reference'):
            entry['url'] = inputs.get(conn, request.options['request_reference'])['url']
        entry['depth'] = request.options['depth']
        backend = request.options['backend']
        try:
            _, observations, following = vacancy_step({'pending': [entry], 'visited': []},
                {**company, 'job_url': ''}, context, CrawlService(backend, before_request=check))
        except SourceError as exc:
            if getattr(exc, 'challenge_url', None) and backend != 'flaresolverr':
                return {'challenge_reference': _put(conn, {'url': exc.challenge_url})}
            raise
        value['vacancy_observations'] = [*value.get('vacancy_observations', []), *observations]
        pages = [_vacancy_page(conn, page) for page in following['pending']]
        return {'reference': _put(conn, value), 'pages': pages}
    if phase == 'prediction_analyze':
        store.record_observations(conn, company['website'], value.get('vacancy_observations', []))
        documents = store.documents(conn, company['website'])
        postings, snapshots = store.hiring_corpus(conn, company['website'])
        report = value['report']
        report['documents'] = [{key: doc.get(key) for key in
            ('id', 'url', 'title', 'event_date', 'date_kind', 'observed_at', 'authored', 'truncated')} for doc in documents]
        report['coverage_gaps'] = list(dict.fromkeys(gap for doc in documents for gap in doc.get('coverage_gaps', [])))
        report['analytics'] = analytics.analyze(documents, value['profile'], value['facets'], postings, snapshots)
        return {'reference': _put(conn, {**value, 'documents': documents})}
    if phase == 'prediction_audit':
        value['report']['audit'] = research.audit(company, value['documents'], value['profile'], value['facets'], check)
        return {'reference': _put(conn, value)}
    if phase == 'prediction_draft':
        value['report']['draft'] = research.draft(company, value['documents'], value['profile'], value['report']['audit'], check)
        return {'reference': _put(conn, value)}
    if phase == 'prediction_publish':
        from applypilot.discovery.repository import transaction
        receipt = digest(('prediction-observation', request.reference))
        with transaction(conn, resource=receipt):
            prior = inputs.receipt(conn, receipt)
            if prior:
                return prior
            if business.candidate_profile(context)[2] != value['fingerprint']:
                return {'state': 'superseded'}
            report = value['report']
            status = 'drafted' if report.get('draft') else 'audited' if report['audit']['alignment'] >= .45 else 'low_fit'
            report['research_reference'] = request.reference
            if not store.save_report(conn, company, report, status=status):
                return {'state': 'superseded'}
            reference = inputs.put(conn, value)
            result = {'state': status, 'reference': reference}
            inputs.save_receipt(conn, receipt, result)
        return result
    raise ValueError('Unknown prediction phase')
