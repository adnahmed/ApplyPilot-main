"""DOM inspection and application upload helpers for the BrowserOS bridge."""
from __future__ import annotations

import json


def _target(page: int, ref: str) -> str:
    return (
        "const target=await browser.observe(" + json.dumps(page) + ").resolveRef(" + json.dumps(ref) + ");"
        "const cdp=(method,params)=>browser.cdp(method,params,target.sessionId);"
        "const resolved=await cdp('DOM.resolveNode',{backendNodeId:target.backendNodeId});"
        "const objectId=resolved.object?.objectId;"
    )


def inspect_control_code(page: int, ref: str) -> str:
    return _target(page, ref) + r"""
if(!objectId)throw new Error('Control is no longer available; take a fresh snapshot.');
try{
  const result=await cdp('Runtime.callFunctionOn',{objectId,returnByValue:true,functionDeclaration:function(){
    const e=this.closest('button,input,a,[role="button"]')||this;
    const type=String(e.type||'').toLowerCase();
    const label=String(e.getAttribute('aria-label')||e.innerText||(['submit','button','reset'].includes(type)?e.value:'')||'').trim().slice(0,300);
    const link=e.closest('a[href]');
    const form=e.form||e.closest('form');
    const visible=n=>!!n.getClientRects().length&&getComputedStyle(n).visibility!=='hidden';
    const scope=form||e.closest('[role="dialog"]')||e.closest('main')||e.ownerDocument;
    const fields=Array.from(scope.querySelectorAll('input,textarea,select')).filter(n=>visible(n)&&!n.disabled&&!['hidden','submit','button'].includes(n.type))
      .map(n=>[n.tagName,n.type,n.name,n.id,n.getAttribute('aria-label')||''].join(':')).sort();
    const heading=Array.from(scope.querySelectorAll('h1,h2,h3,[aria-current="step"]')).filter(visible)
      .map(n=>String(n.innerText||'').trim()).filter(Boolean).join(' | ').slice(0,600);
    const form_identity=JSON.stringify({url:location.href,action:form?.action||'',id:form?.id||'',heading,fields});
    const authentication=/^(?:sign in|log in|login|verify(?: email| code)?|resend(?: code)?|send(?: code| verification email)|create account)$/i.test(label)
      ||(/\b(?:sign in|log in|login|verify (?:email|code)|create (?:an )?account)\b/i.test(heading)&&fields.length<=3);
    const activation=e.matches('button,input[type="submit"],input[type="button"],input[type="image"],a,[role="button"]');
    const submission_capable=activation&&!authentication&&((!!form&&(type==='submit'||(e.tagName==='BUTTON'&&!e.hasAttribute('type'))))
      ||/\b(?:submit|finish|complete application|send application)\b/i.test(label)
      ||(fields.length>0&&!link&&/\b(?:apply|continue|next)\b/i.test(label)));
    return {disabled:!!e.disabled||e.getAttribute('aria-disabled')==='true',label,submission_capable,
      form_identity,form_fields:fields,form_heading:heading,
      href:link?String(link.getAttribute('href')||'').trim():'',
      context:String((e.closest('p,li,[role="group"]')||e.parentElement||e).innerText||'').slice(0,1500),
      form_id:form?.id||'',page_url:location.href,page_title:document.title,
      page_heading:Array.from(document.querySelectorAll('h1')).length===1?document.querySelector('h1').innerText:'',
      local_heading:(e.closest('article,section,[itemtype$="JobPosting"]')?.querySelector('h1,h2,h3')?.innerText||'').slice(0,300),
      footer:!!e.closest('footer,nav,[role="contentinfo"],[role="navigation"]')};
  }.toString()});
  if(result.exceptionDetails||!result.result?.value)throw new Error('Cannot inspect the current control; take a fresh snapshot.');
  const control=result.result.value;
  let frameId=null;
  try{
    const tree=await cdp('Page.getFrameTree',{}),pending=[tree.frameTree],matches=[];
    while(pending.length){const item=pending.pop();if(!item)continue;
      if(item.frame&&(item.frame.url+(item.frame.urlFragment||''))===control.page_url)matches.push(item.frame.id);
      pending.push(...(item.childFrames||[]));}
    if(matches.length===1)frameId=matches[0];
  }catch{}
  return {...control,submit_target:{backend_node_id:target.backendNodeId,session_id:target.sessionId,
    frame_id:frameId,label:control.label,form_id:control.form_id}};
}finally{await cdp('Runtime.releaseObject',{objectId});}
"""


