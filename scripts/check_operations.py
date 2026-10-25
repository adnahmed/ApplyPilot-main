"""Offline checks against disposable workspaces; no provider or model traffic."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json, logging, os
from pathlib import Path
from applypilot import postgres, subprocess, sys, tempfile, time

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo / "src"))
os.environ["PYTHONPATH"] = str(repo / "src") + os.pathsep + os.environ.get("PYTHONPATH", "")
workspace = tempfile.TemporaryDirectory(prefix='applypilot-ops-validation-')
root = Path(workspace.name)
os.environ['APPLYPILOT_DIR']=str(root)
os.environ['SAMPLE_API_KEY']='sk-never-leak-this-fixture-key'
from applypilot.operations import observe, record, scope, submit, thread_target
from applypilot.operations import store
from applypilot.operations.runtime import initialize, sanitize, result_state
from applypilot.operations.metrics import render
results=[]
def check(name, assertion):
    if not assertion:raise AssertionError(name)
    results.append(name)
    print('PASS',name,flush=True)
def drain():
    r=initialize();r.queue.join();time.sleep(.05);return r

@observe('score','job',stage='score',job_arg='job')
def scored(job, failed=False):
    if failed:raise ValueError('invalid response for alice@example.com Bearer sk-never-leak-this-fixture-key')
    return {'score':8}
@observe('company_research','company')
async def advisory():return {'status':'partial','errors':[{'kind':'network'}]}

r=initialize('validation')
with scope(cycle_id='known-cycle'):
    with ThreadPoolExecutor(max_workers=16) as pool:
        fs=[submit(pool,scored,{'url':f'https://jobs.example/job/{i}'}) for i in range(400)]
        check('concurrent wrappers preserve returned values', all(f.result()=={'score':8} for f in fs))
try:scored({'url':'https://jobs.example/private'},True)
except ValueError:pass
else:raise AssertionError('exception swallowed')
asyncio.run(advisory());drain()
x=store.overview(root)
check('journal writer no dropped events or SQL errors',r.write_errors==0 and r.dropped==0)
check('async returned partial failure is recorded', any(c['component']=='company_research' and c['outcome']=='partial' for c in x['counters']))
check('multithread completion counters are exact', any(c['stage']=='score' and c['outcome']=='success' and c['count']==400 for c in x['counters']))
events=store.events(root,{'limit':1000})['items']
check('cycle context propagates into each thread', len([e for e in events if e['cycle_id']=='known-cycle'])==800)
check('job identifiers are hashed, not URLs', all(len(e['job_id'])==24 for e in events if e['component']=='score'))
serialized=json.dumps(events)
check('exception messages redact tokens and email addresses', 'alice@example.com' not in serialized and 'sk-never-leak-this-fixture-key' not in serialized)
check('stack frames retained without locals/source text',any(e['details'].get('exception_chain',[{}])[0].get('frames') for e in events))

check('text-only cover output is not reported ready',result_state({'path':'draft.txt','pdf_path':None,'ready':False})[0]=='failed')
check('approved resume without uploadable document is a failure',result_state({'status':'approved','pdf_path':None})[0]=='failed')
check('intentional backup backoff is deferred, not success',result_state({'status':'backoff'})[0]=='deferred')
class TaskDeferred(RuntimeError): pass
from applypilot.operations import capture
capture(TaskDeferred('ordinary budget reservation'),component_name='discovery',operation='fixture_pause');drain()
check('ordinary budget deferral is not an incident',not any(i['operation']=='fixture_pause' for i in store.overview(root)['incidents']))
with ThreadPoolExecutor(max_workers=8) as pool:
    list(pool.map(lambda i:store.save_settings(root,{'retention_days':7,'max_events':200000+i}),range(16)))
check('concurrent retention saves stay valid',200000<=store.settings(root)['max_events']<200016)
store.save_settings(root,{'retention_days':7,'max_events':200000})

# All workers share one workspace, not an in-memory parent Prometheus registry.
child="""from applypilot.operations import record
from applypilot.operations.runtime import initialize
for i in range(150):record(component_name='crawl',operation='child',kind='finish',severity='info',outcome='success')
r=initialize();r.queue.join();assert r.dropped==0 and r.write_errors==0;r.close()
"""
processes=[subprocess.Popen([sys.executable,'-c',child],env=os.environ.copy()) for _ in range(3)]
check('multiple subprocess recorders exit successfully',all(p.wait(timeout=20)==0 for p in processes))
x=store.overview(root)
check('multiprocess counters aggregate exactly',any(c['component']=='crawl' and c['operation']=='child' and c['count']==450 for c in x['counters']))
check('subprocess run id is inherited',len({p['run_id'] for p in x['processes']})==1)

# Incident presentation and recurrence do not mutate metric failure counters.
for _ in range(2):record(component_name='gmail',operation='fixture',message='oauth_expired',error_type='auth')
drain();x=store.overview(root);inc=next(i for i in x['incidents'] if i['operation']=='fixture')
check('identical incidents are grouped',inc['occurrences']==2)
store.incident_action(root,{'fingerprint':inc['fingerprint'],'action':'warning'})
store.incident_action(root,{'fingerprint':inc['fingerprint'],'action':'acknowledge','last_at':inc['last_at']})
record(component_name='gmail',operation='fixture',message='oauth_expired',error_type='auth');drain()
inc=next(i for i in store.overview(root)['incidents'] if i['operation']=='fixture')
check('recurrence after acknowledgement reopens attention',inc['last_at']>inc['acknowledged_at'])
check('demotion preserves original error severity',inc['severity']=='error' and inc['display_severity']=='warning')
record(component_name='database',operation='fixture',message='database is locked',error_type='storage');drain()
inc=next(i for i in store.overview(root)['incidents'] if i['component']=='database')
try:store.incident_action(root,{'fingerprint':inc['fingerprint'],'action':'warning'})
except ValueError:pass
else:raise AssertionError('storage demotion allowed')
check('critical storage incidents cannot be demoted',True)

page=store.events(root,{'limit':3})
nextpage=store.events(root,{'limit':3,'before':page['next_before']})
check('cursor pagination does not repeat rows', not ({e['id'] for e in page['items']}&{e['id'] for e in nextpage['items']}))
check('literal search prevents SQL injection',store.events(root,{'q':"' OR 1=1 --"})['items']==[])
record(component_name='score',operation='queue',stage='score',kind='gauge',outcome='success',severity='info',details={'pending':87});drain()
text=render(root)
from prometheus_client.parser import text_string_to_metric_families
families=list(text_string_to_metric_families(text))
check('Prometheus exposition parses',bool(families))
check('metrics never use high-cardinality run/job labels',all(not(set(sample.labels)&{'run_id','job_id','task_id','span_id','message'}) for f in families for sample in f.samples))
check('stage queue gauge exported', 'applypilot_pending_jobs{stage="score"} 87.0' in text)
check('uninitialized exporter reports store unavailable','applypilot_operations_store_up 0' in render(root/'missing'))

# Protect operational endpoints using the existing loopback + session + CSRF gate.
from starlette.testclient import TestClient
from applypilot.web.server import API
api=API(root,'x'*48,8765)
with TestClient(api.application(),base_url='http://127.0.0.1:8765') as client:
    check('unauthenticated event reads denied',client.get('/api/operations').status_code==401)
    check('operations asset loads',client.get('/static/operations.js').status_code==200)
    check('existing integration UI asset is served',client.get('/static/integration_ui.js').status_code==200)
    auth=client.post('/api/session',json={'token':'x'*48});csrf=auth.json()['csrf']
    check('authenticated journal reads work',client.get('/api/operations').json()['available'])
    check('authenticated metrics endpoint works',client.get('/api/operations/metrics').status_code==200)
    check('foreign origin denied',client.get('/api/operations',headers={'Origin':'https://evil.example'}).status_code==403)
    check('mutations without CSRF denied',client.post('/api/operations/settings',json={'retention_days':7}).status_code==403)
    check('validated retention saves with CSRF',client.post('/api/operations/settings',json={'retention_days':1,'max_events':1000},headers={'x-applypilot-csrf':csrf}).status_code==200)
    check('unknown operational fields rejected',client.post('/api/operations/settings',json={'arbitrary_path':'/tmp'},headers={'x-applypilot-csrf':csrf}).status_code==400)
    check('bounded authenticated JSON export',client.get('/api/operations/export?limit=3').json()['next_before'] is not None)
if api.setup.jobs:api.setup.jobs.close()
api_ro=API(root,'y'*48,8766,True)
with TestClient(api_ro.application(),base_url='http://127.0.0.1:8766') as client:
    csrf=client.post('/api/session',json={'token':'y'*48}).json()['csrf']
    check('read-only viewer cannot change telemetry retention',client.post('/api/operations/settings',json={'retention_days':2},headers={'x-applypilot-csrf':csrf}).status_code==403)

# Retention is independent of cumulative counters and backup inventory.
from applypilot.backup.archive import allowed, inventory
check('operations journal and settings excluded from backups',not allowed('operations/events.sqlite3') and not allowed('operations/settings.json') and not any(n.startswith('operations/') for n in inventory(root)))
conn=store.connect_writer(root)
old_count=conn.execute('SELECT SUM(count) FROM counters').fetchone()[0]
store.prune(conn,root)
check('event row retention cap enforced',conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]<=1000)
check('retention does not reset metric counters',conn.execute('SELECT SUM(count) FROM counters').fetchone()[0]==old_count)
conn.close()
# Read/write failure telemetry must never change the application's return value.
original=store.append
def broken(*args):raise postgres.OperationalError('validation locked fixture')
store.append=broken
check('application still succeeds when telemetry storage fails',scored({'url':'https://jobs.example/test'})=={'score':8})
drain();store.append=original
check('telemetry losses are counted',r.dropped>0 and r.write_errors>0)
r.close()
print('ALL PASSED',len(results),flush=True)
workspace.cleanup()
