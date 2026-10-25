"""Same-account Gmail desktop fallback for guarded application delivery.

This adapter operates only owned Gmail tabs in the already authenticated
BrowserOS profile. It never activates mailto, signs in, exports cookies, uses
undocumented Gmail API requests or lets an agent supply executable JavaScript.
A Send click is issued once and is not delivery proof. Proof comes from a
matching Sent message's original MIME, including attachment byte digests.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from email import policy
from email.parser import HeaderParser
from email.utils import getaddresses
import hashlib
import json
import re
from urllib.parse import urlencode, quote
from uuid import uuid4

from applypilot.tracking.gmail_browser_scripts import script
from applypilot.tracking.gmail_client import GmailReadError


class GmailBrowserError(GmailReadError):
    pass


def _normal(text):
    return str(text or '').replace('\r\n', '\n').replace('\u00a0', ' ').rstrip('\n')


def original_message(value: dict, message_id: str, thread_id: str) -> dict:
    """Decode bounded Show-original metadata; never infer missing identity."""
    if not value.get('ready') or not isinstance(value.get('headers'), str):
        raise GmailBrowserError('gmail_original_unavailable')
    parser = HeaderParser(policy=policy.default)
    headers = parser.parsestr(value['headers'])
    if headers.defects or any(len(headers.get_all(key, [])) != 1 for key in ('from', 'to', 'subject', 'message-id')):
        raise GmailBrowserError('gmail_original_header_ambiguous')
    senders = getaddresses([str(headers['from'])])
    if len(senders) != 1:
        raise GmailBrowserError('gmail_original_sender_ambiguous')
    plain, attachments = [], []
    for part in value.get('parts', []):
        parsed = parser.parsestr(part['headers'])
        name = parsed.get_filename()
        if name or parsed.get_content_disposition() == 'attachment':
            if not name or not re.fullmatch('[0-9a-f]{64}', part.get('sha256', '')):
                raise GmailBrowserError('gmail_original_attachment_unverified')
            attachments.append({'name': name, 'size': part['size'], 'sha256': part['sha256']})
        elif parsed.get_content_type() == 'text/plain' and 'data' in part:
            try:
                plain.append(base64.b64decode(part['data'], validate=True).decode(parsed.get_content_charset() or 'utf-8'))
            except (ValueError, LookupError, UnicodeError) as exc:
                raise GmailBrowserError('gmail_original_text_unsupported') from exc
    if not plain:
        raise GmailBrowserError('gmail_original_plaintext_missing')
    return {'id': message_id, 'thread_id': thread_id, 'sender': senders[0][1].casefold(),
            'to': str(headers['to']), 'subject': str(headers['subject']),
            'rfc_message_id': str(headers['message-id']).strip(), 'body': '\n'.join(plain),
            'extra_recipients': bool(headers.get('cc') or headers.get('bcc')),
            'attachments': attachments, 'labels': []}


class GmailBrowserMailbox:
    transport = 'gmail_browseros'

    def __init__(self, browser, expected_address: str):
        from applypilot.referrals.store import email_address
        self.browser = browser
        self.address = email_address(expected_address)
        self.mailbox_key = hashlib.sha256(self.address.encode()).hexdigest()
        self.base = 'https://mail.google.com/mail/?' + urlencode({'authuser': self.address})
        self.page = None
        self.token = uuid4().hex
        self.prepared = None
        self.send_started = False
        self.tools = {'get_profile', 'list_messages', 'get_message', 'send_message'}

    async def _eval(self, page, operation, data=None):
        value = await self.browser.evaluate(
            page, script(operation, expected=self.address, token=self.token, data=data),
            allow_fallback=operation not in {'start', 'upload', 'send', 'arm_send'})
        if not isinstance(value, dict) or value.get('ok') is not True:
            kind = value.get('error', 'gmail_dom_contract') if isinstance(value, dict) else 'gmail_dom_contract'
            # Error codes originate only in the fixed script; never return DOM prose.
            if not isinstance(kind, str) or not re.fullmatch('[a-z_]{1,80}', kind):
                kind = 'gmail_dom_contract'
            raise GmailBrowserError(kind)
        return value

    async def _wait(self, page, operation, data=None, *, seconds=20, predicate=None):
        async with asyncio.timeout(seconds):
            while True:
                try:
                    result = await self._eval(page, operation, data)
                except GmailBrowserError as exc:
                    if exc.kind != 'gmail_identity_not_ready':
                        raise
                else:
                    if (predicate(result) if predicate else result.get('ready')):
                        return result
                if hasattr(self.browser, 'pause'):
                    await self.browser.pause(page, 400)
                else:
                    await asyncio.sleep(.4)

    async def probe(self, *, require_send=False):
        if self.page is None:
            self.page = await self.browser.page(self.base + '#inbox')
        result = await self._wait(self.page, 'identity', predicate=lambda r: bool(r.get('address')))
        if require_send and not result.get('compose_available'):
            raise GmailBrowserError('gmail_compose_control_unavailable')
        return {'identity_verified': True, 'read_verified': True, 'send_tool_available': result['compose_available']}

    async def _cdp(self, page, method, params):
        # The host's CDP method is part of the same BrowserOS contract already
        # used by application file upload. There is no new external endpoint.
        if hasattr(self.browser, '_log_fallback'):
            self.browser._log_fallback('gmail_' + method.split('.', 1)[0].casefold(), 'native_tool',
                                       'structured_or_byte_verification')
        result = await self.browser.run('const r=await browser.cdpJsonForPage(' + json.dumps(page) + ',' + json.dumps(method) + ',' + json.dumps(json.dumps(params)) + ');return {result:r};')
        if not isinstance(result, dict) or not isinstance(result.get('result'), dict) or result['result'].get('error'):
            raise GmailBrowserError('gmail_cdp_contract')
        return result['result']

    def _field_candidates(self, field):
        root='document.querySelector('+json.dumps('[data-applypilot-gmail-compose="'+self.token+'"]')+')'
        selectors={
            'to':'input[name="to"],textarea[name="to"],input[peoplekit-id],input[role="combobox"]',
            'subject':'input[name="subjectbox"]',
            'body':'[contenteditable="true"][role="textbox"],[contenteditable="true"][g_editable="true"]',
        }
        selector=selectors[field]
        return ('(()=>{const c='+root+';return c?[...c.querySelectorAll('+json.dumps(selector)+')]'
                '.filter(e=>e.getClientRects().length):[]})()')

    async def _fill_cdp(self, field, value):
        await self._eval(self.page, 'focus', {'field': field})
        if field == 'body':
            # Chromium's multi-line insertText can create nested block nodes
            # whose rendered plaintext gains extra blank lines. Insert soft line
            # breaks instead, in one bounded host call (no per-line MCP trips).
            commands = []
            for index, line in enumerate(value.split("\n")):
                if index:
                    commands.extend(("Input.dispatchKeyEvent", {"type": kind, "key": "Enter",
                        "code": "Enter", "windowsVirtualKeyCode": 13, "modifiers": 8,
                        **({"text": "\r", "unmodifiedText": "\r"} if kind == "keyDown" else {})})
                        for kind in ("keyDown", "keyUp"))
                if line or not index:
                    commands.append(("Input.insertText", {"text": line}))
            result = await self.browser.run('const page=' + json.dumps(self.page)
                + ';const commands=' + json.dumps(commands)
                + ';for(const [method,params] of commands){const r=await browser.cdpJsonForPage(page,method,JSON.stringify(params));'
                + 'if(r.error)throw new Error("gmail_body_input_failed");}return {result:{}};')
            if not isinstance(result, dict) or result.get("result") != {}:
                raise GmailBrowserError('gmail_cdp_contract')
        else:
            await self._cdp(self.page, 'Input.insertText', {'text': value})

    async def _fill(self, field, value):
        marker='ApplyPilot Gmail '+field+' '+uuid4().hex
        labels={'to':('to','recipients'),'subject':('subject',),'body':('message body','body')}[field]
        roles=('textbox','combobox') if field=='to' else ('textbox',)
        ref=await self.browser.tagged_ref(self.page,self._field_candidates(field),marker,
                                          labels=labels,roles=roles)
        native=bool(ref)
        if ref:
            try:
                await self.browser.native_act(self.page,'fill',ref=ref,value=value)
            except Exception as exc:
                current=await self._eval(self.page,'snapshot')
                exact=(field=='subject' and current.get('subject')==value
                       or field=='body' and _normal(current.get('body'))==_normal(value)
                       or field=='to' and current.get('recipients')==[value] and not current.get('pending_recipient'))
                empty=(field=='subject' and not current.get('subject')
                       or field=='body' and not _normal(current.get('body'))
                       or field=='to' and not current.get('recipients') and not current.get('pending_recipient'))
                if not exact and not empty:
                    raise GmailBrowserError('gmail_native_fill_uncertain') from exc
                if empty:
                    if hasattr(self.browser,'_log_fallback'):
                        self.browser._log_fallback('gmail_fill_'+field,'act',
                                                   self.browser._fallback_reason(exc))
                    ref=None
                    native=False
            finally:
                await self.browser.restore_tag(self.page,marker)
        if not ref:
            if hasattr(self.browser,'_log_fallback'):
                self.browser._log_fallback('gmail_fill_'+field,'act','ref_unavailable')
            await self._fill_cdp(field,value)
        if field == 'to':
            if native:
                current=await self._eval(self.page,'snapshot')
                if current.get('recipients')==[value] and not current.get('pending_recipient'):
                    return
                enter_marker='ApplyPilot Gmail recipient '+uuid4().hex
                enter_ref=await self.browser.tagged_ref(self.page,self._field_candidates(field),enter_marker,
                                                        labels=('to','recipients'),roles=('textbox','combobox'))
                if not enter_ref:
                    current=await self._eval(self.page,'snapshot')
                    if current.get('recipients')==[value] and not current.get('pending_recipient'):
                        return
                    raise GmailBrowserError('gmail_recipient_ref_unavailable')
                try:
                    await self.browser.native_act(self.page,'press',ref=enter_ref,key='Enter')
                except Exception as exc:
                    current=await self._eval(self.page,'snapshot')
                    if current.get('recipients')!=[value] or current.get('pending_recipient'):
                        raise GmailBrowserError('gmail_recipient_commit_uncertain') from exc
                finally:
                    await self.browser.restore_tag(self.page,enter_marker)
            else:
                for kind in ('keyDown', 'keyUp'):
                    await self._cdp(self.page, 'Input.dispatchKeyEvent', {'type': kind, 'key': 'Enter', 'code': 'Enter', 'windowsVirtualKeyCode': 13})

    async def prepare_application(self, row, payload):
        if self.prepared is not None or self.send_started:
            raise GmailBrowserError('gmail_compose_already_prepared')
        if payload.get('from') != self.address or not payload.get('delivery_reference'):
            raise GmailBrowserError('gmail_payload_identity')
        await self.probe(require_send=True)
        compose_candidates=(
            '(()=>{if([...document.querySelectorAll("input[name=\\"subjectbox\\"]")]'
            '.some(e=>e.getClientRects().length))return [];'
            'const buttons=[...document.querySelectorAll("[gh=\\"cm\\"],[role=\\"button\\"],button")]'
            '.filter(e=>e.getClientRects().length&&(e.getAttribute("gh")==="cm"||'
            '(e.getAttribute("aria-label")||e.textContent||"").trim()==="Compose"));'
            'const exact=buttons.filter(e=>e.getAttribute("gh")==="cm");return exact.length?exact:buttons})()')
        marker='ApplyPilot Gmail compose '+uuid4().hex
        ref=await self.browser.tagged_ref(self.page,compose_candidates,marker,
                                          labels=('compose',),roles=('button',))
        if ref:
            try:
                await self.browser.native_act(self.page,'click',ref=ref)
            except Exception as exc:
                try:
                    await self._wait(self.page,'claim',seconds=4)
                except Exception:
                    raise GmailBrowserError('gmail_compose_click_uncertain') from exc
            finally:
                await self.browser.restore_tag(self.page,marker)
        else:
            if hasattr(self.browser,'_log_fallback'):
                self.browser._log_fallback('gmail_start_compose','act','ref_unavailable')
            await self._eval(self.page, 'start')
        await self._wait(self.page, 'claim')
        await self._fill('to', payload['to'])
        await self._fill('subject', payload['subject'])
        await self._fill('body', payload['body'])
        await self._eval(self.page, 'reveal_from')
        if payload['attachments']:
            await self._eval(self.page, 'stage_upload')
            if hasattr(self.browser,'_log_fallback'):
                self.browser._log_fallback('gmail_upload','upload','hidden_staging_input')
            document = await self._cdp(self.page, 'DOM.getDocument', {'depth': 0})
            node = await self._cdp(self.page, 'DOM.querySelector', {'nodeId': document['root']['nodeId'],
                'selector': 'input[data-applypilot-gmail-stage="' + self.token + '"]'})
            if not node.get('nodeId'):
                raise GmailBrowserError('gmail_upload_input_contract')
            await self._cdp(self.page, 'DOM.setFileInputFiles', {'nodeId': node['nodeId'], 'files': [a['path'] for a in payload['attachments']]})
            await self._eval(self.page, 'upload', {'attachments': payload['attachments']})
        snapshot = await self._wait(self.page, 'snapshot', seconds=45,
            predicate=lambda s: not s.get('uploading') and sorted(s.get('attachments', [])) == sorted(a['name'] for a in payload['attachments']))
        self.validate_snapshot(snapshot, payload)
        self.prepared = json.dumps(payload, sort_keys=True, separators=(',', ':'))

    @staticmethod
    def validate_snapshot(snapshot, payload):
        if snapshot.get('from') != payload['from']:
            raise GmailBrowserError('gmail_selected_sender_unverified')
        if (snapshot.get('subject') != payload['subject'] or _normal(snapshot.get('body')) != _normal(payload['body'])
                or snapshot.get('recipients') != [payload['to']] or snapshot.get('extra_recipients')
                or snapshot.get('pending_recipient') or snapshot.get('uploading') or not snapshot.get('send_ready')
                or sorted(snapshot.get('attachments', [])) != sorted(a['name'] for a in payload['attachments'])):
            raise GmailBrowserError('gmail_composition_changed')

    async def send_prepared(self, row, payload):
        if self.send_started or self.prepared != json.dumps(payload, sort_keys=True, separators=(',', ':')):
            raise GmailBrowserError('gmail_send_not_prepared')
        if row.get('state') != 'sending' or not row.get('dispatch_token'):
            raise GmailBrowserError('gmail_durable_intent_required')
        self.send_started = True
        snapshot=await self._eval(self.page,'snapshot')
        self.validate_snapshot(snapshot,payload)
        candidates=(
            '(()=>{const c=document.querySelector('+json.dumps('[data-applypilot-gmail-compose="'+self.token+'"]')+');'
            'if(!c)return [];return [...c.querySelectorAll("[role=\\"button\\"],button")]'
            '.filter(e=>e.getClientRects().length&&!e.disabled&&e.getAttribute("aria-disabled")!=="true"&&'
            '/^(Send|Send \\(.*\\))$/.test((e.getAttribute("data-tooltip")||e.getAttribute("aria-label")||e.textContent||"").trim()))})()')
        marker='ApplyPilot Gmail send '+uuid4().hex
        ref=await self.browser.tagged_ref(self.page,candidates,marker,
                                          labels=('send',),roles=('button',))
        if ref:
            # Arm before the native action. Regardless of its response, this
            # mailbox instance cannot issue another Send and the caller must
            # reconcile the exact Sent MIME as delivery proof.
            await self._eval(self.page,'arm_send',payload)
            try:
                await self.browser.native_act(self.page,'click',ref=ref)
            except Exception:
                if hasattr(self.browser,'_log_fallback'):
                    self.browser._log_fallback('gmail_send','act','outcome_uncertain')
            finally:
                await self.browser.restore_tag(self.page,marker)
        else:
            if hasattr(self.browser,'_log_fallback'):
                self.browser._log_fallback('gmail_send','act','ref_unavailable')
            await self._eval(self.page, 'send', payload)  # Never retry this operation.
        return {'clicked': True}  # Not proof: caller must reconcile Sent.

    async def _search(self, query):
        page = await self.browser.page(self.base + '#search/' + quote(query, safe=''))
        try:
            result = await self._wait(page, 'search')
            return result['thread_ids']
        finally:
            await self.browser.close(page)

    async def find_sent_application(self, rfc_id, *, reference=None):
        if not re.fullmatch(r'<applypilot-application-[0-9a-f]{32}@applications\.invalid>', rfc_id):
            raise GmailBrowserError('gmail_reconciliation_identity')
        if reference is not None and not re.fullmatch(r'applypilot-application-[0-9a-f]{32}', reference):
            raise GmailBrowserError('gmail_reconciliation_identity')
        query = 'in:sent ' + ('"' + reference + '"' if reference else 'rfc822msgid:' + rfc_id[1:-1])
        threads = await self._search(query)
        messages, seen = [], set()
        for thread in threads:
            page = await self.browser.page(self.base + '#all/' + thread)
            try:
                data = await self._wait(page, 'thread')
            finally:
                await self.browser.close(page)
            for mid in data['message_ids']:
                if mid in seen:
                    continue
                seen.add(mid)
                if len(seen) > 20:
                    raise GmailBrowserError('gmail_reconciliation_ambiguous')
                source = await self.browser.page(self.base + '&' + urlencode({'view': 'om', 'th': mid}))
                try:
                    raw = await self._wait(source, 'original', {'message_id': mid})
                    message = original_message(raw, mid, thread)
                finally:
                    await self.browser.close(source)
                if (reference and reference not in message['body']) or (not reference and message['rfc_message_id'] != rfc_id):
                    continue
                actual = message['rfc_message_id']
                if not re.fullmatch(r'<[^\s<>]+@[^\s<>]+>', actual):
                    raise GmailBrowserError('gmail_original_identity')
                # A conversation can include both inbound and outbound messages.
                # Verify SENT membership for THIS exact RFC Message-ID too.
                sent_threads = await self._search('in:sent rfc822msgid:' + actual[1:-1])
                if thread in sent_threads:
                    message['labels'] = ['SENT']
                    messages.append(message)
        return messages


@asynccontextmanager
async def open_gmail_browser(expected_address, *, browser=None):
    """Reuse an existing bridge or open a session scoped to this operation."""
    if browser is not None:
        mailbox = GmailBrowserMailbox(browser, expected_address)
        try:
            await mailbox.probe()
            yield mailbox
        finally:
            # Only pages created by this facade, never the worker's posting tab.
            await browser.cleanup_pages()
        return
    from applypilot.unattended.browser import BrowserOS
    async with BrowserOS() as owned:
        mailbox = GmailBrowserMailbox(owned, expected_address)
        await mailbox.probe()
        yield mailbox


class BridgeGmailPages:
    """Owned-page facade over the existing application MCP connection.

    Application tabs are not in this facade's page set. The enclosing bridge
    still journals/owns the entire session and performs final crash cleanup.
    """
    def __init__(self, run, persist, *, native=None, tool_schemas=None):
        self._run, self._persist, self._native = run, persist, native
        self.pages = set()
        self.ownership_uncertain = False
        self.tool_schemas = dict(tool_schemas or {}) if native else {}
        self.evaluation_api = 'tool' if 'evaluate' in self.tool_schemas else 'cdp'
        self.evaluation_fallback_api = 'cdp'
        self.session = None  # The enclosing bridge injects its opaque handle.
        self._fallback_events = set()
        self._tagged_markers = set()

    async def run(self, code, *, timeout=40):
        async with asyncio.timeout(timeout):
            return await self._run(code)

    async def _native_response(self, name, args, timeout):
        if self._native is None:
            raise GmailBrowserError('gmail_native_tool_unavailable')
        return await asyncio.wait_for(self._native(name,args),timeout+5)

    async def native(self, name, args=None, *, timeout=40):
        from applypilot.unattended.browser import BrowserOS
        return await BrowserOS.native(self,name,args,timeout=timeout)

    def _log_fallback(self, operation, capability, reason):
        from applypilot.unattended.browser import BrowserOS
        return BrowserOS._log_fallback(self,operation,capability,reason)

    @staticmethod
    def _fallback_reason(exc):
        from applypilot.unattended.browser import BrowserOS
        return BrowserOS._fallback_reason(exc)

    @staticmethod
    def content_text(value):
        from applypilot.unattended.browser import BrowserOS
        return BrowserOS.content_text(value)

    @staticmethod
    def snapshot_ref(snapshot, marker):
        from applypilot.unattended.browser import BrowserOS
        return BrowserOS.snapshot_ref(snapshot,marker)

    @staticmethod
    def semantic_ref(snapshot,labels,roles=()):
        from applypilot.unattended.browser import BrowserOS
        return BrowserOS.semantic_ref(snapshot,labels,roles)

    async def snapshot_text(self,page,*,mode='interactive'):
        from applypilot.unattended.browser import BrowserOS
        return await BrowserOS.snapshot_text(self,page,mode=mode)

    async def native_act(self,page,kind,**arguments):
        from applypilot.unattended.browser import BrowserOS
        return await BrowserOS.native_act(self,page,kind,**arguments)

    async def tagged_ref(self,page,candidates,marker,*,labels=(),roles=()):
        from applypilot.unattended.browser import BrowserOS
        return await BrowserOS.tagged_ref(self,page,candidates,marker,labels=labels,roles=roles)

    async def restore_tag(self,page,marker):
        from applypilot.unattended.browser import BrowserOS
        return await BrowserOS.restore_tag(self,page,marker)

    async def pause(self,page,milliseconds=500):
        from applypilot.unattended.browser import BrowserOS
        return await BrowserOS.pause(self,page,milliseconds)

    async def page(self, url):
        from applypilot.unattended.browser import BrowserOS
        await self._persist({'gmail_cleanup': 'unknown'}, event='gmail_page_opening', critical=True)
        page = await BrowserOS.page(self, url)
        await self._persist({'gmail_pages': sorted(self.pages, key=str)}, event='gmail_page_owned', critical=True)
        return page

    async def close(self, page):
        from applypilot.unattended.browser import BrowserOS
        await BrowserOS.close(self, page)
        await self._persist({'gmail_pages': sorted(self.pages, key=str)}, event='gmail_page_closed', critical=True)

    async def evaluate(self, page, code, *, allow_fallback=True):
        from applypilot.unattended.browser import BrowserOS
        return await BrowserOS.evaluate(self, page, code, allow_fallback=allow_fallback)

    async def cleanup_pages(self):
        # Never adopt every session-owned page on an ambiguous creation: that
        # would include the posting. The enclosing bridge can clean the group.
        if self.ownership_uncertain:
            return False
        for page in list(self.pages):
            try:
                await self.close(page)
            except Exception:
                return False
        await self._persist({'gmail_cleanup': 'complete'}, event='gmail_pages_cleaned', critical=True)
        return True