def application_forms_code(page: int, *, target: dict) -> str:
    """Read the prepared control's frame; never switch to a different document."""
    if not isinstance(target.get("session_id"), str) or not isinstance(target.get("frame_id"), str):
        raise ValueError("The prepared application frame could not be identified.")
    expression = r"""(()=>{
      const visible=n=>!!n.getClientRects().length&&getComputedStyle(n).visibility!=='hidden';
      const forms=Array.from(document.querySelectorAll('form,[role="dialog"]')).filter(visible);
      if(!forms.length)forms.push(document.querySelector('main')||document);
      return {url:location.href,pending:Array.from(document.querySelectorAll('[aria-busy="true"],[role="progressbar"]:not([aria-valuenow])')).some(visible),
        forms:forms.map(form=>{
          const fields=Array.from(form.querySelectorAll('input,textarea,select')).filter(n=>visible(n)&&!n.disabled&&!['hidden','submit','button'].includes(n.type))
            .map(n=>[n.tagName,n.type,n.name,n.id,n.getAttribute('aria-label')||''].join(':')).sort();
          const heading=Array.from(form.querySelectorAll('h1,h2,h3,[aria-current="step"]')).filter(visible)
            .map(n=>String(n.innerText||'').trim()).filter(Boolean).join(' | ').slice(0,600);
          const controls=Array.from(form.querySelectorAll('button,input[type="submit"],[role="button"]')).filter(n=>visible(n)&&!n.disabled)
            .map(n=>String(n.getAttribute('aria-label')||n.innerText||n.value||'').trim().slice(0,150));
          return {identity:JSON.stringify({url:location.href,action:form.action||'',id:form.id||'',heading,fields}),fields,heading,controls};
        })};
    })()"""
    return ("const target=" + json.dumps(target) + ";"
            "const cdp=(method,params)=>browser.cdp(method,params,target.session_id);"
            "const tree=await cdp('Page.getFrameTree',{}),pending=[tree.frameTree];let found=false;"
            "while(pending.length){const item=pending.pop();if(!item)continue;"
            "if(item.frame?.id===target.frame_id)found=true;pending.push(...(item.childFrames||[]));}"
            "if(!found)throw new Error('The prepared application frame is no longer available');"
            "const world=await cdp('Page.createIsolatedWorld',{frameId:target.frame_id,worldName:'applypilot-form-inspection'});"
            "const r=await cdp('Runtime.evaluate',{contextId:world.executionContextId,expression:" + json.dumps(expression)
            + ",returnByValue:true});if(r.exceptionDetails)throw new Error('Cannot inspect application steps');return r.result.value;")


def intermediate_form_transition(prepared: dict, observed: dict) -> dict | None:
    """A changed page alone never proves that a final submission did not happen."""
    import re
    from urllib.parse import urlsplit

    previous = prepared.get("form_identity")
    if not previous or observed.get("pending") is not False:
        return None
    try:
        before = json.loads(previous)
        old_url, new_url = urlsplit(before["url"]), urlsplit(observed["url"])
        if (old_url.scheme != "https" or not old_url.netloc
                or (old_url.scheme, old_url.netloc) != (new_url.scheme, new_url.netloc)):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    forms = observed.get("forms") or []
    if any(form.get("identity") == previous for form in forms):
        return None
    intermediate_label = bool(re.fullmatch(r"(?:next(?: step)?|continue|save (?:and|&) continue|proceed|review(?: (?:your )?application)?)",
                                          str(prepared.get("label") or "").strip(), re.I))
    def step(value):
        match = re.search(r"\bstep\s+(\d+)\s*(?:of|/)\s*(\d+)\b", str(value or ""), re.I)
        return tuple(map(int, match.groups())) if match else None

    previous_step = step(before.get("heading"))
    for form in forms:
        try:
            current = json.loads(form.get("identity", ""))
        except (ValueError, TypeError):
            continue
        if not isinstance(current, dict) or current.get("url") != observed["url"] or current == before:
            continue
        fields = current.get("fields") or []
        heading = str(current.get("heading") or "")
        controls = form.get("controls") or []
        if not isinstance(controls, list) or any(not isinstance(label, str) for label in controls):
            continue
        if re.search(r"\b(?:feedback|survey|optional|interview|assessment)\b|\b(?:another|new|different) application\b", heading, re.I):
            continue
        current_step = step(heading)
        advancing_step = bool(previous_step and current_step
                              and previous_step[1] == current_step[1]
                              and 1 <= previous_step[0] < current_step[0] <= current_step[1])
        review = bool(re.search(r"\b(?:review (?:your |the )?application|application review|review (?:and|&) submit)\b", heading, re.I))
        final_control = any(re.fullmatch(r"(?:submit(?: (?:my |your )?application)?|send application|finish|complete application)", label.strip(), re.I)
                            for label in controls)
        has_next = any(re.search(r"\b(?:submit|next|continue|finish)\b", label, re.I) for label in controls)
        if has_next and ((fields and (advancing_step or intermediate_label and fields != before.get("fields")))
                         or (intermediate_label and review and final_control)):
            return {"advanced": True, "pending": False, "completion_visible": False,
                    "previous_form": previous, "current_form": form["identity"],
                    "current_controls": controls, "page_url": observed["url"],
                    "step_evidence": heading or prepared.get("label")}
    return None


