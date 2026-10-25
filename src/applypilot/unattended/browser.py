"""Fixed BrowserOS scripts for professional reads and exact, single-recipient DMs.

Owned-page challenges use the configured recovery adapter. Each page remains
bound to this session and is closed independently of application tabs.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import json
import logging
import re
from uuid import uuid4
from urllib.parse import urlsplit, urlencode
from applypilot.referrals.identity import profile_key


log = logging.getLogger(__name__)


class BrowserUnavailable(RuntimeError):
    """Safe, actionable integration errors; never attach raw page/tool output."""
    def __init__(self, message, *, code="browser_unavailable"):
        super().__init__(message)
        self.code = code


def verification_error(exc):
    """Report the observed exception without guessing recovery policy from prose."""
    from applypilot.onboarding.diagnostics import leaves
    from applypilot.operations.runtime import sanitize
    observed = next(iter(leaves(exc)), exc)
    details = getattr(observed, 'details', None)
    if isinstance(details, dict) and details.get('error'):
        return str(details.get('code') or type(observed).__name__), sanitize(str(details['error']), 2000)
    return str(getattr(observed, 'code', None) or type(observed).__name__), sanitize(str(observed), 2000)



class BrowserOS:
    def __init__(self, endpoint=None):
        from applypilot.apply.runtime import browseros_url
        self.endpoint=endpoint or browseros_url()
        p=urlsplit(self.endpoint)
        if p.scheme not in {'http','https'} or p.hostname not in {'localhost','127.0.0.1','::1'} or p.username or p.password:
            raise ValueError('BrowserOS must be a loopback MCP endpoint')
        self.stack=AsyncExitStack();self.pages=set();self.session=None
        self.run_properties=None;self.session_parameter=None
        self.evaluation_api="cdp"
        self.evaluation_fallback_api="cdp"
        self.page_api = True
        self.tool_schemas = {}
        self._fallback_events = set()
        self._tagged_markers = set()
        self.ownership_uncertain = False
        self.cleanup_failures = []
        self.prior_cleanup_failures = []
        self._cleanup_path = None

    async def __aenter__(self):
        from applypilot.execution import async_browser_operation
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        try:
            await self.stack.enter_async_context(async_browser_operation())
            streams=await self.stack.enter_async_context(streamable_http_client(self.endpoint,terminate_on_close=False))
            self.client=await self.stack.enter_async_context(ClientSession(streams[0],streams[1]))
            await asyncio.wait_for(self.client.initialize(),10)
            listing=await asyncio.wait_for(self.client.list_tools(),10)
            advertised={t.name:t for t in listing.tools}
            cursor=getattr(listing,'nextCursor',None)
            while cursor:
                listing=await asyncio.wait_for(self.client.list_tools(cursor=cursor),10)
                advertised.update({t.name:t for t in listing.tools})
                cursor=getattr(listing,'nextCursor',None)
            names=set(advertised)
            self.tool_schemas={name:(getattr(tool,'inputSchema',None) or {}) for name,tool in advertised.items()}
            run_tool=advertised.get('run')
            schema=getattr(run_tool,'inputSchema',None) or {}
            if not isinstance(schema,dict):
                raise BrowserUnavailable('BrowserOS run tool has an invalid input schema')
            self.run_properties=schema.get('properties') if isinstance(schema.get('properties'),dict) else None
            if not {'run','name_session'} <= names:
                raise BrowserUnavailable('This integration needs BrowserOS neo-compatible run/name_session tools; copy the correct MCP endpoint')
            result=await asyncio.wait_for(self.client.call_tool('name_session',{
                'name':'ApplyPilot networking','category':'data-extraction',
                'summary':'Bounded professional research and owner-authorized private messages'}),10)
            raw=result.model_dump(by_alias=True)
            value=self.decode(result)  # Check isError even when naming returns only text.
            meta=raw.get('_meta') or {}
            if not isinstance(meta,dict): meta={}
            self.session=meta.get('com.browseros.neo/session')
            if not self.session and isinstance(value,dict):
                self.session=value.get('session') or value.get('session_id') or value.get('sessionId')
            props=self.run_properties
            self.session_parameter=next((k for k in ('session','session_id','sessionId') if props is not None and k in props),None)
            if props is None:
                # Old servers returned an explicit handle without a useful schema.
                if not self.session:
                    raise BrowserUnavailable('BrowserOS protocol mismatch: run input schema and session handle are both absent')
                self.session_parameter='session'
            elif self.session_parameter in (schema.get('required') or []) and not self.session:
                raise BrowserUnavailable('BrowserOS protocol mismatch: run requires a session handle but name_session did not return one')
            # New Neo servers bind sessions to the MCP connection. An absent _meta
            # is valid only when the advertised run contract does not require it.
            probe=await self.run(
                'return {applypilot_probe:"owned-tab-v1",'
                'page_api:!!(browser.pages && typeof browser.pages.newPage==="function" '
                '&& typeof browser.pages.close==="function"),'
                'eval_api:typeof browser.evaluate==="function"?"sdk":'
                '(typeof browser.cdpJsonForPage==="function"?"cdp":null)};',timeout=10)
            if probe.get('applypilot_probe') != 'owned-tab-v1':
                raise BrowserUnavailable(
                    'BrowserOS did not return the execution probe value. Check its run tool contract; no verification tab was opened.',
                    code='run_result_missing')
            if (probe.get('page_api') is not True and 'tabs' not in names) or (
                    probe.get('eval_api') not in {'cdp','sdk'} and 'evaluate' not in names):
                raise BrowserUnavailable(
                    'BrowserOS does not expose the owned-page creation/read APIs required by ApplyPilot. Check the configured MCP endpoint and installed version.',
                    code='tool_contract')
            self.evaluation_fallback_api=probe['eval_api']
            self.page_api=probe.get('page_api') is True
            self.evaluation_api='tool' if 'evaluate' in names else self.evaluation_fallback_api
            from .browser_cleanup import checkpoint, reconcile
            try:
                self.prior_cleanup_failures = await reconcile(self)
            except Exception as exc:
                from .failures import describe
                self.prior_cleanup_failures = [describe(exc, channel='browseros', operation='cleanup_reconcile')]
            checkpoint(self)
            if self.prior_cleanup_failures:
                log.warning('Networking BrowserOS retained %d unresolved cleanup records', len(self.prior_cleanup_failures))
            return self
        except BaseException:
            await self.stack.aclose()
            raise

    async def __aexit__(self,*exc):
        try:
            await self.cleanup_pages()
        finally:
            try:
                from .browser_cleanup import checkpoint
                checkpoint(self, active=False, errors=self.cleanup_failures)
            except OSError as failure:
                log.warning('Networking cleanup receipt could not be saved: %s', type(failure).__name__)
            try:
                await self.stack.aclose()
            except Exception as failure:
                # Completed research/delivery remains valid despite MCP teardown.
                from applypilot.operations.emergency import emit
                emit('networking.browser_teardown', error=failure)
                log.warning('Networking BrowserOS teardown failed: %s', type(failure).__name__)

    async def cleanup_pages(self):
        from .browser_cleanup import checkpoint, valid_page
        from .failures import describe
        errors = []
        if self.ownership_uncertain:
            try:
                value = await self.run('return {pages:await browser.pages.list()};', timeout=10)
                observed = value.get('pages')
                if not isinstance(observed, list) or any(not isinstance(item, dict) or item.get('ownership') not in {'mine','user','other-agent'} for item in observed):
                    raise BrowserUnavailable('BrowserOS could not establish ownership during cleanup.', code='cleanup_ownership_unknown')
                pages = [item.get('pageId') for item in observed if item['ownership'] == 'mine']
                if not all(valid_page(page) for page in pages):
                    raise BrowserUnavailable('BrowserOS could not identify its owned pages during cleanup.', code='cleanup_ownership_unknown')
                self.pages.update(pages)
                self.ownership_uncertain = False
            except Exception as exc:
                errors.append(describe(exc, channel='browseros', operation='cleanup_reconcile'))
        for page in list(self.pages):
            try:
                await self.close(page)
            except Exception as exc:
                errors.append(describe(exc, channel='browseros', operation='close_page'))
        self.cleanup_failures = errors
        try:
            checkpoint(self, errors=errors)
        except OSError as exc:
            errors.append(describe(exc, channel='browseros', operation='cleanup_receipt'))
        if errors:
            log.warning('Networking page cleanup incomplete: %d failures, %d owned pages remain', len(errors), len(self.pages))
        return not errors and not self.pages and not self.ownership_uncertain

    @staticmethod
    def _tool_failure(text):
        """Preserve the adapter's failure; Temporal owns subsequent attempts."""
        from applypilot.operations.runtime import sanitize
        raise BrowserUnavailable(sanitize(str(text), 4000) or 'BrowserOS tool execution failed')

    @staticmethod
    def _data_text(text):
        """Remove a BrowserOS data fence, not instructions or arbitrary log lines.

        New servers fence even structuredContent.value. Decoding that JSON does
        not make its contents trusted: it remains input to the fixed validators.
        Match the SAME nonce at both ends; do not mine IDs from unrelated text.
        """
        text=str(text).strip()
        match=re.fullmatch(
            r"\[UNTRUSTED_PAGE_CONTENT nonce=(?P<nonce>[0-9a-f]{16}) "
            r"origin=\S+\][^\n]*\n"
            r"(?P<data>.*)\n\[END_UNTRUSTED_PAGE_CONTENT nonce=(?P=nonce)\]", text, re.S)
        return match.group('data').strip() if match else text

    @classmethod
    def decode(cls,response):
        raw=response.model_dump(by_alias=True)
        if raw.get('isError'):
            text=' '.join(str(b.get('text','')) for b in raw.get('content') or [] if isinstance(b,dict))
            value=raw.get('structuredContent')
            if isinstance(value,dict): text+=' '+str(value.get('error',''))
            cls._tool_failure(text)
        value=raw.get('structuredContent')
        if value is None:
            for block in raw.get('content') or []:
                if not isinstance(block,dict) or block.get('type')!='text': continue
                text=cls._data_text(block.get('text',''))
                try:
                    value=json.loads(text)
                    break
                except ValueError:
                    # BrowserOS's text-only rendering is `ok\nreturn: <JSON>`,
                    # optionally followed by logs. Parse only that return field.
                    match=re.match(r"^ok\r?\nreturn:\s*",text)
                    if match:
                        try:
                            value,end=json.JSONDecoder().raw_decode(text[match.end():])
                            tail=text[match.end()+end:]
                            if tail.strip() and not tail.startswith(('\nlogs:', '\r\nlogs:')):
                                value=None
                            else:
                                break
                        except ValueError:
                            pass
                    elif text.startswith('error:'):
                        cls._tool_failure(text)
        for _ in range(5):
            if isinstance(value,dict) and 'result' in value and not any(k in value for k in ('url','page_id','ok','messages')):
                value=value['result']
            else: break
        return value

    def _log_fallback(self, operation, capability, reason):
        """Record routing decisions without page data, arguments, or identities."""
        event=(operation,capability,reason)
        if event in self._fallback_events:
            return
        self._fallback_events.add(event)
        log.info('browseros_fallback operation=%s capability=%s reason=%s',
                 operation,capability,reason)

    @staticmethod
    def _fallback_reason(exc):
        if isinstance(exc,BrowserUnavailable):
            return exc.code
        return type(exc).__name__

    async def _native_response(self,name,args,timeout):
        return await asyncio.wait_for(self.client.call_tool(name,args),timeout+5)

    async def native(self,name,args=None,*,timeout=40):
        """Call one advertised granular BrowserOS tool in this owned session."""
        schema=self.tool_schemas.get(name)
        if not isinstance(schema,dict):
            raise BrowserUnavailable('BrowserOS does not advertise the requested native capability.',
                                     code='native_capability_missing')
        values=dict(args or {})
        props=schema.get('properties') if isinstance(schema.get('properties'),dict) else {}
        session_key=next((k for k in ('session','session_id','sessionId') if k in props),None)
        if session_key and self.session:
            values[session_key]=self.session
        response=await self._native_response(name,values,timeout)
        raw=response.model_dump(by_alias=True)
        meta=raw.get('_meta') or {}
        returned=meta.get('com.browseros.neo/session') if isinstance(meta,dict) else None
        if returned is not None and self.session and returned!=self.session:
            raise BrowserUnavailable('BrowserOS changed the owned session during an operation.',
                                     code='browser_session_changed')
        value=self.decode(response)
        if value is not None:
            return value
        texts=[]
        for block in raw.get('content') or []:
            if isinstance(block,dict) and block.get('type')=='text':
                text=self._data_text(block.get('text',''))
                if text:
                    texts.append(text)
        return '\n'.join(texts) if texts else None

    @staticmethod
    def content_text(value):
        """Extract direct read/snapshot text without assuming a server envelope."""
        if isinstance(value,str):
            return value
        if isinstance(value,list):
            return '\n'.join(filter(None,(BrowserOS.content_text(item) for item in value)))
        if isinstance(value,dict):
            for key in ('markdown','snapshot','text','content','value'):
                if key in value:
                    text=BrowserOS.content_text(value[key])
                    if text:
                        return text
        return ''

    async def native_read(self,page,*,format='markdown'):
        if 'read' not in self.tool_schemas:
            self._log_fallback('read','read','missing_capability')
            return None
        try:
            value=await self.native('read',{'page':page,'format':format},timeout=25)
            text=self.content_text(value)
            if not text:
                raise BrowserUnavailable('BrowserOS native read returned no content.',code='native_result_contract')
            return text
        except Exception as exc:
            self._log_fallback('read','read',self._fallback_reason(exc))
            return None

    async def snapshot_text(self,page,*,mode='interactive'):
        if 'snapshot' not in self.tool_schemas:
            self._log_fallback('snapshot','snapshot','missing_capability')
            return None
        try:
            value=await self.native('snapshot',{'page':page,'mode':mode},timeout=25)
            text=self.content_text(value)
            if not text:
                raise BrowserUnavailable('BrowserOS native snapshot returned no content.',code='native_result_contract')
            return text
        except Exception as exc:
            self._log_fallback('snapshot','snapshot',self._fallback_reason(exc))
            return None

    @staticmethod
    def snapshot_ref(snapshot,marker):
        refs=set()
        for line in str(snapshot or '').splitlines():
            if marker not in line:
                continue
            refs.update(re.findall(r'\bref=(e[0-9]+)\b',line))
        return next(iter(refs)) if len(refs)==1 else None

    @staticmethod
    def semantic_ref(snapshot,labels,roles=()):
        labels={str(label).strip().casefold() for label in labels if str(label).strip()}
        roles={str(role).strip().casefold() for role in roles if str(role).strip()}
        refs=set()
        for line in str(snapshot or '').splitlines():
            found=set(re.findall(r'\bref=(e[0-9]+)\b',line))
            if not found:
                continue
            folded=line.casefold()
            if roles and not any(re.search(r'\b'+re.escape(role)+r'\b',folded) for role in roles):
                continue
            quoted={value.strip().casefold() for value in re.findall(r'["\']([^"\']+)["\']',line)}
            matches=bool(labels & quoted)
            if not matches and not quoted:
                matches=any(re.search(r'(?<![\w])'+re.escape(label)+r'(?![\w])',folded) for label in labels)
            if matches:
                refs.update(found)
        return next(iter(refs)) if len(refs)==1 else None

    async def native_act(self,page,kind,**arguments):
        if 'act' not in self.tool_schemas:
            raise BrowserUnavailable('BrowserOS does not advertise native actions.',
                                     code='native_capability_missing')
        return await self.native('act',{'page':page,'kind':kind,**arguments},timeout=40)

    async def tagged_ref(self,page,candidates,marker,*,labels=(),roles=()):
        """Resolve only the uniquely validated DOM target into a native ref."""
        if 'snapshot' not in self.tool_schemas or 'act' not in self.tool_schemas:
            missing='snapshot' if 'snapshot' not in self.tool_schemas else 'act'
            self._log_fallback('resolve_ref',missing,'missing_capability')
            return None
        code=(
            'const es='+candidates+';if(es.length!==1)return {ok:false};const e=es[0];'
            'e.dataset.applypilotNativeLabelPresent=e.hasAttribute("aria-label")?"1":"0";'
            'e.dataset.applypilotNativeLabel=e.getAttribute("aria-label")||"";'
            'e.setAttribute("aria-label",'+json.dumps(marker)+');return {ok:true};')
        # Tagging changes DOM state. If the native evaluate reply is uncertain,
        # do not repeat it through CDP and risk replacing the saved original.
        tagged=await self.evaluate(page,code,allow_fallback=False)
        if not tagged.get('ok'):
            self._log_fallback('resolve_ref','snapshot','target_not_unique')
            return None
        self._tagged_markers.add(marker)
        snapshot=await self.snapshot_text(page)
        ref=self.snapshot_ref(snapshot,marker)
        if not ref:
            await self.restore_tag(page,marker)
            self._log_fallback('resolve_ref','snapshot','ref_not_exposed')
        return ref

    async def restore_tag(self,page,marker):
        if marker not in self._tagged_markers:
            return
        self._tagged_markers.discard(marker)
        code=(
            'const marker='+json.dumps(marker)+';const docs='+DOCUMENTS+'(()=>true);'
            'for(const doc of docs)for(const e of doc.querySelectorAll("[aria-label]")){'
            'if(e.getAttribute("aria-label")!==marker)continue;'
            'if(e.dataset.applypilotNativeLabelPresent==="1")e.setAttribute("aria-label",e.dataset.applypilotNativeLabel||"");'
            'else e.removeAttribute("aria-label");delete e.dataset.applypilotNativeLabel;'
            'delete e.dataset.applypilotNativeLabelPresent;}return {ok:true};')
        try:
            await self.evaluate(page,code)
        except Exception as exc:
            self._log_fallback('restore_ref_label','evaluate',self._fallback_reason(exc))

    async def pause(self,page,milliseconds=500):
        if 'wait' in self.tool_schemas:
            try:
                await self.native('wait',{'page':page,'for':'time','value':milliseconds,
                                          'timeout':max(milliseconds+1000,2000)},timeout=max(5,milliseconds/1000+3))
                return
            except Exception as exc:
                self._log_fallback('wait','wait',self._fallback_reason(exc))
        else:
            self._log_fallback('wait','wait','missing_capability')
        await asyncio.sleep(milliseconds/1000)

    async def run(self, code, *, timeout=40):
        args={'code':code}
        if self.run_properties is None or 'timeout' in self.run_properties:
            args['timeout']=timeout*1000
        if self.session_parameter and self.session:
            args[self.session_parameter]=self.session
        response=await asyncio.wait_for(self.client.call_tool('run',args),timeout+5)
        meta=response.model_dump(by_alias=True).get('_meta') or {}
        returned=meta.get('com.browseros.neo/session') if isinstance(meta,dict) else None
        if returned is not None and self.session and returned != self.session:
            raise BrowserUnavailable('BrowserOS changed the owned session during an operation.', code='browser_session_changed')
        result=self.decode(response)
        # The run transport's `ok` is NOT the script's result. Keep domain-level
        # {ok:false, reason:...} intact for caller validation, and unwrap only the
        # documented transport envelope. Never infer a page ID from logs.
        if isinstance(result,dict) and isinstance(result.get('ok'),bool) and any(k in result for k in ('value','logs','error')):
            if result['ok'] is not True:
                self._tool_failure(result.get('error',''))
            result=result.get('value')
        if isinstance(result,str):
            try: result=json.loads(self._data_text(result))
            except ValueError: result=None
        if not isinstance(result,dict):
            raise BrowserUnavailable(
                'BrowserOS returned no usable script result.',
                code='run_result_missing')
        return result

    async def page(self,url):
        # Ownership comes ONLY from this creation result, never the active tab
        # or the first entry in a list of the user's pre-existing pages.
        from .browser_cleanup import checkpoint, valid_page
        # Prior sessions were reconciled on connection; their cleanup receipts
        # remain retryable without preventing independent work in this session.
        if self.ownership_uncertain or self.cleanup_failures:
            if not await self.cleanup_pages():
                raise BrowserUnavailable('Owned BrowserOS pages still require cleanup before opening another page.', code='cleanup_pending')
        prior_unknown = self.ownership_uncertain
        self.ownership_uncertain = True
        checkpoint(self)
        if self.page_api:
            # Keep the SDK's scalar page ID in our own result envelope; tabs
            # also returns snapshot/page metadata with a different shape.
            result=await self.run('return {page_id:await browser.pages.newPage('+json.dumps(url)+')};')
        else:
            # A failed tab-creation response may still have created a tab. Never
            # repeat it through the SDK; cleanup adopts only session-owned pages.
            result=await self.native('tabs',{'action':'new','url':url,'background':True})
        page=(result.get('page_id',result.get('pageId',result.get('page')))
              if isinstance(result,dict) else None)
        if isinstance(page,dict):
            # Some adapters return a PageInfo instead of the scalar SDK ID.
            # A CDP targetId/tabId is deliberately NOT an interchangeable ID.
            page=page.get('pageId',page.get('page_id'))
        if not valid_page(page):
            raise BrowserUnavailable(
                'BrowserOS returned no owned page identifier; session cleanup retains the unresolved creation evidence.',
                code='page_id_missing')
        self.pages.add(page)
        self.ownership_uncertain = prior_unknown
        checkpoint(self)
        return page

    async def close(self,page):
        if page in self.pages:
            if 'tabs' in self.tool_schemas:
                result = await self.native('tabs',{'action':'close','page':page},timeout=10)
                if result is False or (isinstance(result,dict) and (result.get('ok') is False or result.get('success') is False or result.get('error'))):
                    raise BrowserUnavailable('BrowserOS did not close the owned page.', code='cleanup_unconfirmed')
            else:
                self._log_fallback('close','tabs','missing_capability')
                await self.run('await browser.pages.close('+json.dumps(page)+');return {ok:true};',timeout=10)
            self.pages.discard(page)
            from .browser_cleanup import checkpoint
            checkpoint(self)

    async def release_page(self, page):
        """Cleanup errors never replace a successfully read document or its cause."""
        try:
            await self.close(page)
        except Exception as exc:
            from .browser_cleanup import checkpoint
            from .failures import describe
            self.cleanup_failures.append(describe(exc, channel='browseros', operation='close_page'))
            try:
                checkpoint(self, errors=self.cleanup_failures)
            except OSError:
                log.warning('Networking page cleanup receipt could not be saved')
            log.warning('Networking page cleanup deferred: %s', type(exc).__name__)

    async def evaluate(self,page,code,*,allow_fallback=True):
        if page not in self.pages:
            raise ValueError('Not an owned networking page')

        def page_value(value):
            # The SDK returns {page, value}; native evaluate returns the value.
            if isinstance(value,dict) and 'page' in value and 'value' in value:
                if value['page'] != page:
                    raise BrowserUnavailable('BrowserOS returned data for a different page.',
                                             code='page_evaluation_failed')
                value=value['value']
            if not isinstance(value,dict):
                raise BrowserUnavailable('BrowserOS evaluate returned incomplete page data.',
                                         code='native_result_contract')
            return value

        if 'evaluate' in self.tool_schemas:
            try:
                arguments={'page':page,'code':code,'timeout':30000}
                if 'maxChars' in self.tool_schemas['evaluate'].get('properties',{}):
                    arguments['maxChars']=200000
                return page_value(await self.native('evaluate',arguments))
            except Exception as exc:
                if not allow_fallback:
                    raise
                self._log_fallback('evaluate','evaluate',self._fallback_reason(exc))
        else:
            self._log_fallback('evaluate','evaluate','missing_capability')
        if self.evaluation_fallback_api=='sdk':
            return page_value(await self.run('return await browser.evaluate('+json.dumps(page)+
                                             ',{code:'+json.dumps(code)+',maxChars:200000});'))
        self._log_fallback('evaluate','sdk_evaluate','missing_or_incompatible')
        # Compatibility route for servers without granular or SDK evaluate.
        params=json.dumps({'expression':'(async () => {\n'+code+'\n})()',
                           'returnByValue':True,'awaitPromise':True,'timeout':30000})
        result=await self.run(
            'const r=await browser.cdpJsonForPage('+json.dumps(page)+',"Runtime.evaluate",'+json.dumps(params)+');'
            'return {applypilot_evaluation:"cdp",exception:!!r.exceptionDetails,value:r.result?.value};')
        if result.get('applypilot_evaluation')!='cdp' or result.get('exception') is not False or not isinstance(result.get('value'),dict):
            raise BrowserUnavailable(
                'The owned BrowserOS tab did not return the expected page data. Check the networking session for a page-script failure; no account was verified.',
                code='page_evaluation_failed')
        return result['value']

    async def recover_challenge(self, page, *, observed=None):
        from applypilot.apply.cloudflare_tool import solve_owned_page
        from applypilot.execution import check_cancelled
        import os

        async def check():
            check_cancelled()
            if page not in self.pages:
                raise ValueError('Challenge recovery requires an owned networking page')

        await check()
        observed = observed or await self.evaluate(page, CHALLENGE_SCRIPT)
        if observed.get('browser_challenge') is not True:
            return False
        await solve_owned_page(page, self.run, proxy=os.getenv('APPLYPILOT_BROWSER_PROXY_URL'), check=check)
        remaining = await self.evaluate(page, CHALLENGE_SCRIPT)
        if remaining.get('browser_challenge') is True:
            raise BrowserUnavailable('The provider challenge remains on the owned page after solver recovery')
        return True

    async def structured_read(self,page):
        value=await self.evaluate(page,READ_SCRIPT)
        if await self.recover_challenge(page, observed=value):
            value = await self.evaluate(page, READ_SCRIPT)
        if not isinstance(value.get('url'),str) or not value['url'].strip():
            raise BrowserUnavailable('BrowserOS returned page content without its observed URL.',
                                     code='page_evaluation_failed')
        native=await self.native_read(page)
        if native:
            value['text']=native[:45000]
            value['truncated']=len(native)>45000
        return value

    async def read(self,url, *, expect_people=False):
        from applypilot.referrals.store import public_url
        public_url(url)
        try:
            identity = profile_key(url) or ''
        except ValueError:
            identity = ''
        expect_profile = identity.startswith(('linkedin:', 'x:'))
        p=await self.page(url)
        try:
            async with asyncio.timeout(120):
                while True:
                    value=await self.structured_read(p)
                    ready = (bool(value.get('people')) or value.get('search_empty') is True
                             if expect_people else
                             profile_key(value.get('url')) == identity and bool(value.get('name'))
                             and len(str(value.get('text') or '').strip()) >= 200
                             if expect_profile else
                             bool(value.get('text') or value.get('people') or value.get('owner_links')))
                    if ready:
                        break
                    await self.pause(p,500)
            return value
        except TimeoutError as exc:
            raise BrowserUnavailable(
                'Professional page did not provide the requested content or an explicit empty result.',
                code='search_not_ready' if expect_people else 'page_not_ready') from exc
        finally: await self.release_page(p)

    async def identity(self,channel):
        if channel not in {'linkedin','x'}:
            raise ValueError('Unknown owner channel')
        url='https://www.linkedin.com/in/me/' if channel=='linkedin' else 'https://x.com/home'
        page=await self.page(url)
        try:
            # The tab exists before a SPA has hydrated or /in/me has redirected.
            # Read only our tab, and never treat an arbitrary profile link as owner.
            async with asyncio.timeout(120):
                while True:
                    value=await self.structured_read(page)
                    if channel=='linkedin':
                        candidate=value.get('url')
                        try: key=profile_key(candidate)
                        except ValueError: key=None
                        if not key or key=='linkedin:me':
                            candidate=next((a.get('url') for a in value.get('owner_links',[]) if '/in/' in a.get('url','')),None)
                    else:
                        candidate=next((a.get('url') for a in value.get('owner_links',[]) if a.get('testid')=='AppTabBar_Profile_Link'),None)
                    try: key=profile_key(candidate)
                    except ValueError: key=None
                    if key and key.startswith(channel+':') and key not in {'linkedin:me','x:home'}:
                        return candidate
                    await self.pause(page,500)
        except TimeoutError as exc:
            raise BrowserUnavailable('The browser did not expose its signed-in owner profile during this observation.', code='identity_not_ready') from exc
        finally:
            await self.release_page(page)

    async def people(self,company,function,*,limit=4,connections=False):
        if connections:
            url='https://www.linkedin.com/mynetwork/invite-connect/connections/'
        else:
            url='https://www.linkedin.com/search/results/people/?'+urlencode({'keywords':company+' '+function})
        doc=await self.read(url, expect_people=True)
        people=[];seen=set()
        for a in doc.get('people',[]):
            try: identity=profile_key(a['profile_url'])
            except ValueError: continue
            if identity in seen or not a.get('name'): continue
            if connections and company.casefold() not in a.get('quote','').casefold(): continue
            seen.add(identity);people.append(a)
            if len(people)>=limit: break
        return people

    async def people_x(self,company,function,*,limit=4):
        url='https://x.com/search?'+urlencode({'q':company+' '+function,'f':'user'})
        doc=await self.read(url, expect_people=True)
        return [p for p in doc.get('people',[]) if (profile_key(p.get('profile_url')) or '').startswith('x:')][:limit]

    async def connections(self,*,page_number=0,limit=30):
        # Scroll only in our own network tab, never claim complete network coverage.
        p=await self.page('https://www.linkedin.com/mynetwork/invite-connect/connections/')
        try:
            for _ in range(min(page_number,20)):
                if 'act' in self.tool_schemas:
                    try:
                        await self.native_act(p,'scroll',direction='down',amount=6)
                    except Exception as exc:
                        self._log_fallback('scroll','act',self._fallback_reason(exc))
                        await self.evaluate(p,'window.scrollBy(0, Math.max(600,innerHeight)); return {ok:true};')
                else:
                    self._log_fallback('scroll','act','missing_capability')
                    await self.evaluate(p,'window.scrollBy(0, Math.max(600,innerHeight)); return {ok:true};')
                await self.pause(p,500)
            doc=await self.structured_read(p)
            if not doc.get('people') and doc.get('search_empty') is not True:
                raise BrowserUnavailable('The network page did not provide connections or an explicit empty result')
            people=doc.get('people',[])
            start=page_number*limit
            # Some layouts virtualize old cards; use the visible tail in those.
            return people[start:start+limit] if len(people)>start else people[-limit:]
        finally: await self.release_page(p)

    async def conversation(self,recipient,channel,owner):
        if profile_key(await self.identity(channel)) != profile_key(owner):
            raise BrowserUnavailable('Signed-in social account differs from the owner authorization', code='owner_mismatch')
        p=await self.page(recipient)
        try:
            candidates = MESSAGE_CANDIDATES.replace('CHANNEL_VALUE', json.dumps(channel))
            entry = None
            try:
                async with asyncio.timeout(120):
                    while True:
                        doc=await self.structured_read(p)
                        if profile_key(doc.get('url'))==profile_key(recipient):
                            entry=await self.evaluate(p, 'const es='+candidates+';'
                                'if(es.length!==1)return {ready:false};const a=es[0].closest("a[href]");'
                                'if(!a)return {ready:true};const u=new URL(a.href);'
                                'return {ready:true,compose_url:u.origin===location.origin&&'
                                '/^\\/messaging\\/compose\\/?$/.test(u.pathname)&&'
                                '!!u.searchParams.get("recipient")?u.href:null};')
                            if entry.get('ready'):
                                break
                        await self.pause(p,400)
            except TimeoutError as exc:
                raise BrowserUnavailable('Profile Message entry did not become uniquely ready; no message sent.',
                                         code='message_entry_not_ready') from exc
            marker='ApplyPilot message '+uuid4().hex
            ref=await self.tagged_ref(p,candidates,marker,
                                      labels=('message','send message'),roles=('button','link'))
            if ref:
                try:
                    await self.native_act(p,'click',ref=ref)
                except Exception:
                    self._log_fallback('open_message','act','outcome_uncertain')
                finally:
                    await self.restore_tag(p,marker)
            else:
                self._log_fallback('open_message','act','ref_unavailable')
                try:
                    result=await self.evaluate(p,OPEN_MESSAGE.replace('CHANNEL_VALUE',json.dumps(channel)),
                                               allow_fallback=False)
                except Exception:
                    self._log_fallback('open_message','evaluate','outcome_uncertain')
                    result={'ok':True}
                if not result.get('ok'):
                    raise BrowserUnavailable('Message entry changed before opening; no message sent.',
                                             code='message_entry_not_ready')
            try:
                snapshot=await self.wait_thread(p,recipient,channel,timeout=10)
            except BrowserUnavailable as exc:
                if exc.code not in {'recipient_not_ready','composer_not_ready'} or not entry.get('compose_url'):
                    raise
                replacement=await self.page(entry['compose_url'])
                await self.release_page(p)
                p=replacement
                snapshot=await self.wait_thread(p,recipient,channel,timeout=25)
            return p,snapshot
        except BaseException:
            await self.release_page(p);raise

    async def thread(self,page,recipient,channel):
        return await self.evaluate(page,THREAD_SCRIPT.replace('CHANNEL_VALUE',json.dumps(channel))
            .replace('RECIPIENT_VALUE',json.dumps(recipient)))

    async def wait_thread(self,page,recipient,channel,*,timeout):
        snapshot={}
        try:
            async with asyncio.timeout(timeout):
                while True:
                    snapshot=await self.thread(page,recipient,channel)
                    if snapshot.get('paid'):
                        raise BrowserUnavailable('This conversation requires paid messaging; no message sent.',
                                                 code='social_dm_unavailable')
                    if snapshot.get('verified') and not snapshot.get('busy'):
                        return snapshot
                    await self.pause(page,400)
        except TimeoutError as exc:
            code='composer_not_ready' if snapshot.get('recipient_verified') else 'recipient_not_ready'
            raise BrowserUnavailable('Cannot uniquely verify a ready free DM composer for the recipient; no message sent.',
                                     code=code) from exc

    @staticmethod
    def composer_candidates(recipient,channel,*,send=False):
        resolver=THREAD_CONTEXT.replace('CHANNEL_VALUE',json.dumps(channel)).replace('RECIPIENT_VALUE',json.dumps(recipient))
        if not send:
            return '(()=>{'+resolver+'return context.verified?[context.editor]:[];})()'
        return '(()=>{'+resolver+'return context.verified?sendControls(context):[];})()'

    async def fill(self,page,recipient,channel,body):
        snapshot=await self.thread(page,recipient,channel)
        if not snapshot.get('verified') or snapshot.get('composer'):
            raise BrowserUnavailable('Recipient changed or composer already contains a draft', code='composer_not_ready')
        candidates=self.composer_candidates(recipient,channel)
        marker='ApplyPilot composer '+uuid4().hex
        ref=await self.tagged_ref(page,candidates,marker,
                                  labels=('write a message','message'),roles=('textbox',))
        if ref:
            try:
                await self.native_act(page,'fill',ref=ref,value=body)
            except Exception as exc:
                current=await self.thread(page,recipient,channel)
                if current.get('verified') and normalize(current.get('composer',''))==normalize(body):
                    return current
                if not current.get('verified') or normalize(current.get('composer','')):
                    raise BrowserUnavailable('Native composer action left an uncertain draft; inspect it before retrying.',
                                             code='composer_uncertain') from exc
                self._log_fallback('fill_composer','act',self._fallback_reason(exc))
                ref=None
            finally:
                await self.restore_tag(page,marker)
        if not ref:
            prep=await self.evaluate(page,'const es='+candidates+';if(es.length!==1)return {ok:false};'
                'es[0].focus();return {ok:true};')
            if not prep.get('ok'): raise BrowserUnavailable('Composer is not unique', code='composer_not_ready')
            self._log_fallback('fill_composer','act','ref_unavailable')
            result=await self.run('const r=await browser.cdpJsonForPage('+json.dumps(page)+',"Input.insertText",'
                                  +json.dumps(json.dumps({'text':body}))
                                  +');return {ok:!r.error};')
            if result.get('ok') is not True:
                raise BrowserUnavailable('BrowserOS compatibility input failed.',code='composer_input_failed')
        snapshot=await self.thread(page,recipient,channel)
        if not snapshot.get('verified') or normalize(snapshot.get('composer',''))!=normalize(body):
            raise BrowserUnavailable('Composer bytes do not match the authorized message')
        return snapshot

    async def send(self,page,recipient,channel,body):
        # Caller has durably recorded the send intent BEFORE invoking this method.
        before=await self.thread(page,recipient,channel)
        if not before.get('verified') or normalize(before.get('composer',''))!=normalize(body):
            raise BrowserUnavailable('Final recipient/composer check failed')
        known={m['id'] for m in before.get('messages',[])}
        candidates=self.composer_candidates(recipient,channel,send=True)
        marker='ApplyPilot send '+uuid4().hex
        ref=await self.tagged_ref(page,candidates,marker,labels=('send',),roles=('button',))
        if ref:
            try:
                current=await self.thread(page,recipient,channel)
                if not current.get('verified') or normalize(current.get('composer',''))!=normalize(body):
                    raise BrowserUnavailable('Final recipient/composer check changed', code='composer_not_ready')
                try:
                    await self.native_act(page,'click',ref=ref)
                except Exception:
                    # Read confirmation is the only retry after a send click.
                    self._log_fallback('send_message','act','outcome_uncertain')
            finally:
                await self.restore_tag(page,marker)
        else:
            self._log_fallback('send_message','act','ref_unavailable')
            code=SEND_SCRIPT.replace('CHANNEL_VALUE',json.dumps(channel)).replace('BODY_VALUE',json.dumps(body)).replace('RECIPIENT_VALUE',json.dumps(recipient))
            clicked=await self.evaluate(page,code,allow_fallback=False)
            if not clicked.get('ok'): raise BrowserUnavailable('Send control unavailable; delivery is not proven')
        try:
            async with asyncio.timeout(30):
                while True:
                    after=await self.thread(page,recipient,channel)
                    matches=[m for m in after.get('messages',[]) if m.get('direction')=='out'
                             and normalize(m['text'])==normalize(body) and m['id'] not in known]
                    if (after.get('verified') and not normalize(after.get('composer','')) and len(matches)==1
                            and not after.get('error') and not after.get('busy')):
                        return {'id':matches[0]['id'],'threadId':after['url']}
                    if after.get('error') or after.get('paid'):
                        break
                    await self.pause(page,400)
        except TimeoutError:
            pass
        raise BrowserUnavailable('No unique post-send message confirmation; reconcile instead of resending',
                                 code='delivery_unconfirmed')


