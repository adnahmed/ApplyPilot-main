"""Offline contracts only. NOT a Temporal SDK, PostgreSQL, replay or browser test.

Run: python tools/temporal/validate_offline.py --report /tmp/temporal-offline.json
Uses explicit SDK doubles and an in-memory SQLite transaction double. No network,
user workspace, external applications, or production database is contacted.
"""
from __future__ import annotations
import argparse
import ast
import asyncio
from contextlib import contextmanager
from dataclasses import asdict
from datetime import timedelta
import importlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = tempfile.TemporaryDirectory(prefix="applypilot-temporal-offline-")
os.environ["APPLYPILOT_DIR"] = WORKSPACE.name
sys.path.insert(0, str(ROOT / "src"))

# These doubles deliberately do NOT simulate Temporal persistence/matching.
def decorate(value=None, **kw):
    return value if value is not None else lambda fn: fn
class ApplicationError(Exception):
    def __init__(self, message, *, type=None, non_retryable=False, next_retry_delay=None):
        super().__init__(message); self.type = type; self.non_retryable = non_retryable; self.next_retry_delay = next_retry_delay
class ActivityError(Exception): pass
class CancelledError(Exception): pass
class WorkflowAlreadyStartedError(Exception): pass
class ContinueAsNew(BaseException):
    def __init__(self, command): self.command = command
class RetryPolicy:
    def __init__(self, **kw): self.__dict__.update(kw)
sdk = types.ModuleType("temporalio"); sdk.__path__ = []
wf = types.ModuleType("temporalio.workflow")
wf.defn = wf.run = wf.query = decorate
wf.ParentClosePolicy = types.SimpleNamespace(ABANDON="ABANDON")
wf.info = lambda: types.SimpleNamespace(workflow_id="ap:test:job:opaque", is_continue_as_new_suggested=lambda: False)
wf.uuid4 = lambda: types.SimpleNamespace(hex="stable-sweep")
wf.continue_as_new = lambda c: (_ for _ in ()).throw(ContinueAsNew(c))
wf.execute_activity = wf.execute_child_workflow = wf.start_child_workflow = None
wf.sleep = asyncio.sleep
common = types.ModuleType("temporalio.common")
common.RetryPolicy = RetryPolicy
common.WorkflowIDReusePolicy = types.SimpleNamespace(ALLOW_DUPLICATE="ALLOW_DUPLICATE",REJECT_DUPLICATE="REJECT_DUPLICATE")
errors = types.ModuleType("temporalio.exceptions")
for item in (ApplicationError, ActivityError, CancelledError, WorkflowAlreadyStartedError):
    setattr(errors, item.__name__, item)
activity = types.ModuleType("temporalio.activity"); activity.defn = decorate
sdk.workflow = wf; sdk.activity = activity
sys.modules.update({"temporalio": sdk, "temporalio.workflow": wf, "temporalio.common": common,
    "temporalio.exceptions": errors, "temporalio.activity": activity})
from applypilot.orchestration import contracts, settings, store, workflows, activities, domain, observability
from applypilot import execution
from pydantic import ValidationError
import yaml

def policy():
    return settings.load(Path(WORKSPACE.name)).model_dump(mode="json")

def connection():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store.initialize(conn)
    return conn

@contextmanager
def fake_database(conn):
    mod = types.ModuleType("applypilot.database")
    mod.get_connection = lambda: conn
    mod.close_connection = lambda: None
    with patch.dict(sys.modules, {"applypilot.database": mod}):
        yield mod

