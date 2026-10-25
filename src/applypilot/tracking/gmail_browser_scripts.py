"""Fixed Gmail desktop DOM operations. No provider API, cookie export or agent JS.

The desktop DOM is deliberately a checked adapter contract. An unfamiliar
layout returns a blocker; it is not a reason to relax sender/send verification.
"""
from __future__ import annotations

import json

COMMON = r'''
const visible=e=>!!e && !!e.getClientRects().length;
const norm=s=>String(s||'').replace(/\r\n/g,'\n').replace(/\u00a0/g,' ').replace(/\n+$/,'');
const addresses=s=>Array.from(new Set((String(s||'').match(/[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/g)||[]).map(x=>x.toLowerCase())));
const fail=error=>({ok:false,error});
if(location.origin!=='https://mail.google.com'||!location.pathname.startsWith('/mail/'))return fail('gmail_origin_required');
function account(){
  const banner=document.querySelector('header,[role="banner"]');
  if(!banner)return null;
  const nodes=Array.from(banner.querySelectorAll('a[href],button[aria-label]')).filter(e=>{
    const label=e.getAttribute('aria-label')||'';
    let url;try{url=new URL(e.href);}catch{}
    return visible(e) && ((url?.origin==='https://accounts.google.com' && /SignOutOptions|Logout/.test(url.pathname)) || /^Google Account:/i.test(label));
  });
  const found=Array.from(new Set(nodes.flatMap(e=>addresses((e.getAttribute('aria-label')||'')+' '+(e.getAttribute('title')||'')))));
  return found.length===1?found[0]:null;
}
const owner=account();
if(!owner)return fail('gmail_identity_not_ready');
if(owner!==expected)return fail('mailbox_identity_mismatch');
function compose(){
  const matches=Array.from(document.querySelectorAll('[data-applypilot-gmail-compose]')).filter(e=>e.dataset.applypilotGmailCompose===token);
  return matches.length===1&&visible(matches[0])?matches[0]:null;
}
function snapshot(c){
  const subject=c.querySelector('input[name="subjectbox"]');
  const bodies=Array.from(c.querySelectorAll('[contenteditable="true"][role="textbox"],[contenteditable="true"][g_editable="true"]')).filter(visible);
  const toInputs=Array.from(c.querySelectorAll('input[name="to"],textarea[name="to"]'));
  const toChips=Array.from(c.querySelectorAll('[email]')).filter(e=>!e.closest('[data-name="from"],[name="from"],.az2'));
  let recipients=Array.from(new Set(toInputs.flatMap(e=>addresses(e.value))));
  if(!recipients.length)recipients=Array.from(new Set(toChips.flatMap(e=>addresses(e.getAttribute('email')))));
  const pending=Array.from(c.querySelectorAll('input[role="combobox"],input[peoplekit-id]')).filter(visible).some(e=>e.value.trim());
  const extra=Array.from(c.querySelectorAll('[name="cc"],[name="bcc"],[data-name="cc"] [email],[data-name="bcc"] [email]')).some(e=>(e.value||e.getAttribute('email')||'').trim());
  // The selected From must be observed, not inferred from the signed-in user.
  const fromNodes=Array.from(c.querySelectorAll('[name="from"],[data-name="from"],.az2'));
  const from=Array.from(new Set(fromNodes.flatMap(e=>addresses(e.value||e.getAttribute('email')||e.getAttribute('data-hovercard-id')||e.textContent))));
  const nameNodes=Array.from(c.querySelectorAll('.aV3,.dL .dO,.a1 .vI,a[download]')).filter(visible);
  // Prefer leaf labels when a layout exposes both a wrapper and its filename.
  // Do not deduplicate names: two same-named attachments must fail the guard.
  const attachments=nameNodes.filter(e=>!nameNodes.some(other=>other!==e&&e.contains(other)))
    .map(e=>(e.getAttribute('download')||e.textContent||'').trim()).filter(Boolean);
  const uploading=Array.from(c.querySelectorAll('[role="progressbar"],.aVn,.dQ')).some(visible);
  const buttons=Array.from(c.querySelectorAll('[role="button"],button')).filter(e=>visible(e)&&/^(Send|Send \(.*\))$/.test((e.getAttribute('data-tooltip')||e.getAttribute('aria-label')||e.textContent||'').trim()));
  const send=buttons.length===1?buttons[0]:null;
  return {subject:subject?.value,body:bodies.length===1?norm(bodies[0].innerText):null,
    recipients,extra_recipients:extra,pending_recipient:pending,from:from.length===1?from[0]:null,
    attachments,uploading,send_ready:!!send&&!send.disabled&&send.getAttribute('aria-disabled')!=='true',_send:send};
}
function matches(s,p){
  return s.from===p.from && s.subject===p.subject && s.body===norm(p.body) &&
    !s.extra_recipients&&!s.pending_recipient && s.recipients.length===1&&s.recipients[0]===p.to &&
    !s.uploading && s.attachments.length===p.attachments.length &&
    p.attachments.every(a=>s.attachments.includes(a.name)) && s.send_ready;
}
'''

