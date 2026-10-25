"""Durable control flow only. No database, environment, filesystem or model IO.

The policy is part of Workflow input/history. Changing a worker's YAML cannot
change the meaning of an existing history during replay.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ActivityError, CancelledError, WorkflowAlreadyStartedError
from .contracts import (ActivityRequest, Command, NON_RETRYABLE, Observation,
                        PHASE_ROLES, WorkPage, identity, task_queue, EntityCommand, ApplicationCommand, digest)
from .posting_research_workflows import CompanyPostingResearchWorkflow
from .posting_research_contracts import PostingResearchCommand


def activity_options(policy, phase, *, once=False):
    role = PHASE_ROLES[phase]
    settings = policy["activities"][role]
    return dict(
        task_queue=task_queue(policy["queue_prefix"], policy["workspace"], role),
        start_to_close_timeout=timedelta(seconds=settings["start_to_close_seconds"]),
        heartbeat_timeout=timedelta(seconds=settings["heartbeat_seconds"]),
        retry_policy=RetryPolicy(
            initial_interval=timedelta(seconds=policy["activity_retry_initial_seconds"]),
            maximum_interval=timedelta(seconds=policy["activity_retry_maximum_seconds"]),
            maximum_attempts=1 if once else 0,
            non_retryable_error_types=list(NON_RETRYABLE)),
    )


async def operation(command, phase, *, cursor=None, sequence=0, once=False):
    role = PHASE_ROLES[phase]
    options = {"heartbeat_interval_seconds": command.policy["activities"][role]["heartbeat_interval_seconds"],
               "sweep": getattr(command, "token", ""), "workspace": command.policy["workspace"]}
    if isinstance(command, EntityCommand):
        options["job_id"] = command.entity_id
    if isinstance(command, ApplicationCommand):
        options.update(application_id=command.application_id, operation_id=command.operation_id,
            attempt_id=command.attempt_id, previous_attempt_id=command.previous_attempt_id,
            after_dispatch=command.dispatch_started, review_reference=command.review_reference)
        if phase == "application_account_enqueue":
            from dataclasses import asdict
            options.update(command=asdict(command), requester=workflow.info().workflow_id)
    request = ActivityRequest(command.reference, phase, cursor, sequence, options)
    execution_options = activity_options(command.policy, phase, once=once)
    if phase == "application_attempt":
        execution_options["cancellation_type"] = workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED
    return await workflow.execute_activity("applypilot.operation.v1", request,
        result_type=Observation, **execution_options)


@workflow.defn(name="applypilot.company-posting.v1")
class CompanyWorkflow:
    """One employer/posting evidence identity, independent of job preparation."""
    def __init__(self):
        self.state = "created"

    @workflow.query
    def status(self) -> dict:
        return {"state": self.state}

    @workflow.run
    async def run(self, command: EntityCommand) -> Observation:
        self.state = "researching"
        result = await workflow.execute_child_workflow(CompanyPostingResearchWorkflow.run,
            PostingResearchCommand(command.reference, command.policy, entity_id=command.entity_id),
            id=identity(command.policy["workspace"], "posting-research", command.entity_id),
            task_queue=task_queue(command.policy["queue_prefix"], command.policy["workspace"], "workflow"),
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE)
        self.state = result.state
        return result


@workflow.defn(name="applypilot.job.v3")
class JobWorkflow:
    def __init__(self):
        self.phase = "created"
        self.results = {}
        self.job_id = ""

    @workflow.query
    def status(self) -> dict:
        return {"job_id": self.job_id, "phase": self.phase, "results": self.results}

    @workflow.run
    async def run(self, command: EntityCommand) -> dict:
        self.job_id = command.entity_id
        if any(p in command.policy["job_phases"] for p in ("tailor", "cover", "pdf")):
            await operation(command, "document_inspect")
        for phase in command.policy["job_phases"]:
            self.phase = phase
            if phase == "company_research":
                result = await workflow.execute_child_workflow(CompanyWorkflow.run, command,
                    id=identity(command.policy["workspace"], "company-posting", command.entity_id),
                    task_queue=task_queue(command.policy["queue_prefix"], command.policy["workspace"], "workflow"),
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE)
                self.results[phase] = result.state
                if result.state == "needs_input":
                    return self.results
            elif phase == "pdf":
                from applypilot.documents.contracts import JobDocumentCommand
                from .document_workflows import JobDocumentWorkflow
                cfg = command.policy["activities"]["documents"]
                children = [workflow.execute_child_workflow(JobDocumentWorkflow.run,
                    JobDocumentCommand(command.reference, stage,
                        task_queue(command.policy["queue_prefix"], command.policy["workspace"], "documents"),
                        cfg["start_to_close_seconds"], cfg["heartbeat_seconds"], cfg["heartbeat_interval_seconds"]),
                    id=identity(command.policy["workspace"], "job-document", command.entity_id + ":" + stage),
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE)
                    for stage in ("tailor", "cover")]
                outcomes = await asyncio.gather(*children, return_exceptions=True)
                for stage, outcome in zip(("tailor", "cover"), outcomes):
                    self.results["document_" + stage] = "failed" if isinstance(outcome, BaseException) else outcome.state
                failure = next((item for item in outcomes if isinstance(item, BaseException)), None)
                if failure is not None:
                    raise failure
                if any(outcome.state in {"approval_not_current", "superseded"} for outcome in outcomes):
                    await operation(command, "document_inspect")
                    self.phase = "documents_need_preparation"
                    self.results[phase] = "not_ready"
                    return self.results
                if any(outcome.state not in {"rendered", "not_applicable"} for outcome in outcomes):
                    from temporalio.exceptions import ApplicationError
                    raise ApplicationError("Unsupported document outcome", type="DocumentContractError", non_retryable=True)
                self.results[phase] = "rendered" if any(o.state == "rendered" for o in outcomes) else "not_applicable"
            else:
                result = await operation(command, phase)
                self.results[phase] = result.state
                if result.state == "superseded":
                    self.phase = "input_changed"
                    return self.results
        if command.kind == "apply":
            self.phase = "application"
            try:
                application = await workflow.execute_child_workflow(ApplicationWorkflow.run,
                    ApplicationCommand(command.reference, command.policy, command.entity_id),
                    id=identity(command.policy["workspace"], "application", command.entity_id),
                    task_queue=task_queue(command.policy["queue_prefix"], command.policy["workspace"], "workflow"),
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    parent_close_policy=workflow.ParentClosePolicy.REQUEST_CANCEL)
                self.results["application"] = application["state"]
            except WorkflowAlreadyStartedError:
                self.results["application"] = "already_running"
        self.phase = "complete"
        return self.results


@workflow.defn(name="applypilot.application.v2")
class ApplicationWorkflow:
    """Prepare -> one dispatch -> evidence, never timeout -> blind re-submission.

    A new attempt ID is issued only after durable negative evidence. Both
    dispatch_started and every identity survive Continue-As-New. Signals carry
    only references; an Activity validates attributable operator/provider proof.
    """
    def __init__(self):
        self.state = "observing"
        self.application_id = ""
        self.attempt_id = ""
        self.review_reference = ""
        self.account_completed = ""

    @workflow.signal
    def account_dispatch_completed(self, attempt_id: str) -> None:
        if not self.attempt_id or attempt_id == self.attempt_id:
            self.account_completed = attempt_id

    @workflow.query
    def status(self) -> dict:
        return {"application_id": self.application_id, "attempt_id": self.attempt_id, "state": self.state}

    @workflow.signal
    def review_available(self, reference: str) -> None:
        if isinstance(reference, str) and len(reference) == 64 and all(c in "0123456789abcdef" for c in reference):
            self.review_reference = reference

    async def _wait(self, seconds):
        try:
            await workflow.wait_condition(lambda: bool(self.review_reference) or
                bool(self.attempt_id and self.account_completed == self.attempt_id), timeout=timedelta(seconds=seconds))
        except TimeoutError:
            pass

    @workflow.run
    async def run(self, command: ApplicationCommand) -> dict:
        command.operation_id = command.operation_id or workflow.uuid4().hex
        self.application_id = command.application_id
        self.attempt_id = command.attempt_id
        self.review_reference = self.review_reference or command.review_reference
        observations = 0
        try:
            while True:
                if self.account_completed == command.attempt_id:
                    command.account_queued = False
                    command.dispatch_started = True
                    self.account_completed = ""
                delay = command.policy["evidence_poll_seconds"]
                if self.review_reference:
                    consumed = self.review_reference
                    command.review_reference = consumed
                    try:
                        reviewed = await operation(command, "application_review", once=True)
                        if reviewed.detail.get("attempt_id"):
                            command.attempt_id = reviewed.detail["attempt_id"]
                            self.attempt_id = command.attempt_id
                    except ActivityError as exc:
                        if isinstance(getattr(exc, "cause", None), CancelledError):
                            raise
                        self.state = "evidence_rejected"
                    if self.review_reference == consumed:
                        self.review_reference = ""
                    command.review_reference = ""
                observed = await operation(command, "application_evidence")
                self.state = observed.state
                if observed.state in {"submitted", "not_applicable", "dry_run"}:
                    return {"state": observed.state, "application_id": command.application_id}
                prepared = None
                if observed.state in {"not_started", "confirmed_not_submitted"}:
                    previous = observed.detail.get("attempt_id", "")
                    if previous and previous == command.attempt_id:
                        command.sequence += 1
                    command.previous_attempt_id = previous
                    command.attempt_id = digest({"operation": command.operation_id, "sequence": command.sequence})
                    self.attempt_id = command.attempt_id
                    command.dispatch_started = False
                    command.account_queued = False
                    command.state = "preparing"
                    prepared = await operation(command, "application_prepare")
                elif observed.state == "prepared":
                    prepared = observed
                elif observed.detail.get("attempt_id"):
                    # Adopt only the evidence identity for review, never permission
                    # to relaunch somebody else's prepared or interrupted adapter.
                    command.attempt_id = observed.detail["attempt_id"]
                    self.attempt_id = command.attempt_id
                if prepared is not None:
                    self.state = prepared.state
                    if prepared.state in {"submitted", "not_applicable", "dry_run"}:
                        return {"state": prepared.state, "application_id": command.application_id}
                    if prepared.state == "prepared" and command.dispatch_started:
                        self.state = "reconciling"
                    elif prepared.state == "prepared":
                        if prepared.detail.get("channel") == "linkedin-easy-apply":
                            if not command.account_queued:
                                queued = await operation(command, "application_account_enqueue")
                                command.account_workflow_id = queued.detail["workflow_id"]
                                command.account_queued = True
                            self.state = "awaiting_account_spacing"
                            await self._wait(delay)
                            observations += 1
                            if (observations >= command.policy["evidence_observations_per_run"]
                                    or workflow.info().is_continue_as_new_suggested()):
                                command.review_reference = self.review_reference
                                if self.account_completed == command.attempt_id:
                                    command.account_queued = False
                                    command.dispatch_started = True
                                workflow.continue_as_new(command)
                            continue
                        command.state = "dispatched"
                        command.dispatch_started = True
                        try:
                            attempted = await operation(command, "application_attempt")
                            self.state = attempted.state
                            if attempted.state in {"submitted", "not_applicable", "dry_run"}:
                                return {"state": attempted.state, "application_id": command.application_id}
                        except ActivityError as exc:
                            if isinstance(getattr(exc, "cause", None), CancelledError):
                                raise
                            self.state = "reconciling"
                    elif prepared.state == "provider_restricted":
                        delay = max(delay, prepared.detail.get("retry_after_seconds", delay))
                self.attempt_id = command.attempt_id
                command.state = self.state
                await self._wait(delay)
                observations += 1
                if (observations >= command.policy["evidence_observations_per_run"]
                        or workflow.info().is_continue_as_new_suggested()):
                    command.review_reference = self.review_reference
                    if self.account_completed == command.attempt_id:
                        command.account_queued = False
                        command.dispatch_started = True
                    workflow.continue_as_new(command)
        except asyncio.CancelledError:
            if command.account_queued and command.account_workflow_id:
                await workflow.get_external_workflow_handle(command.account_workflow_id).signal(
                    'cancel_attempt', command.attempt_id)
            raise



@workflow.defn(name="applypilot.discovery.v2")
class DiscoveryWorkflow:
    def __init__(self):
        self.pages = 0
        self.state = "created"

    @workflow.query
    def status(self) -> dict:
        return {"state": self.state, "pages": self.pages}

    @workflow.run
    async def run(self, command: Command) -> dict:
        command.token = command.token or workflow.uuid4().hex
        cursor = command.cursor or None
        self.pages = command.page
        observe_coverage = workflow.patched("source-coverage-observation-v1")
        while True:
            self.state = "fetching"
            request = ActivityRequest(command.reference, "discovery_page", cursor, self.pages,
                {"heartbeat_interval_seconds": command.policy["activities"]["discovery"]["heartbeat_interval_seconds"],
                 "sweep": command.token})
            if observe_coverage:
                request.options["coverage_clean"] = command.coverage_clean
            result = await workflow.execute_activity("applypilot.fetch_source_page.v1",
                request, result_type=Observation,
                **activity_options(command.policy, "discovery_page"))
            if result.state == "browser_challenge":
                self.state = "browser_recovery"
                request.options["browser_recovery"] = result.detail["reference"]
                result = await workflow.execute_activity("applypilot.fetch_source_page.v1",
                    request, result_type=Observation,
                    **activity_options(command.policy, "discovery_page"))
            if result.state == "owner_target":
                from .target_contracts import TargetCommand
                from .target_workflows import TargetWorkflow
                try:
                    target_result = await workflow.execute_child_workflow(TargetWorkflow.run,
                        TargetCommand(command.reference, command.policy, token=command.token),
                        id=identity(command.policy['workspace'], 'target', result.detail['target_id']),
                        task_queue=task_queue(command.policy['queue_prefix'], command.policy['workspace'], 'workflow'),
                        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE)
                    return {'state': target_result.state, **target_result.detail}
                except WorkflowAlreadyStartedError:
                    return {'state': 'already_running'}
            # Await the ChildWorkflowExecutionStarted event, not the result.
            # A child persists independently after this reader completes or continues.
            from applypilot.signals.contracts import SignalCommand
            from .signal_workflows import SignalWorkflow
            model_cfg = command.policy["activities"]["models"]
            publish_cfg = command.policy["activities"]["discovery"]
            for reference in result.detail.get("signal_ids", ()):
                child = SignalCommand(reference,
                    task_queue(command.policy["queue_prefix"], command.policy["workspace"], "models"),
                    task_queue(command.policy["queue_prefix"], command.policy["workspace"], "discovery"),
                    model_cfg["start_to_close_seconds"], publish_cfg["start_to_close_seconds"],
                    min(model_cfg["heartbeat_seconds"], publish_cfg["heartbeat_seconds"]),
                    min(model_cfg["heartbeat_interval_seconds"], publish_cfg["heartbeat_interval_seconds"])).validate()
                try:
                    await workflow.start_child_workflow(SignalWorkflow.run, child,
                        id=identity(command.policy["workspace"], "signal", reference),
                        task_queue=task_queue(command.policy["queue_prefix"], command.policy["workspace"], "workflow"),
                        parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
                except WorkflowAlreadyStartedError:
                    # Repeated pages point at the same immutable signal revision.
                    pass
            self.pages += 1
            self.state = result.state
            if observe_coverage:
                command.coverage_clean = command.coverage_clean and result.detail.get("coverage_complete", False)
            if result.detail.get("terminal") or result.state in {"exhausted", "not_applicable", "source_access_denied"}:
                return {"state": result.state, "pages": self.pages}
            if result.next_cursor is None or result.next_cursor == cursor:
                # A provider contract bug is visible, never a spinning workflow.
                from temporalio.exceptions import ApplicationError
                raise ApplicationError("Nonterminal source page did not advance its cursor",
                                       type="SourceContractError", non_retryable=True)
            cursor = result.next_cursor
            if (self.pages % command.policy["discovery_pages_per_run"] == 0
                    or workflow.info().is_continue_as_new_suggested()):
                if observe_coverage:
                    workflow.continue_as_new(Command(command.reference, command.policy,
                        command.kind, cursor, self.pages, token=command.token,
                        coverage_clean=command.coverage_clean))
                workflow.continue_as_new(Command(command.reference, command.policy,
                    command.kind, cursor, self.pages, token=command.token))


@workflow.defn(name="applypilot.service.v1")
class ServiceWorkflow:
    @workflow.run
    async def run(self, command: Command) -> Observation:
        if command.kind in {"learning", "company_discovery", "career_ops", "referrals",
                            "network_prepare", "network_research", "network_delivery"}:
            if command.kind == "learning":
                from .learning_workflows import LearningWorkflow
                implementation = LearningWorkflow.run
            elif command.kind == "company_discovery":
                from .company_workflows import CompanyDiscoveryWorkflow
                implementation = CompanyDiscoveryWorkflow.run
            elif command.kind == "career_ops":
                from .integration_workflows import ReportsWorkflow
                implementation = ReportsWorkflow.run
            else:
                from .integration_workflows import NetworkingWorkflow
                implementation = NetworkingWorkflow.run
            child = await workflow.execute_child_workflow(implementation, command,
                id=identity(command.policy["workspace"], "service", command.kind + ":" + workflow.info().run_id),
                task_queue=task_queue(command.policy["queue_prefix"], command.policy["workspace"], "workflow"))
            return Observation(child.get("state", "complete"))
        if command.kind == 'tracking':
            from .mailbox_workflows import MailboxSyncWorkflow
            try:
                await workflow.start_child_workflow(MailboxSyncWorkflow.run, command,
                    id=identity(command.policy['workspace'], 'mailbox-sync', 'primary'),
                    task_queue=task_queue(command.policy['queue_prefix'], command.policy['workspace'], 'workflow'),
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    parent_close_policy=workflow.ParentClosePolicy.ABANDON)
                return Observation('accepted')
            except WorkflowAlreadyStartedError:
                return Observation('already_running')
        # Each Schedule has its own durable overlap policy. No app scheduler,
        # background executor, supervisor retry loop, or global readiness check.
        cursor = command.cursor or None
        for _ in range(command.policy["pages_per_run"]):
            result = await operation(command, command.kind, cursor=cursor,
                once=command.kind in {"network_delivery"})
            if command.kind != "admission" or result.next_cursor is None:
                return result
            if result.next_cursor == cursor:
                from temporalio.exceptions import ApplicationError
                raise ApplicationError("Evidence scan did not advance", type="SourceContractError", non_retryable=True)
            cursor = result.next_cursor
        workflow.continue_as_new(Command(command.reference, command.policy, command.kind, cursor))


@workflow.defn(name="applypilot.dispatch.v2")
class DispatchWorkflow:
    """Enumerate business work; matching, buffering and retries belong to Temporal.