class IdentityContracts(unittest.TestCase):
    def test_canonical_digest(self):
        self.assertEqual(contracts.digest({"a": 1, "b": 2}), contracts.digest({"b": 2, "a": 1}))
    def test_nan_rejected(self):
        with self.assertRaises(ValueError): contracts.digest(float("nan"))
    def test_invalid_workspace(self):
        for value in ("", "a/b", "' OR 1=1", "x" * 65):
            with self.subTest(value=value), self.assertRaises(ValueError): contracts.workspace_id(value)
    def test_opaque_semantic_identity(self):
        value = contracts.identity("test", "job", contracts.digest("https://private.example/job?token=secret"))
        self.assertTrue(value.startswith("ap:test:job:")); self.assertNotIn("secret", value)
        self.assertLess(len(value), 255)
    def test_role_queues_distinct(self):
        self.assertEqual(len(contracts.ROLES), len({contracts.task_queue("ap", "test", role) for role in contracts.ROLES}))
    def test_invalid_role(self):
        with self.assertRaises(ValueError): contracts.task_queue("ap", "test", "shell")
    def test_json_wire_roundtrip(self):
        cmd = contracts.Command("a" * 64, policy())
        self.assertEqual(cmd, contracts.Command(**json.loads(json.dumps(asdict(cmd)))))

class PolicyContracts(unittest.TestCase):
    def test_complete_default(self):
        p = settings.load(Path(WORKSPACE.name))
        self.assertEqual(set(contracts.ROLES), set(p.workers))
        self.assertEqual(set(contracts.SERVICE_PHASES) | {"jobs", "discover"}, set(p.schedules))
    def test_unknown_key_rejected(self):
        with self.assertRaises(ValidationError): settings.TemporalPolicy.model_validate({**policy(), "budget": 3})
    def test_missing_role_rejected(self):
        p=policy(); del p["workers"]["learning"]
        with self.assertRaises(ValidationError): settings.TemporalPolicy.model_validate(p)
    def test_invalid_dependency_order(self):
        p=policy(); p["job_phases"]=["cover", "enrich"]
        with self.assertRaises(ValidationError): settings.TemporalPolicy.model_validate(p)
    def test_interval_relation(self):
        with self.assertRaises(ValidationError): settings.ActivityPolicy(start_to_close_seconds=120, heartbeat_seconds=60, heartbeat_interval_seconds=30)
    def test_invalid_number_rejected(self):
        for value in (0, -1, float("inf")):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                settings.ActivityPolicy(start_to_close_seconds=value)
    def test_attempts_are_native_and_queues_untimed(self):
        opts=workflows.activity_options(policy(), "score")
        self.assertEqual(opts["retry_policy"].maximum_attempts, 0)
        self.assertNotIn("schedule_to_start_timeout", opts)
        self.assertNotIn("schedule_to_close_timeout", opts)
        self.assertEqual(workflows.activity_options(policy(), "application_attempt", once=True)["retry_policy"].maximum_attempts, 1)
    def test_partial_central_override(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "runtime_policy.yaml").write_text("temporal:\n  page_size: 17\n", encoding="utf-8")
            self.assertEqual(settings.load(Path(d)).page_size, 17)

class StoreContracts(unittest.TestCase):
    def setUp(self): self.conn=connection()
    def tearDown(self): self.conn.close()
    def test_deduplicated_input(self):
        a=store.put(self.conn, {"v": 1}); b=store.put(self.conn, {"v": 1})
        self.assertEqual(a,b); self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM temporal_inputs").fetchone()[0],1)
    def test_put_does_not_commit(self):
        key=store.put(self.conn, {"v": "pending"}); self.conn.rollback()
        with self.assertRaises(LookupError): store.get(self.conn,key)
    def test_content_tampering_rejected(self):
        key=store.put(self.conn,{"v":1});self.conn.execute("UPDATE temporal_inputs SET body_json=?",('{"v":2}',))
        with self.assertRaises(ValueError): store.get(self.conn,key)
    def test_invalid_reference(self):
        with self.assertRaises(ValueError): store.get(self.conn,"../input")
    def test_receipt_requires_transaction(self):
        with self.assertRaises(RuntimeError): store.save_receipt(self.conn,"r",{})
    def test_receipt_rollback(self):
        self.conn.execute("BEGIN");store.save_receipt(self.conn,"r",{"ok":True});self.conn.rollback()
        self.assertIsNone(store.receipt(self.conn,"r"))
    def test_receipt_roundtrip(self):
        self.conn.execute("BEGIN");store.save_receipt(self.conn,"r",{"sequence":1});self.conn.commit()
        self.assertEqual(store.receipt(self.conn,"r"),{"sequence":1})