OPERATIONS = {
    'identity': r'''
return {ok:true,address:owner,compose_available:Array.from(document.querySelectorAll('[gh="cm"],[role="button"],button')).some(e=>visible(e)&&(e.getAttribute('gh')==='cm'||(e.getAttribute('aria-label')||e.textContent||'').trim()==='Compose'))};
''',
    'start': r'''
if(Array.from(document.querySelectorAll('input[name="subjectbox"]')).some(visible))return fail('existing_compose_on_owned_page');
const buttons=Array.from(document.querySelectorAll('[gh="cm"],[role="button"],button')).filter(e=>visible(e)&&(e.getAttribute('gh')==='cm'||(e.getAttribute('aria-label')||e.textContent||'').trim()==='Compose'));
const exact=buttons.filter(e=>e.getAttribute('gh')==='cm');
const options=exact.length?exact:buttons;
if(options.length!==1)return fail('gmail_compose_control_unavailable');
options[0].click();return {ok:true};
''',
    'claim': r'''
let c=compose();
if(!c){
  const subjects=Array.from(document.querySelectorAll('input[name="subjectbox"]')).filter(visible);
  if(subjects.length!==1)return {ok:true,ready:false};
  c=subjects[0].closest('[role="dialog"]');
  if(!c)return fail('gmail_compose_contract');
  c.dataset.applypilotGmailCompose=token;
}
return {ok:true,ready:true};
''',
    'focus': r'''
const c=compose();if(!c)return fail('gmail_compose_lost');
let nodes;
if(data.field==='to')nodes=Array.from(c.querySelectorAll('input[name="to"],textarea[name="to"],input[peoplekit-id],input[role="combobox"]')).filter(visible);
else if(data.field==='subject')nodes=Array.from(c.querySelectorAll('input[name="subjectbox"]')).filter(visible);
else if(data.field==='body')nodes=Array.from(c.querySelectorAll('[contenteditable="true"][role="textbox"],[contenteditable="true"][g_editable="true"]')).filter(visible);
else return fail('gmail_invalid_field');
if(nodes.length!==1)return fail('gmail_field_contract');
const e=nodes[0];e.focus();
if(e.isContentEditable){const range=document.createRange();range.selectNodeContents(e);const sel=getSelection();sel.removeAllRanges();sel.addRange(range);}
else e.select();
return {ok:true};
''',
    'reveal_from': r'''
const c=compose();if(!c)return fail('gmail_compose_lost');
// Gmail hides the From line when the recipient area is collapsed.
const fields=Array.from(c.querySelectorAll('input[name="to"],textarea[name="to"],input[peoplekit-id],input[role="combobox"]')).filter(visible);
if(fields.length===1)fields[0].focus();
const s=snapshot(c);delete s._send;return {ok:true,...s};
''',
    'stage_upload': r'''
const c=compose();if(!c)return fail('gmail_compose_lost');
const inputs=Array.from(c.querySelectorAll('input[type="file"]')).filter(e=>!e.disabled);
if(inputs.length!==1)return fail('gmail_upload_input_contract');
let stage=document.querySelector('input[data-applypilot-gmail-stage="'+token+'"]');
if(stage)return fail('gmail_upload_already_staged');
stage=document.createElement('input');stage.type='file';stage.multiple=true;stage.hidden=true;stage.dataset.applypilotGmailStage=token;
c.append(stage);return {ok:true};
''',
    'upload': r'''
const c=compose();if(!c)return fail('gmail_compose_lost');
const stage=c.querySelector('input[data-applypilot-gmail-stage="'+token+'"]');
const inputs=Array.from(c.querySelectorAll('input[type="file"]')).filter(e=>e!==stage&&!e.disabled);
if(!stage||inputs.length!==1||stage.files.length!==data.attachments.length)return fail('gmail_upload_input_contract');
const transfer=new DataTransfer();
for(let i=0;i<stage.files.length;i++){
  const f=stage.files[i],a=data.attachments[i],bytes=await f.arrayBuffer();
  const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),b=>b.toString(16).padStart(2,'0')).join('');
  if(f.size!==a.size||hash!==a.sha256)return fail('gmail_attachment_bytes_changed');
  transfer.items.add(new File([f],a.name,{type:a.name.endsWith('.pdf')?'application/pdf':'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}));
}
inputs[0].files=transfer.files;
inputs[0].dispatchEvent(new Event('input',{bubbles:true}));inputs[0].dispatchEvent(new Event('change',{bubbles:true}));
return {ok:true,verified:data.attachments.map(a=>({name:a.name,size:a.size,sha256:a.sha256}))};
''',
    'snapshot': r'''
const c=compose();if(!c)return fail('gmail_compose_lost');
const s=snapshot(c);delete s._send;return {ok:true,...s};
''',
    'arm_send': r'''
const c=compose();if(!c)return fail('gmail_compose_lost');
if(c.dataset.applypilotGmailSendAttempted)return fail('gmail_send_already_attempted');
const s=snapshot(c);if(!matches(s,data))return fail('gmail_composition_changed');
// Mark before the native act call. A missing/failed response is reconciled in
// Sent and must never lead to a second click.
c.dataset.applypilotGmailSendAttempted='true';return {ok:true};
''',
    'send': r'''
const c=compose();if(!c)return fail('gmail_compose_lost');
if(c.dataset.applypilotGmailSendAttempted)return fail('gmail_send_already_attempted');
const s=snapshot(c);
if(!matches(s,data))return fail('gmail_composition_changed');
// This marker supplements (never replaces) the PostgreSQL durable send intent.
// It is set before click. This command is never retried, including on timeout.
c.dataset.applypilotGmailSendAttempted='true';s._send.click();return {ok:true,clicked:true};
''',
    'search': r'''
const main=document.querySelector('[role="main"]');if(!main)return {ok:true,ready:false};
const rows=Array.from(main.querySelectorAll('tr.zA,[role="row"]')).filter(visible);
const ids=[];
for(const row of rows){
  const nodes=[row,...row.querySelectorAll('[data-legacy-thread-id],[data-thread-id]')];
  for(const e of nodes){let id=e.getAttribute('data-legacy-thread-id');
    if(!id){const f=/^#?thread-f:(\d+)$/.exec(e.getAttribute('data-thread-id')||'');if(f)id=BigInt(f[1]).toString(16);}
    if(id&&/^[0-9a-f]{10,32}$/i.test(id))ids.push(id.toLowerCase());}
}
const found=Array.from(new Set(ids));
const empty=/No conversations found|No results found|No messages matched/i.test(main.innerText||'');
if(found.length>10)return fail('gmail_search_ambiguous');
return {ok:true,ready:!!found.length||empty,thread_ids:found};
''',
    'thread': r'''
const main=document.querySelector('[role="main"]');if(!main)return {ok:true,ready:false};
const ids=[];
for(const e of main.querySelectorAll('[data-legacy-message-id],[data-message-id]')){
  let id=e.getAttribute('data-legacy-message-id');
  if(!id){const f=/^#?msg-f:(\d+)$/.exec(e.getAttribute('data-message-id')||'');if(f)id=BigInt(f[1]).toString(16);}
  if(id&&/^[0-9a-f]{10,32}$/i.test(id))ids.push(id.toLowerCase());
}
const found=Array.from(new Set(ids));if(found.length>20)return fail('gmail_thread_too_large');
return {ok:true,ready:!!found.length,message_ids:found};
''',
}