def normalize(text):
    return str(text).replace('\r\n','\n').replace('\xa0',' ').strip()


CHALLENGE_SCRIPT = r"""
return {browser_challenge:!!document.querySelector('script[src*="/cdn-cgi/challenge-platform/"]') &&
  (typeof window._cf_chl_opt==='object'||document.title.trim()==='Just a moment...')};
"""


DOCUMENTS=r"""(accept=>{
const docs=[document];
for(let i=0;i<docs.length;i++)for(const frame of docs[i].querySelectorAll('iframe')){
 try{const doc=frame.contentDocument;if(accept(frame)&&doc&&doc.location.origin===location.origin&&!docs.includes(doc))docs.push(doc)}catch{}
}
return docs;
})"""
READ_SCRIPT=r"""
const main=document.querySelector('main')||document.body;
if(!main)return {url:location.href,text:'',people:[],owner_links:[]};
const text=main.innerText||'';
const cards=[...main.querySelectorAll('li, [data-view-name="search-entity-result-universal-template"], .mn-connection-card')];
const people=[];
for(const card of cards){
 const a=card.querySelector('a[href*="/in/"]'); if(!a)continue;
 const name=card.querySelector('.mn-connection-card__name, .entity-result__title-text [aria-hidden="true"]')?.innerText||a.innerText;
 if(name?.trim())people.push({profile_url:a.href,name:name.trim().slice(0,200),quote:(card.innerText||'').slice(0,1600),source_url:location.href});
}
for(const card of main.querySelectorAll('[data-testid="UserCell"]')){
 const links=[...card.querySelectorAll('a[href]')].filter(a=>{try{return /^\/[A-Za-z0-9_]{1,15}\/?$/.test(new URL(a.href).pathname)}catch{return false}});
 const a=links[0];const name=card.querySelector('[data-testid="UserName"]')?.innerText||a?.innerText;
 if(a&&name?.trim())people.push({profile_url:a.href,name:name.trim().split('\n')[0].slice(0,200),quote:(card.innerText||'').slice(0,1600),source_url:location.href});
}
return {url:location.href,title:document.title,text:text.slice(0,45000),truncated:text.length>45000,
 browser_challenge:!!document.querySelector('script[src*="/cdn-cgi/challenge-platform/"]') &&
   (typeof window._cf_chl_opt==='object'||document.title.trim()==='Just a moment...'),
 search_empty:[...main.querySelectorAll('h1,h2,h3,[role="status"],[data-testid="emptyState"]')]
   .some(e=>e.getClientRects().length>0 && /^(?:no (?:matching )?(?:results|people|connections)(?: found)?|we couldn[’']t find (?:any )?(?:results|people)|you (?:don[’']t have|have no) (?:any )?connections)(?:[.!]|$)/i.test((e.innerText||'').trim())),
 name:main.querySelector('h1, [data-testid="UserName"]')?.innerText?.trim()||'',people:people.slice(0,100),
 links:[...main.querySelectorAll('a[href]')].slice(0,300).map(a=>({url:a.href,text:(a.innerText||'').slice(0,200)})),
 owner_links:[...document.querySelectorAll('a[data-testid="AppTabBar_Profile_Link"], a.global-nav__secondary-link[href*="/in/"]')].map(a=>({url:a.href,testid:a.getAttribute('data-testid')})),
 json_ld:[...document.querySelectorAll('script[type="application/ld+json"]')].slice(0,30).map(e=>e.textContent.slice(0,100000))};
"""
# A visible "Message" anchor can be a mailto link, not a social DM. Both
# execution paths share this filter; email delivery belongs to the Gmail outbox.
MESSAGE_CANDIDATES = r"""(()=>{
const channel=CHANNEL_VALUE,main=document.querySelector('main')||document.body;
const controls=channel==='x'?[...main.querySelectorAll('[data-testid="sendDMFromProfile"]')]:
 [...main.querySelectorAll('button,a')].filter(e=>/^(message|send message)$/i.test((e.innerText||e.getAttribute('aria-label')||'').trim()));
return controls.filter(e=>{
 if(!e.getClientRects().length||e.disabled||e.getAttribute('aria-disabled')==='true')return false;
 const anchor=e.closest('a[href]');
 if(!anchor)return true;
 try{
  const u=new URL(anchor.getAttribute('href'),location.href);
  const host=u.hostname.replace(/^www\./,'').toLowerCase();
  return u.protocol==='https:'&&!u.username&&!u.password&&
   (channel==='x'?['x.com','twitter.com'].includes(host):host==='linkedin.com'&&/^\/messaging(?:\/|$)/.test(u.pathname));
 }catch{return false;}
});
})()"""
OPEN_MESSAGE = "const buttons=" + MESSAGE_CANDIDATES + r""";
if(buttons.length!==1)return {ok:false,reason:'No unique free social DM entry point. Do not click mailto links; email requires the separately authorized Gmail outbox. Invitations and paid InMail are not used.'};
buttons[0].click();return {ok:true};
"""
THREAD_CONTEXT=r"""
const channel=CHANNEL_VALUE,recipient=RECIPIENT_VALUE;
const norm=u=>{try{const p=new URL(u);if(p.protocol!=='https:'||p.username||p.password)return '';return p.hostname.replace(/^www\./,'').replace(/^twitter.com$/,'x.com')+p.pathname.replace(/\/$/,'').toLowerCase()}catch{return ''}};
const visible=e=>!!e&&[...e.getClientRects()].some(r=>r.width>0&&r.height>0)&&e.ownerDocument.defaultView.getComputedStyle(e).visibility!=='hidden';
const docs="""+DOCUMENTS+r"""(visible);
const roots=docs.flatMap(doc=>channel==='linkedin'?[...doc.querySelectorAll('.msg-overlay-conversation-bubble, .msg-thread, .msg-compose-container')].filter(visible):[doc.querySelector('main')].filter(visible));
const matches=new Map();let recipientVerified=false,paid=false;
for(const root of roots){
 const compose=channel==='linkedin'&&root.matches('.msg-compose-container');
 const headers=channel==='linkedin'?[...root.querySelectorAll(compose?'.msg-compose__inmail-compose-entity-lockup':'.msg-overlay-bubble-header, .msg-thread__topcard')].filter(visible):[...root.querySelectorAll('[data-testid="DmActivityViewport"] header, [data-testid="DMConversationHeader"], [data-testid="primaryColumn"] [role="banner"]')].filter(visible);
 const links=headers.flatMap(h=>[...h.querySelectorAll(compose?'a.msg-compose__profile-link[href]':'a[href]')]).filter(a=>{
  try{const p=new URL(a.href);return channel==='linkedin'?/\/in\/[^/]+\/?$/.test(p.pathname):/^\/[A-Za-z0-9_]{1,15}\/?$/.test(p.pathname)}catch{return false}
 });
 const identities=new Set(links.map(a=>norm(a.href)));
 if(identities.size!==1||!identities.has(norm(recipient))||headers.some(h=>/group conversation/i.test(h.innerText||'')))continue;
 recipientVerified=true;
 const credits=[...root.querySelectorAll('.msg-inmail-credits-display, .msg-inmail-credit-count, [data-test-inmail-credits]')].filter(visible).map(e=>(e.innerText||'').trim());
 const free=credits.some(text=>/^free message\b/i.test(text));
 const charged=credits.some(text=>!/^free message\b/i.test(text)&&/credit|inmail|premium|\$|payment/i.test(text))||
  [...root.querySelectorAll('button,a[role="button"]')].filter(visible).some(e=>/^(?:buy (?:inmail )?credits|upgrade (?:to|your)|purchase|pay\b)/i.test((e.innerText||'').trim()));
 if(charged){paid=true;continue}
 if((compose||root.querySelector('.msg-inmail-compose-form-v2'))&&!free)continue;
 const editors=[...root.querySelectorAll(channel==='linkedin'?'.msg-form__contenteditable[contenteditable="true"]':'[data-testid="dmComposerTextInput"]')].filter(visible);
 if(editors.length!==1)continue;
 const editor=editors[0];
 if(!matches.has(editor)||matches.get(editor).root.contains(root))matches.set(editor,{root,editor});
}
const context=matches.size===1&&!paid?{...matches.values().next().value,verified:true,recipient_verified:true,paid:false}:{verified:false,recipient_verified:recipientVerified,paid};
const sendControls=ctx=>{
 const scope=ctx.editor.closest('form')||ctx.editor.closest('.msg-form')||ctx.editor.closest('[data-testid="DmActivityViewport"]');
 if(!scope||!ctx.root.contains(scope))return [];
 return [...scope.querySelectorAll(channel==='linkedin'?'.msg-form__send-button':'[data-testid="dmComposerSendButton"]')].filter(e=>visible(e)&&!e.disabled&&e.getAttribute('aria-disabled')!=='true');
};
"""
THREAD_SCRIPT=THREAD_CONTEXT+r"""
if(!context.verified)return {verified:false,recipient_verified:context.recipient_verified,paid:context.paid,url:location.href,messages:[],composer:''};
const {root,editor}=context;
const composer=editor.value??editor.innerText??'';
const entries=[...root.querySelectorAll(channel==='linkedin'?'.msg-s-event-listitem':'[data-testid="messageEntry"]')].slice(-60);
const messages=entries.map((e,i)=>{
 const text=(e.querySelector(channel==='linkedin'?'.msg-s-event-listitem__body':'[data-testid="tweetText"]')?.innerText||e.innerText||'').trim();
 const group=e.closest('.msg-s-message-group');
 let direction=group?.classList.contains('msg-s-message-group--me')?'out':group?.classList.contains('msg-s-message-group--other')?'in':null;
 if(e.getAttribute('data-direction')==='outgoing')direction='out';
 if(e.getAttribute('data-direction')==='incoming')direction='in';
 const sender=group?.querySelector('a.msg-s-message-group__profile-link, a.msg-s-message-group__name');
 if(sender && norm(sender.href)===norm(recipient))direction='in';
 const label=(e.getAttribute('aria-label')||'');
 if(/^you (?:sent|said)\b/i.test(label))direction='out';
 const id=e.getAttribute('data-event-urn')||e.getAttribute('data-message-id')||e.id||('ui:'+(e.querySelector('time')?.getAttribute('datetime')||'')+':'+text);
 return {id,text,direction,at:e.querySelector('time')?.getAttribute('datetime')||null};
}).filter(m=>m.text);
return {verified:true,recipient_verified:true,paid:false,url:editor.ownerDocument.location.href,composer,messages,
 busy:root.getAttribute('aria-busy')==='true'||[...root.querySelectorAll('[aria-busy="true"]')].some(visible),
 error:[...root.querySelectorAll('[role="alert"]')].some(e=>/failed|not sent|error/i.test(e.innerText||''))};
"""
SEND_SCRIPT=THREAD_CONTEXT+r"""
const body=BODY_VALUE;
const normBody=x=>(x||'').replace(/\r\n/g,'\n').replace(/\u00a0/g,' ').trim();
if(!context.verified||normBody(context.editor.value??context.editor.innerText)!==normBody(body))return {ok:false};
const buttons=sendControls(context);
if(buttons.length!==1)return {ok:false};buttons[0].click();return {ok:true};
"""