Children are acknowledged before continuing and ABANDONed on parent close so a
new scan/run cannot cancel accepted work. No app-side in-flight reservation set.
The database inputs store contains no task execution state.
"""
    def __init__(self):
        self.totals = {}

    @workflow.query
    def status(self) -> dict:
        return self.totals

    @workflow.run
    async def run(self, command: Command) -> dict:
        cursor = command.cursor
        self.totals = dict(command.totals)
        native_selection = workflow.patched("native-dispatch-selection-v1")
        if native_selection and command.selection_time is None:
            command.selection_time = workflow.now().timestamp()
        for _ in range(command.policy["pages_per_run"]):
            request = ActivityRequest(command.reference, "plan", cursor,
                options={"kind": command.kind, "page_size": command.policy["page_size"],
                         "job_phases": command.policy["job_phases"], "workspace": command.policy["workspace"],
                         "heartbeat_interval_seconds": command.policy["activities"]["maintenance"]["heartbeat_interval_seconds"]})
            if native_selection:
                request.options.update(native_selection=True,
                    selected=self.totals.get("selected", 0), selection_time=command.selection_time,
                    source_refresh_age_seconds=command.policy.get("source_refresh_age_seconds", 86400))
            page = await workflow.execute_activity("applypilot.plan.v1", request,
                result_type=WorkPage, **activity_options(command.policy, "plan"))
            for item in page.items:
                kind = item["kind"]
                if kind == 'target':
                    from .target_contracts import TargetCommand
                    from .target_workflows import TargetWorkflow
                    child = TargetWorkflow.run
                    argument = TargetCommand(item['reference'], command.policy)
                    entity_kind = 'target'
                elif kind == 'discover':
                    child = DiscoveryWorkflow.run
                    argument = Command(item['reference'], command.policy, kind)
                    entity_kind = 'discovery'
                else:
                    child = JobWorkflow.run
                    argument = EntityCommand(item['reference'], command.policy, item['subject'], kind)
                    entity_kind = 'job'
                try:
                    await workflow.start_child_workflow(child, argument,
                        id=identity(command.policy["workspace"], entity_kind, item["subject"]),
                        task_queue=task_queue(command.policy["queue_prefix"], command.policy["workspace"], "workflow"),
                        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                        parent_close_policy=workflow.ParentClosePolicy.ABANDON)
                    self.totals["started"] = self.totals.get("started", 0) + 1
                except WorkflowAlreadyStartedError:
                    self.totals["already_running"] = self.totals.get("already_running", 0) + 1
            if native_selection:
                # Already-running entities count toward the selection cap too.
                self.totals["selected"] = self.totals.get("selected", 0) + len(page.items)
            if page.next_cursor is None:
                return self.totals
            cursor = page.next_cursor
        # This is a history checkpoint, not a cap on discovery or job processing.
        if native_selection:
            workflow.continue_as_new(Command(command.reference, command.policy,
                command.kind, cursor, totals=self.totals, selection_time=command.selection_time))
        workflow.continue_as_new(Command(command.reference, command.policy,
            command.kind, cursor, totals=self.totals))


WORKFLOWS = [CompanyWorkflow, JobWorkflow, ApplicationWorkflow, DiscoveryWorkflow, ServiceWorkflow, DispatchWorkflow]