def submission_rejection_code(page: int, ref: str, *, target: dict | None = None) -> str:
    """Observe explicit validation on the still-enabled submitted form."""
    if (isinstance(target, dict) and type(target.get("backend_node_id")) is int
            and isinstance(target.get("session_id"), str)):
        replacement = (
            "(()=>{const expected=" + json.dumps({"label": target.get("label", ""), "form_id": target.get("form_id", "")}) + ";"
            "const norm=value=>String(value||'').replace(/\\s+/g,' ').trim().toLowerCase();"
            "if(!norm(expected.label))return null;"
            "const scope=(expected.form_id&&document.getElementById(expected.form_id))||document;"
            "const matches=Array.from(scope.querySelectorAll('button,input[type=submit],input[type=button],[role=button]'))"
            ".filter(e=>e.isConnected&&e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden'"
            "&&!e.disabled&&e.getAttribute('aria-disabled')!=='true')"
            ".filter(e=>norm(e.getAttribute('aria-label')||e.innerText||e.value)===norm(expected.label));"
            "return matches.length===1?matches[0]:null;})()"
        )
        setup = (
            "const target=" + json.dumps(target) + ";"
            "let cdp=(method,params)=>browser.cdp(method,params,target.session_id);let objectId=null;"
            "try{const resolved=await cdp('DOM.resolveNode',{backendNodeId:target.backend_node_id});"
            "objectId=resolved.object?.objectId||null;"
            "if(objectId){const live=await cdp('Runtime.callFunctionOn',{objectId,returnByValue:true,"
            "functionDeclaration:'function(){return this.isConnected;}'});"
            "if(live.result?.value!==true){await cdp('Runtime.releaseObject',{objectId});objectId=null;}}}catch{objectId=null;}"
            "if(!objectId){const params={expression:" + json.dumps(replacement) + ",returnByValue:false};let replacement;"
            "try{replacement=await cdp('Runtime.evaluate',params);}catch{"
            "cdp=(method,params)=>browser.cdpJsonForPage(" + json.dumps(page) + ",method,JSON.stringify(params));"
            "replacement=await cdp('Runtime.evaluate',params);}objectId=replacement.result?.objectId||null;}"
        )
    else:
        setup = _target(page, ref)
    return setup + r"""
if(!objectId)return {rejected:false};
try{
 const result=await cdp('Runtime.callFunctionOn',{objectId,returnByValue:true,functionDeclaration:function(){
   const e=this.closest('button,input')||this,form=e.form||e.closest('form');
   const scope=form||e.closest('[role="dialog"]')||e.ownerDocument;
   const visible=n=>!!n.getClientRects().length&&getComputedStyle(n).visibility!=='hidden';
   if(!scope||!e.isConnected||!visible(e)||e.disabled||e.getAttribute('aria-disabled')==='true')return {rejected:false};
   const pending=Array.from(scope.querySelectorAll('[aria-busy="true"],[role="progressbar"]:not([aria-valuenow]),.loading-spinner')).some(visible);
   const invalid=Array.from(scope.querySelectorAll('input,select,textarea,[aria-invalid="true"]'))
     .filter(n=>visible(n)&&!n.disabled&&(n.getAttribute('aria-invalid')==='true'||(n.willValidate&&n.validity?.valid===false)))
     .filter(n=>!/one-time-code|\botp\b|(?:verification|security|authentication)[\s_-]*code/i.test([n.autocomplete,n.name,n.id,n.getAttribute('aria-label')].join(' ')));
   const fields=invalid.slice(0,30).map(n=>({
     name:String(n.name||n.id||n.getAttribute('aria-label')||n.tagName).slice(0,120),
     reason:String(n.validationMessage||n.getAttribute('aria-errormessage')||'invalid_field').slice(0,300)
   }));
   const messages=Array.from(scope.querySelectorAll('[role="alert"],[aria-live],.field-error,.error-message,.invalid-feedback'))
     .filter(visible).map(n=>String(n.innerText||'').trim().slice(0,500))
     .filter(text=>/\brequired\b|\binvalid\b|\bplease (?:select|enter|choose|provide|complete)\b|\bmust (?:select|enter|choose|provide|complete)\b/i.test(text)).slice(0,20);
   return {rejected:!pending&&!!(fields.length||messages.length),pending,page_url:location.href,fields,messages};
 }.toString()});
 if(result.exceptionDetails||!result.result?.value)return {rejected:false};
 return result.result.value;
}finally{await cdp('Runtime.releaseObject',{objectId});}
"""


