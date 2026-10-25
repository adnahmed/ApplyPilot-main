"""Offline behavioral checks. Uses temporary workspaces and fake GitHub/browser boundaries."""
import os,sys,tempfile,json,base64,types,contextlib,tomllib
from pathlib import Path
from unittest.mock import patch
ROOT=Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'));sys.dont_write_bytecode=True
passed=[]
def check(name,condition):
 if not condition:raise AssertionError(name)
 passed.append(name)
def expect(kind,fn):
 try:fn()
 except kind as exc:return exc
 raise AssertionError('Expected '+kind.__name__)
with tempfile.TemporaryDirectory(prefix='main7-offline-') as d:
 root=Path(d);os.environ['APPLYPILOT_DIR']=d
 from applypilot.integrations import github_auth as auth
 from applypilot.backup import common,service,github
 from applypilot.onboarding import github_connect as connect,sessions,diagnostics
 from applypilot.onboarding.storage import write_json,read_json
 cfg=common.DEFAULTS|{'repository':'owner/archive','github_account':'owner','auth_mode':'github_cli'}
 write_json(common.folder(root)/'config.json',cfg)
 write_json(root/'setup/github-auth.json',{'login':'owner'})
 (root/'.env').write_text("APPLYPILOT_BACKUP_TOKEN='stale-test-token'\nAPPLYPILOT_BACKUP_KEY='"+base64.urlsafe_b64encode(b'k'*32).decode()+"'\n")
 with patch.object(auth,'saved_token',return_value='fresh-cli-token') as saved:
  check('CLI mode ignores a stale APPLYPILOT_BACKUP_TOKEN',auth.backup_token(root)=='fresh-cli-token')
  check('CLI mode pins saved account',saved.call_args.kwargs['account']=='owner')
 with patch.object(auth,'saved_token',return_value=''):
  check('Missing selected CLI credential never falls back to PAT',auth.backup_token(root)=='')
 with patch.object(auth,'saved_token',return_value='fresh-cli-token'):
  check('Auto mode prefers owner-connected CLI identity',auth.backup_token(root,cfg|{'auth_mode':'auto','github_account':''})=='fresh-cli-token')
 with patch.object(auth,'saved_token',side_effect=AssertionError('PAT must not use CLI')):
  check('Token mode uses only the manual token',auth.backup_token(root,cfg|{'auth_mode':'token'})=='stale-test-token')
 (root/'setup/github-auth.json').unlink()
 with patch.object(auth,'saved_token',side_effect=AssertionError('Unconnected auto uses explicit PAT')):
  check('Legacy unconnected auto can use explicit PAT',auth.backup_token(root,cfg|{'auth_mode':'auto','github_account':''})=='stale-test-token')
 write_json(root/'setup/github-auth.json',{'login':'owner'})
 selected=auth.backup_auth_status(root)
 check('Safe auth status explains ignored token',selected['token_ignored'] and 'stale-test-token' not in json.dumps(selected))
 clean=auth.cli_env({'GH_TOKEN':'x','GITHUB_TOKEN':'x','GH_DEBUG':'api','DEBUG':'1','PATH':'ok'})
 check('CLI child environment strips token overrides and HTTP debug',not any(k in clean for k in ['GH_TOKEN','GITHUB_TOKEN','GH_DEBUG','DEBUG']) and clean['PATH']=='ok')
 with patch.object(auth.shutil,'which',return_value='/fake/gh'),patch.object(auth.subprocess,'run',return_value=types.SimpleNamespace(returncode=0,stdout='chosen-token\n')) as run:
  check('Keychain token read returns bounded captured value',auth.saved_token(account='owner')=='chosen-token')
  argv=run.call_args.args[0]
  check('Keychain command pins github.com and user without a shell',argv==['/fake/gh','auth','token','--hostname','github.com','--user','owner'] and not run.call_args.kwargs.get('shell'))

 class BadCredentialsException(Exception):
  status=401
 class FakeClient:
  login='owner';private=True;archived=False;push=True;reject=False;created=[]
  def __init__(self,**kwargs):
   self.kwargs=kwargs;self.calls=[];self.closed=False;self.__class__.created.append(self)
  def get_user(self):
   self.calls.append('get_user')
   if self.reject:raise BadCredentialsException('FAKE_SECRET_UPSTREAM_BODY')
   return types.SimpleNamespace(login=self.login)
  def get_repo(self,name):
   self.calls.append('get_repo')
   return types.SimpleNamespace(private=self.private,archived=self.archived,permissions=types.SimpleNamespace(push=self.push))
  def close(self):self.closed=True
 fake=types.ModuleType('github');fake.Auth=types.SimpleNamespace(Token=lambda t:('token',t));fake.Github=FakeClient
 with patch.dict(sys.modules,{'github':fake}),patch.object(auth,'saved_token',return_value='fresh-cli-token'):
  r=github.Remote(root,'owner/archive');r.require_write_access();r.close()
  check('Remote uses CLI token and pinned account',r.token=='fresh-cli-token' and r.login=='owner' and r.auth_source=='github_cli')
  check('Remote performs only identity/repository reads before approval',r.client.calls==['get_user','get_repo'] and r.client.closed)
  with patch.object(FakeClient,'reject',True):
   exc=expect(auth.GitHubCredentialError,lambda:github.Remote(root,'owner/archive'))
   check('401 identifies auth failure without secret body or credential fallback',exc.details['code']=='github_authentication_failed' and 'FAKE_SECRET' not in str(exc) and FakeClient.created[-1].closed)
  with patch.object(FakeClient,'login','another-owner'):
   expect(auth.GitHubCredentialError,lambda:github.Remote(root,'owner/archive'))
   check('Different account rejected before reading destination',FakeClient.created[-1].calls==['get_user'])
  with patch.object(FakeClient,'private',False):
   expect(ValueError,lambda:github.Remote(root,'owner/archive'))
   check('Public repository still rejected',FakeClient.created[-1].closed)
  with patch.object(FakeClient,'push',False):
   rr=github.Remote(root,'owner/archive');expect(ValueError,rr.require_write_access);rr.close()
   check('Known read-only repository permission stops writes',rr.client.closed)
  result=service.configure(root,{'repository':'owner/archive','enabled':True,'auth_mode':'github_cli','confirm_private_upload':True})
  check('Authorize verifies source and persists account',result['config']['enabled'] and result['config']['github_account']=='owner' and result['auth']['source']=='github_cli')
  before=(common.folder(root)/'config.json').read_bytes()
  with patch.object(FakeClient,'reject',True):
   expect(auth.GitHubCredentialError,lambda:service.configure(root,{'enabled':True,'confirm_private_upload':True}))
  check('Rejected authorization preserves prior configuration',before==(common.folder(root)/'config.json').read_bytes())
  expect(ValueError,lambda:service.configure(root,{'enabled':True,'confirm_private_upload':False}))
  check('Private-data consent remains required',before==(common.folder(root)/'config.json').read_bytes())
  with patch.object(auth,'saved_token',side_effect=AssertionError('Status must not hit credential store')):
   report=service.status(root)
  check('Status polling does not query credential store',report['auth']['token_ignored'])
  # Pinning identity during auto/PAT authorization must not change the next source.
  manual_root=root/'legacy-manual';manual_root.mkdir()
  (manual_root/'.env').write_bytes((root/'.env').read_bytes())
  with patch.object(auth,'saved_token',side_effect=AssertionError('Manual authorization must never use CLI')):
   manual=service.configure(manual_root,{'repository':'owner/archive','enabled':True,'auth_mode':'auto','confirm_private_upload':True})
   check('Auto authorization freezes verified manual source',manual['config']['auth_mode']=='token' and manual['config']['github_account']=='owner')
   check('Pinned manual source is stable on subsequent backup requests',auth.backup_token(manual_root)=='stale-test-token')
 with patch.object(connect,'require_stopped'),patch.object(connect,'saved_token',side_effect=AssertionError('Existing repo must not require login')):
  result=connect.create_backup_repository(root,{'confirm':True})
 check('Repeated repo creation is idempotent and preserves authorization',result['already_configured'] and not result['created'] and not result['uploaded'] and result['enabled'] and before==(common.folder(root)/'config.json').read_bytes())
 detail=diagnostics.explain(BadCredentialsException('FAKE_SECRET_UPSTREAM_BODY'),component='backup_sync')
 check('Late PyGithub 401 is actionable and redacted',detail['code']=='github_authentication_failed' and 'FAKE_SECRET' not in json.dumps(detail))

 class Page:
  url='https://x.com/home'
  def goto(self,*args,**kwargs):pass
  def is_closed(self):return False
 class Context:
  def __init__(self):self.pages=[Page()];self.closed=False
  def cookies(self):return [{'name':'auth_token','value':'x-token','domain':'.x.com'}, {'name':'ct0','value':'x-csrf','domain':'.x.com'}, {'name':'google-secret','value':'never-export','domain':'.google.com'}]
  def close(self):self.closed=True
 launches=[]
 def launch(path,**kwargs):
  c=Context();launches.append((path,kwargs,c));return c
 cloak=types.ModuleType('cloakbrowser');cloak.launch_persistent_context=launch
 with patch.dict(sys.modules,{'cloakbrowser':cloak}):
  with sessions._login_context(root,'x') as browser:pass
  check('CloakBrowser is headed and uses stable private profile',launches[-1][0].endswith('/x-cloakbrowser') and launches[-1][1]=={'headless':False} and browser.closed)
  with sessions._login_context(root,'x') as again:pass
  check('CloakBrowser profile survives repeated launches',launches[0][0]==launches[1][0])
  expect(ValueError,lambda:sessions._login_context(root,'x','chrome').__enter__())
  check('Login context cannot launch Chrome',len(launches)==2)
  with patch.object(sessions,'require_stopped'),patch.object(sessions,'_save_cookies',return_value={'saved':True}) as save:
   result=sessions.connect(root,{'provider':'twikit','consent':True,'browser_backend':'chrome'})
   check('Legacy Chrome request is normalized to CloakBrowser',result['saved'] and len(launches)==3)
   check('Only authorized platform cookies are exported',save.call_args.args[2]=={'auth_token':'x-token','ct0':'x-csrf'})
   expect(ValueError,lambda:sessions.connect(root,{'provider':'twikit','consent':False}))
   check('Social consent required before launching browser',len(launches)==3)
 with patch.dict(sys.modules,{'cloakbrowser':None}):
  exc=expect(ValueError,lambda:sessions._login_context(root,'x').__enter__())
  check('Incomplete install has upgrade instruction, no optional-browser fallback','required dependency' in str(exc) and 'No Chrome fallback' in str(exc))
 package=tomllib.loads((ROOT/'pyproject.toml').read_text())
 check('Default dependencies require CloakBrowser','cloakbrowser==0.5.6' in package['project']['dependencies'])
 check('PostgreSQL driver and process owner declared', 'pgembed==0.2.0' in package['project']['dependencies'] and any(d.startswith('psycopg[binary]') for d in package['project']['dependencies']))
 print('\n'.join('PASS: '+name for name in passed));print(f'\n{len(passed)} offline behavioral checks passed. GitHub/browser boundaries were simulated, not live.')