class ExecutionContracts(unittest.TestCase):
    def test_native_scope_and_restore(self):
        stop=threading.Event(); previous=execution.current_task()
        with execution.activity_scope(key="unit",stage="score",stop_event=stop):
            self.assertEqual(execution.current_task()["backend"],"temporal")
            self.assertIsNone(execution.current_execution())
            with execution.browser_slot(), execution.rlm_slot(check=lambda:None): pass
        self.assertEqual(execution.current_task(),previous)
    def test_no_nested_legacy_executor(self):
        with execution.activity_scope(key="unit",stage="score",stop_event=threading.Event()):
            with self.assertRaises(RuntimeError): execution.Runtime()
    def test_cancellation(self):
        stop=threading.Event();stop.set()
        with execution.activity_scope(key="unit",stage="score",stop_event=stop):
            with self.assertRaises(execution.ExecutionStopped):
                with execution.browser_slot(): pass
    def test_async_browser_without_dask(self):
        async def run():
            with execution.activity_scope(key="unit",stage="enrich",stop_event=threading.Event()):
                async with execution.async_browser_slot(): pass
        asyncio.run(run());self.assertNotIn("dask",sys.modules);self.assertNotIn("distributed",sys.modules)
    def test_relative_artifact(self):
        root=Path(WORKSPACE.name)
        self.assertEqual(domain._document_path("docs/a.pdf",root),root/"docs/a.pdf")
    def test_artifact_escape(self):
        for path in ("../secret", "/tmp/secret", ""):
            with self.subTest(path=path), self.assertRaises(ValueError): domain._document_path(path,Path(WORKSPACE.name))
    def test_symlink_escape(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outside:
            Path(d,"linked").symlink_to(outside,target_is_directory=True)
            with self.assertRaises(ValueError): domain._document_path("linked/file",Path(d))

class ActivityBoundary(unittest.TestCase):
    def setUp(self):
        self.conn=connection()
        @contextmanager
        def scope(request): yield threading.Event(),lambda *a:None
        self.scope=patch.object(activities,"execution",scope);self.scope.start()
        self.db=fake_database(self.conn);self.db.__enter__()
    def tearDown(self):
        self.db.__exit__(None,None,None);self.scope.stop();self.conn.close()
    def test_wrong_role(self):
        with self.assertRaises(ApplicationError) as caught:
            activities.Activities("preparation")._check(contracts.ActivityRequest("a"*64,"application_attempt"))
        self.assertTrue(caught.exception.non_retryable)
    def test_cursor_bodies_stay_out_of_history(self):
        raw="https://provider/page?token=secret"; cursor=store.put(self.conn,{"kind":"cursor","value":raw});self.conn.commit()
        request=contracts.ActivityRequest("a"*64,"score",cursor)
        def operation(decoded,*_):
            self.assertEqual(decoded.cursor,raw)
            return contracts.Observation("complete",next_cursor="private-next")
        result=activities.Activities("preparation")._invoke(request,operation)
        self.assertEqual(len(result.next_cursor),64);self.assertNotIn("private",json.dumps(asdict(result)))
        self.assertEqual(store.get(self.conn,result.next_cursor)["value"],"private-next")
        self.assertFalse(self.conn.in_transaction)
    def test_error_message_sanitized(self):
        def fail(*_): raise RuntimeError("provider secret=private-password")
        with self.assertRaises(ApplicationError) as caught:
            activities.Activities("preparation")._invoke(contracts.ActivityRequest("a"*64,"score"),fail)
        self.assertNotIn("private-password",str(caught.exception))
        self.assertIsNone(caught.exception.__context__)
    def test_bad_input_not_retried(self):
        def fail(*_): raise ValueError("password")
        with self.assertRaises(ApplicationError) as caught:
            activities.Activities("preparation")._invoke(contracts.ActivityRequest("a"*64,"score"),fail)
        self.assertTrue(caught.exception.non_retryable)

class SubmissionEvidence(unittest.TestCase):
    def observe(self, attempt=None, applied=None, disposition="uncertain"):
        conn=sqlite3.connect(":memory:");conn.row_factory=sqlite3.Row
        conn.executescript("CREATE TABLE jobs(url TEXT,applied_at TEXT,state TEXT); CREATE TABLE application_attempts(job_url TEXT,status TEXT,reservation_id TEXT,reserved_at TEXT);")
        conn.execute("INSERT INTO jobs VALUES(?,?,?)",("job",applied,"active"))
        if attempt: conn.execute("INSERT INTO application_attempts VALUES(?,?,?,?)",("job",attempt,"receipt","2026-01-01"))
        evidence=types.ModuleType("applypilot.apply.submission_evidence")
        evidence.reconcile_captured_submission=lambda *a,**kw:{"disposition":disposition}
        recovery=types.ModuleType("applypilot.recovery");recovery.safe_pre_submit=lambda _:False
        try:
            with fake_database(conn),patch.dict(sys.modules,{evidence.__name__:evidence,recovery.__name__:recovery}):
                return domain._application_evidence({"job_url":"job"},{})
        finally: conn.close()
    def test_no_attempt(self): self.assertEqual(self.observe().state,"not_started")
    def test_database_receipt(self): self.assertEqual(self.observe(applied="2026-01-01").state,"submitted")
    def test_reserved_never_expires_to_permission(self): self.assertEqual(self.observe("reserved").state,"outcome_unknown")
    def test_unknown_intent(self): self.assertEqual(self.observe("side_effect_intent").state,"outcome_unknown")
    def test_confirmed_capture(self): self.assertEqual(self.observe("verification_required",disposition="confirmed").state,"submitted")
    def test_proven_absent(self): self.assertEqual(self.observe("verified_absent").state,"confirmed_not_submitted")
    def test_unproven_cancel(self): self.assertEqual(self.observe("cancelled_before_submit").state,"outcome_unknown")

class WorkflowLogic(unittest.IsolatedAsyncioTestCase):
    async def test_job_phase_order(self):
        calls=[]
        async def op(command,phase,**kw): calls.append(phase);return contracts.Observation("complete")
        from applypilot.documents.contracts import DocumentResult
        children=[]
        async def child(function, command, **kw):
            children.append(command.stage)
            return DocumentResult("b"*64,"rendered","c"*64)
        with patch.object(workflows,"operation",op),patch.object(wf,"execute_child_workflow",child):
            result=await workflows.JobWorkflow().run(contracts.Command("a"*64,policy()))
        self.assertEqual(calls,[phase for phase in policy()["job_phases"] if phase != "pdf"])
        self.assertEqual(children,["tailor","cover"])
        self.assertEqual(result["pdf"],"rendered")
        self.assertEqual(result["document_tailor"],"rendered")
        self.assertEqual(result["document_cover"],"rendered")

    async def test_document_failure_does_not_prevent_sibling_execution(self):
        from applypilot.documents.contracts import DocumentResult
        children=[]
        async def op(command,phase,**kw): return contracts.Observation("complete")
        async def child(function,command,**kw):
            children.append(command.stage)
            if command.stage == "cover":
                raise ActivityError("document renderer failed")
            return DocumentResult("b"*64,"rendered","c"*64)
        instance=workflows.JobWorkflow()
        with patch.object(workflows,"operation",op),patch.object(wf,"execute_child_workflow",child):
            with self.assertRaises(ActivityError):
                await instance.run(contracts.Command("a"*64,policy()))
        self.assertEqual(set(children),{"tailor","cover"})
        self.assertEqual(instance.results["document_tailor"],"rendered")
        self.assertEqual(instance.results["document_cover"],"failed")
    async def test_stale_document_approval_never_enters_application(self):
        from applypilot.documents.contracts import DocumentResult
        for state in ("superseded","approval_not_current"):
            seen=[]
            async def op(command,phase,**kw): return contracts.Observation("complete")
            async def child(function,command,**kw):
                seen.append(command.stage)
                return DocumentResult("b"*64,state if command.stage == "tailor" else "rendered")
            instance=workflows.JobWorkflow()
            with patch.object(workflows,"operation",op),patch.object(wf,"execute_child_workflow",child):
                result=await instance.run(contracts.Command("a"*64,policy(),"apply"))
            self.assertEqual(seen,["tailor","cover"])
            self.assertEqual(instance.phase,"documents_need_preparation")
            self.assertEqual(result["pdf"],"not_ready")
            self.assertNotIn("application",result)

    async def test_malformed_document_command_fails_execution_without_activity(self):
        from applypilot.documents.contracts import DocumentCommand
        from applypilot.orchestration.document_workflows import DocumentWorkflow
        with self.assertRaises(ApplicationError) as caught:
            await DocumentWorkflow().run(DocumentCommand("a"*64,"pdf","docs",float("inf"),60,10))
        self.assertTrue(caught.exception.non_retryable)
        self.assertEqual(caught.exception.type,"DocumentInputError")

    async def test_signal_children_start_without_awaiting_model_completion(self):
        from applypilot.orchestration.signal_workflows import SignalWorkflow
        children=[];pages=[]
        async def act(name,request,**kw):
            pages.append(request.sequence)
            return contracts.Observation("published",{"signal_ids":["b"*64],"terminal":request.sequence==1},
                                         next_cursor="cursor" if request.sequence==0 else None)
        async def child(function,command,**kw):
            self.assertIs(function,SignalWorkflow.run)
            self.assertEqual(command.signal_id,"b"*64)
            self.assertEqual(kw["parent_close_policy"],"ABANDON")
            children.append(kw["id"])
            if len(children)>1:raise WorkflowAlreadyStartedError()
            return object()  # Not awaitable: caller must not await child result.
        with patch.object(wf,"execute_activity",act),patch.object(wf,"start_child_workflow",child):
            result=await workflows.DiscoveryWorkflow().run(contracts.Command("a"*64,policy(),"discover"))
        self.assertEqual(pages,[0,1]);self.assertEqual(result["pages"],2)
        self.assertEqual(children[0],children[1])

    async def test_signal_workflow_routes_model_and_publication_separately(self):
        from applypilot.orchestration.signal_workflows import SignalWorkflow
        from applypilot.signals.contracts import SignalCommand,SignalResult
        calls=[]
        async def act(name,request,**kw):
            calls.append((name,request.signal_id,kw["task_queue"]))
            return SignalResult(request.signal_id,"extracted" if len(calls)==1 else "published",2)
        with patch.object(wf,"execute_activity",act):
            result=await SignalWorkflow().run(SignalCommand("a"*64,"test.models","test.discovery",300,120,60,10))
        self.assertEqual(calls,[("applypilot.extract_signal.v1","a"*64,"test.models"),
                                ("applypilot.publish_signal.v1","a"*64,"test.discovery")])
        self.assertEqual(result.state,"published")

    async def test_submit_response_lost_reconciles_without_duplicate(self):
        evidence=iter(("not_started","submitted")); attempts=[]
        async def op(command,phase,**kw):
            if phase=="application_evidence":return contracts.Observation(next(evidence))
            attempts.append(kw);raise ActivityError("lost response after send")
        async def sleep(_): pass
        with patch.object(workflows,"operation",op),patch.object(wf,"sleep",sleep):
            result=await workflows.ApplicationWorkflow().run(contracts.Command("a"*64,policy(),"apply"))
        self.assertEqual(result["state"],"submitted");self.assertEqual(attempts,[{"once":True}])
    async def test_unknown_never_submits(self):
        seen=[]; evidence=iter(("outcome_unknown","submitted"))
        async def op(command,phase,**kw): seen.append(phase);return contracts.Observation(next(evidence))
        async def sleep(_):pass
        with patch.object(workflows,"operation",op),patch.object(wf,"sleep",sleep):
            await workflows.ApplicationWorkflow().run(contracts.Command("a"*64,policy(),"apply"))
        self.assertEqual(seen,["application_evidence","application_evidence"])
    async def test_submission_cancellation_not_swallowed(self):
        async def op(command,phase,**kw):
            if phase=="application_evidence":return contracts.Observation("not_started")
            error=ActivityError("cancelled");error.cause=CancelledError("cancelled")
            raise error
        with patch.object(workflows,"operation",op),self.assertRaises(ActivityError):
            await workflows.ApplicationWorkflow().run(contracts.Command("a"*64,policy(),"apply"))
    async def test_discovery_continuation_keeps_sweep(self):
        p=policy();p["discovery_pages_per_run"]=2
        async def op(name,request,**kw):
            self.assertEqual(name,"applypilot.fetch_source_page.v1")
            return contracts.Observation("published",next_cursor=str(request.sequence+1))
        with patch.object(wf,"execute_activity",op):
            with self.assertRaises(ContinueAsNew) as caught:
                await workflows.DiscoveryWorkflow().run(contracts.Command("a"*64,p,"discover"))
        self.assertEqual(caught.exception.command.token,"stable-sweep");self.assertEqual(caught.exception.command.page,2)
    async def test_bad_cursor_is_visible(self):
        async def op(name,request,**kw):return contracts.Observation("published",next_cursor=request.cursor)
        with patch.object(wf,"execute_activity",op),self.assertRaises(ApplicationError):
            await workflows.DiscoveryWorkflow().run(contracts.Command("a"*64,policy(),"discover"))
    async def test_dispatch_awaits_child_acceptance_not_completion(self):
        started=[]
        async def act(*a,**kw):return contracts.WorkPage([{"kind":"jobs","subject":"opaque","reference":"b"*64}])
        async def child(*a,**kw):started.append(kw)
        with patch.object(wf,"execute_activity",act),patch.object(wf,"start_child_workflow",child):
            result=await workflows.DispatchWorkflow().run(contracts.Command("a"*64,policy()))
        self.assertEqual(result["started"],1);self.assertEqual(started[0]["parent_close_policy"],"ABANDON")
    async def test_dispatch_duplicate_active_child(self):
        async def act(*a,**kw):return contracts.WorkPage([{"kind":"jobs","subject":"opaque","reference":"b"*64}])
        async def child(*a,**kw):raise WorkflowAlreadyStartedError()
        with patch.object(wf,"execute_activity",act),patch.object(wf,"start_child_workflow",child):
            result=await workflows.DispatchWorkflow().run(contracts.Command("a"*64,policy()))
        self.assertEqual(result,{"already_running":1})
    async def test_admission_history_checkpoint(self):
        p=policy();p["pages_per_run"]=2
        async def op(command,phase,**kw):return contracts.Observation("complete",next_cursor=(kw["cursor"] or "")+"x")
        with patch.object(workflows,"operation",op),self.assertRaises(ContinueAsNew) as caught:
            await workflows.ServiceWorkflow().run(contracts.Command("a"*64,p,"admission"))
        self.assertEqual(caught.exception.command.cursor,"xx")

class DiagnosticAndSourceContracts(unittest.TestCase):
    def test_missing_snapshot_not_healthy(self):
        with tempfile.TemporaryDirectory() as d:self.assertEqual(observability.cached(Path(d))["state"],"not_observed")
    def test_snapshot_is_explicitly_cached(self):
        with tempfile.TemporaryDirectory() as d:
            observability.write_snapshot(Path(d),"status.json",{"captured_at":"example","workflows":[]})
            self.assertTrue(observability.cached(Path(d))["cached"])
            self.assertEqual(list(Path(d,"temporal").iterdir()),[Path(d,"temporal/status.json")])
    def test_snapshot_filename_restriction(self):
        with self.assertRaises(ValueError):observability.write_snapshot(Path(WORKSPACE.name),"../escape",{})
    def test_sensitive_tables_excluded(self):
        source=(ROOT/"src/applypilot/operations/bundle.py").read_text()
        self.assertIn('"temporal_inputs"',source);self.assertIn('"temporal_publication_receipts"',source)
    def test_replay_modules_have_no_io_imports(self):
        tree=ast.parse((ROOT/"src/applypilot/orchestration/workflows.py").read_text())
        imports=[n.module for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
        self.assertTrue(all(m in {"__future__","datetime","temporalio","temporalio.common",
            "temporalio.exceptions","contracts","applypilot.documents.contracts","document_workflows","applypilot.signals.contracts","signal_workflows"} for m in imports))
        direct=[a.name for n in ast.walk(tree) if isinstance(n,ast.Import) for a in n.names]
        self.assertTrue(all(name == "asyncio" for name in direct))
        calls=[ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n,ast.Call)]
        self.assertFalse(set(calls)&{"asyncio.sleep","asyncio.to_thread","open","time.sleep"})
    def test_no_hidden_scheduler_in_new_modules(self):
        for p in (ROOT/"src/applypilot/orchestration").glob("*.py"):
            tree=ast.parse(p.read_text())
            imported=[n.module or "" for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
            self.assertFalse(any(m.startswith(("dask","applypilot.supervisor","applypilot.flow")) for m in imported),p.name)
    def test_compose_is_persistent_local_and_ordered(self):
        compose=yaml.safe_load((ROOT/"docs/guides/docker-compose.temporal.yml").read_text());s=compose["services"]
        self.assertEqual(s["temporal"]["depends_on"]["temporal-schema"]["condition"],"service_completed_successfully")
        self.assertIn("temporal-postgres-data",compose["volumes"])
        self.assertTrue(all(port.startswith("127.0.0.1:") for name in ("temporal","temporal-ui") for port in s[name]["ports"]))
    def test_all_python_312_grammar(self):
        for p in (ROOT/"src").rglob("*.py"):
            ast.parse(p.read_text(encoding="utf-8-sig"),filename=str(p),feature_version=(3,12))

class SourcePublicationContracts(unittest.TestCase):
    def setUp(self):
        from dataclasses import dataclass
        from applypilot.discovery.models import SourceTarget, DiscoveryPage
        self.conn=connection(); self.conn.execute("CREATE TABLE published(value TEXT)");self.conn.commit()
        self.fetches=0;self.acks=0;self.fail_ack=False;self.fail_publish=False
        self.mode="complete";self.cursor=None;self.ack_metadata=True
        @dataclass
        class Context:
            runtime: object=None
            intent: object=None
            applicant: object=None
            reserve_budget: object=None
            reserve_budgets: object=None
            capture_signal: object=None
        self.ctx=Context({},types.SimpleNamespace(fingerprint=lambda:"intent"),types.SimpleNamespace(fingerprint=lambda:"profile"))
        self.target=SourceTarget(provider="unit_source")
        self.value={"target":asdict(self.target)}
        self.request=contracts.ActivityRequest("a"*64,"discovery_page",options={"sweep":"sweep"})
        self.cfg={"context":self.ctx,"stop_event":threading.Event()}
        self.root={"context_key":"context"}
        repo=types.ModuleType("applypilot.discovery.repository")
        @contextmanager
        def transaction(conn,**kw):
            conn.execute("BEGIN")
            try:yield;conn.commit()
            except BaseException:conn.rollback();raise
        def publish(records,*a,**kw):
            self.assertTrue(self.conn.in_transaction)
            self.conn.execute("INSERT INTO published VALUES(?)",("row",))
            if self.fail_publish:raise RuntimeError("publication failed")
            return types.SimpleNamespace(fetched=len(records),new=len(records))
        repo.transaction=transaction;repo.publish_records=publish
        caps=types.ModuleType("applypilot.discovery.capabilities")
        caps.access_reason=lambda *a:None
        caps.capability_for=lambda *a:types.SimpleNamespace(module="unit_temporal_adapter")
        caps.source_key=lambda *a:"unit"
        adapter=types.ModuleType("applypilot.discovery.unit_temporal_adapter")
        def fetch(task,context,cursor):
            self.assertFalse(self.conn.in_transaction)
            self.fetches+=1
            if self.mode=="paid":context.reserve_budget("paid")
            metadata={"signal_publication_receipts":[{"id":"receipt"}]} if self.ack_metadata else {}
            return DiscoveryPage(task.task_id,records=({"source_signal_id":"signal","source_revision":"revision"},),
                exhausted=self.mode=="complete",partial=self.mode=="partial",next_cursor=self.cursor,request_metadata=metadata)
        adapter.execute_task=fetch
        cache=types.ModuleType("applypilot.discovery.signal_cache")
        def ack(*a):
            self.assertFalse(self.conn.in_transaction)
            self.acks+=1
            if self.fail_ack:raise RuntimeError("acknowledgement offline")
        cache.cache_for=lambda _:types.SimpleNamespace(acknowledge_publications=ack)
        self.db=fake_database(self.conn);self.db.__enter__()
        self.mods=patch.dict(sys.modules,{m.__name__:m for m in (repo,caps,adapter,cache)});self.mods.start()
        # from-package imports can cache an old attribute even when sys.modules is replaced.
        import applypilot.discovery as package
        self.repoattr=patch.object(package,"repository",repo,create=True);self.repoattr.start()
    def tearDown(self):
        self.repoattr.stop();self.mods.stop();self.db.__exit__(None,None,None);self.conn.close()
    def run_page(self):
        from applypilot.orchestration.source_page import execute_page
        return execute_page(self.request,self.value,self.root,self.cfg,lambda *_:None)
    def test_network_finishes_before_publication_and_ack_after_commit(self):
        result=self.run_page();self.assertEqual(result.state,"exhausted");self.assertEqual(self.acks,1)
    def test_duplicate_reuses_publication_receipt(self):
        a=self.run_page();b=self.run_page()
        self.assertEqual(a,b);self.assertEqual(self.fetches,1);self.assertEqual(self.acks,2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM published").fetchone()[0],1)
    def test_ack_crash_replays_without_refetch(self):
        self.fail_ack=True
        with self.assertRaises(RuntimeError):self.run_page()
        self.fail_ack=False;self.run_page()
        self.assertEqual(self.fetches,1);self.assertEqual(self.acks,2)
    def test_publication_failure_rolls_back_receipt(self):
        self.fail_publish=True
        with self.assertRaises(RuntimeError):self.run_page()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM published").fetchone()[0],0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM temporal_publication_receipts").fetchone()[0],0)
        self.assertEqual(self.acks,0)
    def test_partial_page_not_false_exhaustion(self):
        self.mode="partial";self.cursor="next-page"
        result=self.run_page()
        self.assertEqual(result.state,"coverage_partial")
        self.assertFalse(result.detail["terminal"])
        self.assertEqual(result.next_cursor,"next-page")
    def test_cursor_cycle_rejected(self):
        self.run_page();self.request.sequence=1
        with self.assertRaises(ApplicationError):self.run_page()
    def test_missing_sweep_rejected(self):
        self.request.options={}
        with self.assertRaises(ApplicationError):self.run_page()
    def test_paid_transport_never_gets_fake_grant(self):
        self.mode="paid"
        with self.assertRaises(ApplicationError) as caught:self.run_page()
        self.assertEqual(caught.exception.type,"SourceAccessDenied")

class RecordingResult(unittest.TextTestResult):
    def startTest(self,test):
        self.current_id=test.id();super().startTest(test)
    def addSuccess(self,test):
        super().addSuccess(test);self.success_ids=getattr(self,"success_ids",[])+[test.id()]

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--report",type=Path)
    args=parser.parse_args()
    result=unittest.TextTestRunner(verbosity=2,resultclass=RecordingResult).run(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    report={"suite":"offline_contracts_with_explicit_doubles","python":sys.version.split()[0],
        "tests_run":result.testsRun,"passed":len(getattr(result,"success_ids",[])),
        "failures":[{"test":t.id(),"trace":trace} for t,trace in result.failures+result.errors],
        "success":result.wasSuccessful(),"passed_tests":getattr(result,"success_ids",[]),
        "not_tested":["actual Temporal SDK import/worker startup","Temporal server matching/persistence/replay","abrupt pod/worker death","PostgreSQL integration/locking","BrowserOS/external submissions","Kubernetes/autoscaling","full legacy adapter behavior"]}
    if args.report:args.report.write_text(json.dumps(report,indent=2),encoding="utf-8")
    return 0 if result.wasSuccessful() else 1
if __name__=="__main__":raise SystemExit(main())