def inspect_email_links_code(page: int) -> str:
    """Read application email candidates without activating a protocol handler."""
    expression = r"""JSON.stringify({page_url:location.href,page_title:document.title,
      page_heading:Array.from(document.querySelectorAll('h1')).length===1?document.querySelector('h1').innerText:'',
      links:Array.from(document.querySelectorAll('a[href]'))
      .filter(e=>/^mailto:/i.test(e.getAttribute('href').trim()) && (e.getClientRects().length || e.closest('details:not([open])')))
      .slice(0,50).map(e=>({href:e.getAttribute('href').trim(),
        label:String(e.getAttribute('aria-label')||e.innerText||'').trim().slice(0,300),
        context:String((e.closest('p,li,[role="group"]')||e.parentElement||e).innerText||'').slice(0,1500),
        local_heading:(e.closest('article,section,[itemtype$="JobPosting"]')?.querySelector('h1,h2,h3')?.innerText||'').slice(0,300),
        footer:!!e.closest('footer,nav,[role="contentinfo"],[role="navigation"]')}))})"""
    return (
        "const result=await browser.cdpJsonForPage(" + json.dumps(page)
        + ",'Runtime.evaluate',JSON.stringify({expression:" + json.dumps(expression)
        + ",returnByValue:true}));if(result.exceptionDetails)throw new Error('Cannot inspect application links');"
        + "return JSON.parse(result.result.value);"
    )


def is_application_email(control: dict) -> bool:
    """An email address alone does not establish an invitation to apply."""
    import re
    if control.get("footer") or not str(control.get("href", "")).lower().startswith("mailto:"):
        return False
    label = str(control.get("label", ""))
    context = str(control.get("context", ""))
    support = r"\b(?:questions?|inquir\w*|support|assistance|accommodations?|accessibility)\b"
    if re.search(support, label, re.I):
        return False
    if re.search(r"\b(?:do not|don't|not accepting|no unsolicited)\b.{0,45}\b(?:send|email|resume|cv|applications?)\b", context, re.I):
        return False
    positive_label = re.search(r"^\s*apply\b|\b(?:send|email|submit)\b.{0,45}\b(?:resume|cv|cover letter|application)\b", label, re.I)
    # Bind context instructions to THIS recipient's sentence. A separate form
    # invitation elsewhere in the paragraph cannot authorize a support address.
    position = context.casefold().find(label.casefold()) if label else -1
    if position < 0:
        return bool(positive_label)
    before = re.split(r"[.!?]\s+|\n", context[:position])[-1]
    after = re.split(r"[.!?]\s+|\n", context[position + len(label):])[0]
    clause = before + label + after
    if re.search(support, clause, re.I):
        return False
    return bool(positive_label or re.search(
        r"\bto apply\b.{0,65}\b(?:email|send)\b|\b(?:send|email|submit)\b.{0,65}\b(?:resume|cv|application)\b",
        clause, re.I))


def email_link_matches_posting(control: dict, title: str) -> bool:
    """Bind an invitation to a role card or a page about one specific role."""
    import re
    def words(value):
        return " ".join(re.findall(r"\w+", str(value or "").casefold()))
    expected = words(title)
    if not expected:
        return False
    heading = words(control.get("local_heading"))
    generic = re.fullmatch(r"(?:how to |ready to )?apply(?: now)?|application(?: process)?|interested|join us|send (?:your )?(?:resume|cv)", heading)
    if heading and expected != heading and not generic:
        return False
    explicit_role = re.fullmatch(r"apply for (.+)", words(control.get("label")))
    if explicit_role and explicit_role[1] not in {expected, "this role", "this job", "the role", "the job"}:
        return False
    titles = [control.get("local_heading"), control.get("page_heading")]
    titles.extend(re.split(r"\s+[|\u2014\u2013]\s+", str(control.get("page_title") or ""))[:1])
    titles.extend(str(control.get("context") or "").splitlines())
    return expected in {words(value) for value in titles}


