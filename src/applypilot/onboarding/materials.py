"""Public web material joins files, existing evidence and impact in one intake workspace."""
from __future__ import annotations
from urllib.parse import urlsplit
from .storage import digest, now, setup_lock, read_json, write_json
from .documents import clean_text


def collect_url(root, data, progress=lambda **kw: None):
    url=str(data.get('url') or '').strip()
    from applypilot.integrations.net import public_url
    from applypilot.integrations.types import SourceError
    reader=data.get('reader','auto')
    reader_truncated=False
    if reader not in {'auto','http','browseros'}: raise ValueError('Choose automatic public reader, HTTP, or your BrowserOS session')
    if reader=='browseros' and data.get('browser_consent') is not True:
        raise ValueError('Confirm reading this one page with your own BrowserOS session; signed-in content may be included')
    try:
        url=public_url(url,resolve=False)
        # GitHub retains revision-pinned provenance rather than a second scraper.
        if urlsplit(url).hostname in {'github.com','www.github.com'}:
            from .github import collect
            return collect(root,{'identity':url,'limit':data.get('limit',3),
                'use_ai':data.get('use_ai') is True,'share_with_model':data.get('share_with_model') is True,
                'include_private':data.get('include_private') is True},progress)
        if reader=='browseros':
            import asyncio
            from applypilot.unattended.browser import BrowserOS, BrowserUnavailable
            from applypilot.integrations.types import Document
            # A manually selected, owner-authorized page read, not a crawler
            # fallback. No recursive traversal or automatic login escalation.
            url=public_url(url)
            progress(message='Reading the selected page in your BrowserOS session. Only this page is imported; no messages are sent.')
            async def read_page():
                async with BrowserOS() as browser:
                    return await browser.read(url)
            try:
                page=asyncio.run(asyncio.wait_for(read_page(),100))
            except BrowserUnavailable as exc:
                raise ValueError('BrowserOS could not import this page: '+str(exc)[:250]+'. Sign in normally, or paste/upload content you are authorized to use.') from None
            final=public_url(page.get('url',''))
            text=page.get('text')
            if not isinstance(text,str): raise ValueError('BrowserOS returned no page text')
            reader_truncated=bool(page.get('truncated')) or len(text)>45000
            doc=Document(final,str(page.get('title') or '')[:500],text[:45000],(), 'browseros-owner-import')
        else:
            from applypilot.integrations.crawl import CrawlService
            from applypilot.integrations.state import SourceState
            progress(message='Reading the selected public page with your configured public reader; no account credentials are sent.')
            # auto now honors APPLYPILOT_CRAWLER, including local Playwright.
            doc=CrawlService(backend='http' if reader=='http' else None,
                state=SourceState(root/'free_sources'/'sources.pgstore')).read(url)
    except SourceError as exc:
        hint=(' Select BrowserOS with explicit session consent for a one-page owner import, or paste/upload the content.'
              if exc.kind=='policy' else '')
        raise ValueError('Could not import that page: '+str(exc)[:250]+hint) from None
    text=clean_text(doc.markdown)
    if len(text)<30: raise ValueError('No usable text; paste the content or upload a document instead')
    identifier=digest({'url':doc.url})[:20]
    row={'id':identifier,'name':doc.title or urlsplit(doc.url).hostname,'kind':'supporting','url':doc.url,
         'text':text,'characters':len(text),'sha256':digest(text.encode()),'extension':'.md',
         'imported_at':now(),'reader':doc.provider,'owner_browser_session':reader=='browseros','model_shared_by_intake':False,'truncated':reader_truncated or len(doc.markdown)>len(text)}
    with setup_lock(root):
        path=root/'setup'/'sources.json';sources=read_json(path,[])
        sources=[s for s in sources if s['id']!=identifier]
        if len(sources)>=20 or sum(s['characters'] for s in sources)+len(text)>500000:
            raise ValueError('Material limit reached; remove an old imported item first')
        write_json(path,sources+[row])
    return {'source':{k:v for k,v in row.items() if k!='text'},'note':'Selected page saved as material, not proof of personal authorship. Browser-session content can be private; model sharing still requires your separate opt-in.'}