# Gmail's Show original view exposes the MIME text. Parse it locally in the
# owned page so attachment bytes never enter an MCP result, log or model prompt.
ORIGINAL = r'''
if(location.origin!=='https://mail.google.com'||!location.pathname.startsWith('/mail/'))return {ok:false,error:'gmail_origin_required'};
const here=new URL(location.href);
if(here.searchParams.get('view')!=='om'||here.searchParams.get('th')!==data.message_id)return {ok:false,error:'gmail_original_identity'};
const nodes=Array.from(document.querySelectorAll('#raw_message_text,.raw_message,pre'));
let raw=nodes.map(e=>e.textContent||'').find(t=>/^From:/mi.test(t)&&/^Message-ID:/mi.test(t));
if(!raw)return {ok:true,ready:false};
if(raw.length>32000000)return {ok:false,error:'gmail_original_too_large'};
raw=raw.replace(/\r\n/g,'\n');
const leaves=[];let count=0;
function headerValue(headers,name){
  const unfolded=headers.replace(/\n[ \t]+/g,' ');
  const hit=unfolded.split('\n').find(s=>s.toLowerCase().startsWith(name.toLowerCase()+':'));
  return hit?hit.slice(hit.indexOf(':')+1).trim():'';
}
async function walk(part,depth){
  if(depth>8||++count>50)throw new Error('mime_limit');
  const split=part.indexOf('\n\n');if(split<0)throw new Error('mime_headers');
  const headers=part.slice(0,split),body=part.slice(split+2),type=headerValue(headers,'content-type');
  if(/^multipart\//i.test(type)){
    const m=/boundary\s*=\s*(?:"([^"\r\n]+)"|([^;\s]+))/i.exec(type);if(!m)throw new Error('mime_boundary');
    const boundary=m[1]||m[2];if(boundary.length>200)throw new Error('mime_boundary');
    const lines=body.split('\n');let buffer=null,closed=false;
    for(const line of lines){
      if(line==='--'+boundary||line==='--'+boundary+'--'){
        if(buffer!==null)await walk(buffer.join('\n'),depth+1);
        if(line==='--'+boundary+'--'){closed=true;break;}buffer=[];
      }else if(buffer!==null)buffer.push(line);
    }
    if(!closed)throw new Error('mime_truncated');return;
  }
  const encoding=headerValue(headers,'content-transfer-encoding').toLowerCase();let bytes;
  if(encoding==='base64')bytes=Uint8Array.from(atob(body.replace(/\s/g,'')),c=>c.charCodeAt(0));
  else if(encoding==='quoted-printable'){
    const qp=body.replace(/=\n/g,'');const out=[];
    for(let i=0;i<qp.length;i++){if(qp[i]==='='&&/^[0-9a-f]{2}$/i.test(qp.slice(i+1,i+3))){out.push(parseInt(qp.slice(i+1,i+3),16));i+=2;}
      else{const n=qp.charCodeAt(i);if(n>255)throw new Error('mime_encoding');out.push(n);}}
    bytes=Uint8Array.from(out);
  }else if(!encoding||encoding==='7bit'||encoding==='8bit')bytes=new TextEncoder().encode(body);
  else throw new Error('mime_encoding');
  const disposition=headerValue(headers,'content-disposition');
  const attachment=/attachment|filename\s*=/i.test(disposition)||/;\s*name\s*=/i.test(type);
  const entry={headers,size:bytes.length};
  if(attachment)entry.sha256=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),b=>b.toString(16).padStart(2,'0')).join('');
  else if(/^text\/(plain|html)\b/i.test(type)){
    if(bytes.length>100000)throw new Error('mime_text_too_large');
    let binary='';for(const b of bytes)binary+=String.fromCharCode(b);entry.data=btoa(binary);
  }
  leaves.push(entry);
}
try{await walk(raw,0);return {ok:true,ready:true,headers:raw.slice(0,raw.indexOf('\n\n')),parts:leaves};}
catch{return {ok:false,error:'gmail_original_mime_unsupported'};}
'''


def script(operation: str, *, expected: str, token: str = '', data: dict | None = None) -> str:
    """Only named operations are executable; all owner/payload data is JSON."""
    values = 'const expected=' + json.dumps(expected) + ';const token=' + json.dumps(token) + ';const data=' + json.dumps(data or {}) + ';\n'
    if operation == 'original':
        return values + ORIGINAL
    return values + COMMON + OPERATIONS[operation]
