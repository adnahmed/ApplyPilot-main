"""Actual SDK/test-server/replay checks using MOCK domain Activities, no applicant IO.

Run in the project's Python 3.12 environment after installing temporalio==1.33.0:
  python tools/temporal/validate_native.py --report temporal-native-validation.json
The SDK downloads its time-skipping test server when necessary. This does not
contact the configured production namespace, database or job providers. It does
not replace abrupt-process-death, real PostgreSQL or BrowserOS integration tests.
"""
from __future__ import annotations
import argparse
import asyncio
from contextlib import AsyncExitStack
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from applypilot.orchestration.contracts import ActivityRequest, Command, Observation, ROLES
from applypilot.documents.contracts import PrepareDocument, PreparedDocument, RenderDocument, DocumentResult
from applypilot.signals.contracts import SignalInput, SignalResult

async def validate():
    from temporalio import activity
    from temporalio.client import ScheduleAlreadyRunningError
    from temporalio.exceptions import ApplicationError
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker, Replayer
    from applypilot.orchestration.client import desired_schedule
    from applypilot.orchestration.settings import load, TemporalPolicy
    from applypilot.orchestration.workflows import WORKFLOWS, JobWorkflow, ApplicationWorkflow, DiscoveryWorkflow, ServiceWorkflow
    from applypilot.orchestration.document_workflows import DOCUMENT_WORKFLOWS
    from applypilot.orchestration.signal_workflows import SIGNAL_WORKFLOWS
    from applypilot.orchestration.search_workflows import SEARCH_WORKFLOWS
    from applypilot.documents.contracts import (PrepareDocument, PreparedDocument, RenderDocument, DocumentResult)
    from applypilot.signals.contracts import SignalInput, SignalResult
    from applypilot.orchestration.contracts import identity
    definitions = [*WORKFLOWS, *DOCUMENT_WORKFLOWS, *SIGNAL_WORKFLOWS, *SEARCH_WORKFLOWS]

    calls=[]; submitted=False
    @activity.defn(name="applypilot.operation.v1")
    async def mock_operation(request: ActivityRequest) -> Observation:
        nonlocal submitted
        calls.append((request.reference,request.phase,activity.info().attempt,request.sequence,request.options.get("sweep")))
        if request.reference=="1"*64 and request.phase=="score" and activity.info().attempt==1:
            raise ApplicationError("Synthetic transient failure",type="SyntheticTransient")
        if request.phase=="application_evidence":
            return Observation("submitted" if submitted else "not_started")
        if request.phase=="application_attempt":
            submitted=True
            raise ApplicationError("Synthetic response loss after committing mock effect",type="SyntheticTransient")
        if request.phase=="discovery_page":
            return Observation("exhausted" if request.sequence==2 else "published",next_cursor=str(request.sequence+1))
        if request.phase=="admission":
            return Observation("complete",next_cursor=None if request.cursor=="next" else "next")
        return Observation("complete")

    @activity.defn(name="applypilot.prepare_document.v1")
    async def mock_prepare(request: PrepareDocument) -> PreparedDocument:
        request.validate()
        return PreparedDocument(("a" if request.stage == "tailor" else "b") * 64, "prepared", "docx")

    @activity.defn(name="applypilot.render_document.v1")
    async def mock_render(request: RenderDocument) -> DocumentResult:
        request.validate()
        calls.append((request.document_id, "render_document", activity.info().attempt, 0, None))
        if request.document_id == "a" * 64 and activity.info().attempt == 1:
            raise ApplicationError("Synthetic render failure", type="SyntheticTransient")
        return DocumentResult(request.document_id, "rendered", "c" * 64)

    @activity.defn(name="applypilot.fetch_source_page.v1")
    async def mock_page(request: ActivityRequest) -> Observation:
        calls.append((request.reference, request.phase, activity.info().attempt,
                      request.sequence, request.options.get("sweep")))
        return Observation("exhausted" if request.sequence == 2 else "published",
                           detail={"signal_ids": ["e" * 64]},
                           next_cursor=str(request.sequence + 1))

    @activity.defn(name="applypilot.extract_signal.v1")
    async def mock_extract(request: SignalInput) -> SignalResult:
        return SignalResult(request.validate().signal_id, "extracted", 1)

    @activity.defn(name="applypilot.publish_signal.v1")
    async def mock_publish(request: SignalInput) -> SignalResult:
        return SignalResult(request.validate().signal_id, "published", 1)

    checks=[]
    with tempfile.TemporaryDirectory() as directory:
        policy=load(Path(directory))
        p=policy.model_dump(mode="json")
        p.update(workspace="validation-"+uuid.uuid4().hex[:12],discovery_pages_per_run=2,evidence_poll_seconds=1)
        async with await WorkflowEnvironment.start_time_skipping() as env:
            p["namespace"]=env.client.namespace
            policy=TemporalPolicy.model_validate(p)
            async with AsyncExitStack() as stack:
                # Real SDK Worker bootstrap validates all production Workflow definitions.
                await stack.enter_async_context(Worker(env.client,task_queue=policy.queue("workflow"),workflows=definitions))
                # Intentionally start without a preparation poller: accepted work must
                # remain durable while a capability worker is absent.
                job=await env.client.start_workflow(JobWorkflow.run,Command("1"*64,p),
                    id="native-test-job-"+uuid.uuid4().hex,task_queue=policy.queue("workflow"))
                await env.sleep(2)
                for role in ROLES:
                    if role!="workflow":
                        activities = {"documents": [mock_prepare, mock_render],
                                      "models": [mock_extract],
                                      "discovery": [mock_page, mock_publish]}.get(role, [mock_operation])
                        await stack.enter_async_context(Worker(env.client,task_queue=policy.queue(role),activities=activities))
                result=await job.result()
                assert set(result) == set(p["job_phases"]) | {"document_tailor", "document_cover"}
                assert result["pdf"] == result["document_tailor"] == result["document_cover"] == "rendered"
                assert [x[2] for x in calls if x[0] == "a" * 64 and x[1] == "render_document"] == [1, 2]
                checks.append("native_document_children_render_retry_and_sibling_completion")
                checks.append("job_completed_after_capability_worker_joined")
                assert [x[2] for x in calls if x[0]=="1"*64 and x[1]=="score"]==[1,2]
                checks.append("native_activity_retry_after_transient_failure")
                await Replayer(workflows=definitions).replay_workflow(await job.fetch_history())
                checks.append("job_history_replay")

                app=await env.client.start_workflow(ApplicationWorkflow.run,Command("2"*64,p,"apply"),
                    id="native-test-application-"+uuid.uuid4().hex,task_queue=policy.queue("workflow"))
                assert (await app.result())["state"]=="submitted"
                assert len([x for x in calls if x[1]=="application_attempt"])==1
                await Replayer(workflows=definitions).replay_workflow(await app.fetch_history())
                checks.append("lost_response_reconciled_without_duplicate_mock_submission_and_replayed")

                source=await env.client.start_workflow(DiscoveryWorkflow.run,Command("3"*64,p,"discover"),
                    id="native-test-discovery-"+uuid.uuid4().hex,task_queue=policy.queue("workflow"))
                assert (await source.result())["pages"]==3
                source_calls=[x for x in calls if x[1]=="discovery_page"]
                assert [x[3] for x in source_calls]==[0,1,2]
                assert len({x[4] for x in source_calls})==1
                await Replayer(workflows=definitions).replay_workflow(await source.fetch_history())
                checks.append("continue_as_new_keeps_discovery_sweep_and_replays")
                signal_handle = env.client.get_workflow_handle(identity(policy.workspace, "signal", "e" * 64))
                signal_result = await signal_handle.result()
                assert signal_result["state"] == "published"
                await Replayer(workflows=definitions).replay_workflow(await signal_handle.fetch_history())
                checks.append("signal_child_survives_provider_parent_and_replays")

                result=await env.client.execute_workflow(ServiceWorkflow.run,Command("4"*64,p,"admission"),
                    id="native-test-service-"+uuid.uuid4().hex,task_queue=policy.queue("workflow"))
                assert result.state=="complete" and result.next_cursor is None
                checks.append("finite_service_cursor_traversal")

                sid="native-test-schedule-"+uuid.uuid4().hex
                schedule=desired_schedule(policy,"tracking","5"*64,start=False)
                await env.client.create_schedule(sid,schedule)
                handle=env.client.get_schedule_handle(sid)
                try:
                    assert (await handle.describe()).schedule.state.paused
                    try:await env.client.create_schedule(sid,schedule)
                    except ScheduleAlreadyRunningError:pass
                    else:raise AssertionError("duplicate schedule was unexpectedly accepted")
                    checks.append("native_schedule_created_paused_and_duplicate_detected")
                finally:await handle.delete()
    return checks

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--report",type=Path,required=True);args=parser.parse_args()
    report={"suite":"actual_sdk_test_server_with_mock_domain_activities","python":sys.version.split()[0],
        "not_tested":["abrupt process/pod death","real PostgreSQL transactions","browser recovery/real submissions","Kubernetes deployment","every legacy feature adapter"]}
    try:
        version=importlib.metadata.version("temporalio")
        if version!="1.33.0":raise RuntimeError("Use the pinned temporalio==1.33.0 for this validation")
        report["sdk"]=version
        if sys.version_info[:2] != (3, 12):
            raise RuntimeError("Use the project-supported Python 3.12 runtime for native validation")
        report["passed_checks"]=asyncio.run(validate());report["status"]="passed"
    except importlib.metadata.PackageNotFoundError:
        report.update(status="not_run",reason="Temporal SDK is not installed")
    except Exception as exc:
        report.update(status="failed",error_type=type(exc).__name__,message=str(exc))
    args.report.write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report,indent=2))
    return 0 if report["status"]=="passed" else 2
if __name__=="__main__":raise SystemExit(main())
