"""Finite crawl observations; Temporal owns traversal and backend selection."""
from dataclasses import replace
import re
from urllib.parse import urldefrag, urlsplit
from . import store as inputs
from .company_domain import _put


def execute(request, value, conn, check):
    from applypilot.integrations.crawl import CrawlService
    from applypilot.integrations.types import Document, SourceError, stable_id

    if request.phase == 'company_crawl_prepare':
        options = value['options']['crawl']
        root = urldefrag(options['url'])[0]
        return {'root_reference': _put(conn, {'url': root}),
                'backend': options['backend'], 'allow_browseros': options['allow_browseros'],
                'max_pages': options['max_pages'], 'max_depth': options['depth']}

    if request.phase == 'company_crawl_publish':
        result = {'documents': [inputs.get(conn, reference)['document']
                                for reference in request.options['documents']],
                  'errors': [], 'complete': request.options['complete'], 'bounded': True}
        return {'reference': _put(conn, {'result': result}), 'complete': True}

    options = inputs.get(conn, request.options['crawl_reference'])['options']['crawl']
    backend = request.options['backend']
    try:
        doc = CrawlService(backend, before_request=check,
                           allow_browseros=backend == 'browseros').read(value['url'], fresh=options['fresh'])
    except SourceError as exc:
        if (getattr(exc, 'challenge_url', None) and backend != 'flaresolverr'
                and options['backend'] in {'auto', 'hybrid'}):
            return {'challenge': True}
        raise
    thin = len(doc.markdown.strip()) < 600 or bool(re.search(
        r'(?:enable|requires?) javascript|javascript (?:is )?(?:required|disabled)', doc.markdown, re.I))
    previous = request.options.get('previous_reference')
    if previous:
        before = Document.from_dict(inputs.get(conn, previous)['document'])
        richest = doc if len(doc.markdown.strip()) > len(before.markdown.strip()) else before
        doc = replace(richest, provider=before.provider + '+' + doc.provider,
            links=tuple(dict.fromkeys([*before.links, *doc.links])),
            structured_data=tuple({stable_id(item): item for item in [*before.structured_data, *doc.structured_data]}.values()))

    host = urlsplit(options['url']).hostname
    links = list(dict.fromkeys(urldefrag(link)[0] for link in doc.links
                              if urlsplit(link).hostname == host))
    terms = set(re.findall(r'[a-z0-9]+', (options.get('focus') or '').lower()))
    links.sort(key=lambda link: (-len(terms & set(re.findall(r'[a-z0-9]+', urlsplit(link).path.lower()))), link))
    observed = {'document_reference': inputs.put(conn, {'document': doc.to_dict()}),
                'final_reference': inputs.put(conn, {'url': urldefrag(doc.url)[0]}),
                'links': [inputs.put(conn, {'url': link}) for link in links], 'thin': thin}
    conn.commit()
    return observed