INSPECT_UPLOADS = """
const tree=await browser.cdpJsonForPage(PAGE,"DOM.getDocument",JSON.stringify({depth:-1,pierce:true}));
const pending=[tree.root], inputs=[];
while(pending.length && inputs.length<100){
  const n=pending.pop(); if(!n)continue;
  const a={}; for(let i=0;i<(n.attributes||[]).length;i+=2)a[n.attributes[i]]=n.attributes[i+1];
  if(n.nodeName==='INPUT' && String(a.type||'').toLowerCase()==='file'){
    let hasFile=false;
    const resolved=await browser.cdpJsonForPage(PAGE,'DOM.resolveNode',JSON.stringify({backendNodeId:n.backendNodeId}));
    const objectId=resolved.object?.objectId;
    if(objectId){
      try{
        const state=await browser.cdpJsonForPage(PAGE,'Runtime.callFunctionOn',JSON.stringify({objectId,returnByValue:true,
          functionDeclaration:'function(){return !!this.files?.length;}'}));
        hasFile=!state.exceptionDetails && state.result?.value===true;
      }finally{await browser.cdpJsonForPage(PAGE,'Runtime.releaseObject',JSON.stringify({objectId}));}
    }
    inputs.push({backend_node_id:n.backendNodeId,id:(a.id||'').slice(0,200),
      name:(a.name||'').slice(0,200),label:(a['aria-label']||'').slice(0,300),
      accept:(a.accept||'').slice(0,200),required:('required' in a)||a['aria-required']==='true',
      disabled:'disabled' in a,has_file:hasFile});
  }
  pending.push(...(n.children||[]),...(n.shadowRoots||[]));
  if(n.contentDocument)pending.push(n.contentDocument);
}
return {inputs};
"""


def inspect_uploads_code(page: int) -> str:
    return INSPECT_UPLOADS.replace("PAGE", json.dumps(page))


def upload_code(page: int, *, ref: str | None, backend_node_id: int | None, path: str = "", attach: bool = True) -> str:
    """Resolve a real input, optionally attach a file, and read its byte hash."""
    if ref is not None:
        setup = ("const target=await browser.observe(" + json.dumps(page) + ").resolveRef("
                 + json.dumps(ref) + ");const node=target.backendNodeId;"
                 "const cdp=(method,params)=>browser.cdp(method,params,target.sessionId);")
    else:
        setup = ("const node=" + json.dumps(backend_node_id) + ";"
                 "const cdp=(method,params)=>browser.cdpJsonForPage(" + json.dumps(page)
                 + ",method,JSON.stringify(params));")
    return setup + """
const described=await cdp('DOM.describeNode',{backendNodeId:node});
const n=described.node,a={}; for(let i=0;i<(n?.attributes||[]).length;i+=2)a[n.attributes[i]]=n.attributes[i+1];
if(n?.nodeName!=='INPUT'||String(a.type||'').toLowerCase()!=='file')
  throw new Error('Upload target is not a file input element. Use inspect_application_uploads.');
ATTACH_FILE
const resolved=await cdp('DOM.resolveNode',{backendNodeId:node});
const objectId=resolved.object?.objectId;
if(!objectId)throw new Error('Upload completed but file input metadata is unavailable.');
try{
  const checked=await cdp('Runtime.callFunctionOn',{objectId,awaitPromise:true,returnByValue:true,
    functionDeclaration: "async function(){const f=this.files?.[0];if(this.files?.length!==1||!f)return {verified:false};const hash=await crypto.subtle.digest('SHA-256',await f.arrayBuffer());return {verified:true,name:f.name,size:f.size,sha256:Array.from(new Uint8Array(hash),b=>b.toString(16).padStart(2,'0')).join('')};}"});
  if(checked.exceptionDetails)throw new Error('Upload completed but file byte verification failed.');
  return {backend_node_id:node,...checked.result?.value};
}finally{await cdp('Runtime.releaseObject',{objectId});}
""".replace("ATTACH_FILE", "if('disabled' in a)throw new Error('Upload file input is disabled.');"
            "await cdp('DOM.setFileInputFiles',{backendNodeId:node,files:"
            + json.dumps([path]) + "});" if attach else "")
