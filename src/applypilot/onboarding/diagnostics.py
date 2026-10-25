"""Bounded diagnostics for nested MCP/AnyIO failures, without echoing provider payloads."""
from __future__ import annotations


def leaves(exc):
    out=[]; seen=set()
    def walk(e, depth=0):
        if id(e) in seen or depth>8 or len(out)>=8: return
        seen.add(id(e))
        children=getattr(e,'exceptions',None)
        if isinstance(children,(list,tuple)):
            for c in children: walk(c,depth+1)
        else:
            out.append(e)
            if e.__cause__ is not None: walk(e.__cause__,depth+1)
    walk(exc)
    return out


def explain(exc, *, stderr='', component='setup'):
    """Keep a useful classification and the bounded, redacted exception chain."""
    from applypilot.operations.runtime import exception_details
    return {**_explain(exc, stderr=stderr, component=component), **exception_details(exc)}


def _explain(exc, *, stderr='', component='setup'):
    from applypilot.reasoning.budget import InferenceBudgetExceeded
    errors=leaves(exc)
    if any(isinstance(error, InferenceBudgetExceeded) for error in errors):
        return {'error': 'Profile/model work exhausted its inference budget. Review the configured call, output, or time limit before retrying.',
                'code': 'inference_budget_exhausted', 'component': component,
                'causes': list(dict.fromkeys(type(error).__name__ for error in errors)),
                'stderr_captured': bool(stderr.strip()), 'details_redacted': True}
    # Preserve trusted structured diagnostics through wrapper/AnyIO failures.
    for error in errors:
        detail = getattr(error, 'details', None)
        if type(error).__module__ in {'applypilot.backup.restic', 'applypilot.onboarding.browser_runtime',
                                     'applypilot.tracking.gmail_client', 'applypilot.tracking.gmail_transport'} and isinstance(detail, dict) and detail.get('details_redacted') is True:
            return dict(detail, component=component)
    text=(' '.join(str(e) for e in errors)+' '+stderr).lower()
    kinds=[type(e).__name__ for e in errors]
    # Use the exception type/status, not raw response bodies or credentials.
    if any(type(e).__name__ == "BadCredentialsException" or
           (type(e).__module__.startswith("github") and getattr(e, "status", None) == 401)
           for e in errors):
        return {"error": "GitHub rejected the backup credential (HTTP 401). Select GitHub CLI OAuth in "
                         "Backups and reconnect that account, or replace the selected manual token. Keep "
                         "your repository; no new recovery point is verified.",
                "code": "github_authentication_failed", "component": component,
                "causes": list(dict.fromkeys(kinds)), "stderr_captured": bool(stderr.strip()),
                "details_redacted": True}
    if any(isinstance(e,ModuleNotFoundError) for e in errors): text+=' missing python dependency'
    rules=[
      (('mailbox_identity_mismatch','missing_mailbox_identity'), 'gmail_identity',
       'The connected Gmail account does not match the saved profile, or its identity was unavailable. Verify the intended account in Setup.'),
      (('eaddrinuse','address already in use'), 'runtime_port_conflict',
       'The MCP HTTP listener collided with another process. Use the updated Gmail transport (a private dynamic port) and restart ApplyPilot.'),
      (('err_module_not_found','cannot find module','err_require_esm'), 'node_dependency',
       'The selected Node MCP installation is incomplete or incompatible. Rebuild source-tools, or reinstall the pinned Gmail MCP in the selected local runtime.'),
      (('browseros protocol mismatch','unexpected browseros result envelope'), 'browseros_protocol',
       'BrowserOS responded, but its advertised tool contract or result format is incompatible. Check that this endpoint exposes Neo run/name_session.'),
      (('missing python dependency',), 'missing_python_dependency',
       'The application Python environment lacks a required dependency. Run uv sync in this checkout, then restart the dashboard.'),
      (('required_read_capability_missing',), 'mcp_capability',
       'The selected Gmail MCP does not expose the required read tool. Verify the pinned server and its tool list.'),
      (('invalid_grant','token has been expired','token has been revoked','oauth2 credentials are invalid'), 'oauth_expired',
       'Google rejected the saved refresh token. Reconnect the same Gmail account; do not delete the client file.'),
      (('invalid_client','unauthorized_client','oauth2 client could not be created'), 'oauth_client',
       'Google rejected the OAuth client. Verify that the client JSON and refresh token belong together.'),
      (('insufficient permission','insufficientpermissions','insufficient authentication scopes','access_token_scope_insufficient'), 'oauth_scope',
       'The saved account lacks a required Gmail scope. Reconnect with the required permissions.'),
      (('winerror 2','errno 2','enoent','not found on path','cannot find the file'), 'missing_program_or_file',
       'A required executable or credential file was not found. Check the selected runtime and the detected paths.'),
      (('eacces','access is denied','permission denied'), 'file_access',
       'The subprocess cannot access a program or credential file. Check permissions and any file locks.'),
      (('econnrefused','connection refused','daemon is not running','cannot connect to the docker'), 'connection_refused',
       'The selected local service is not reachable. Check Docker Desktop and the source-tools container.'),
      (('enotfound','eai_again','getaddrinfo','certificate verify','self signed certificate'), 'network_or_certificate',
       'Name resolution or certificate validation failed. Check the network/proxy without disabling TLS verification.'),
      (('etimedout','timed out','timeout'), 'timeout',
       'The subprocess or service did not respond before its deadline. Check runtime health and network access.'),
      (('e404','npm err!','npm error'), 'npm_startup',
       'npm could not start the pinned Gmail MCP. The Docker runtime avoids downloading it on each verification.'),
      (('connection closed','endofstream','brokenresourceerror'), 'subprocess_closed',
       'The MCP subprocess closed before completing the request. Check its runtime and credential configuration.'),
    ]
    code='connection_failure';message='The operation failed; no successful connection or write is assumed.'
    if any(isinstance(e,TimeoutError) for e in errors): text+=' timeout'
    if any(isinstance(e,FileNotFoundError) for e in errors): text+=' enoent'
    for needles,code1,message1 in rules:
        if any(n in text for n in needles): code,message=code1,message1;break
    return {'error':message,'code':code,'component':component,'causes':list(dict.fromkeys(kinds)),
            'stderr_captured':bool(stderr.strip()),'details_redacted':True}
