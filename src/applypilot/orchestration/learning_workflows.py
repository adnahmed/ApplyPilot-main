"""Independent program learning lifecycles; all model/corpus work is in Activities."""
from __future__ import annotations

from temporalio import workflow
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from .contracts import Command, identity, task_queue
from .workflows import operation


@workflow.defn(name="applypilot.learning-run.v1")
class LearningRunWorkflow:
    def __init__(self):
        self.phase = "created"
        self.identities = {}

    @workflow.query
    def status(self) -> dict:
        return {"phase": self.phase, **self.identities}

    @workflow.run
    async def run(self, command: Command) -> dict:
        self.phase = "building_dataset"
        dataset = await operation(command, "learning_build_dataset")
        if dataset.state != "dataset_ready":
            return {"state": dataset.state, **dataset.detail}
        self.identities = {key: dataset.detail[key] for key in
            ("learning_run_id", "dataset_id", "program_id", "optimizer_run_id")}
        run = Command(dataset.detail["reference"], command.policy, "learning_run", token=command.token)
        for phase in ("learning_evaluate_current", "learning_optimize",
                      "learning_evaluate_candidate", "learning_publish"):
            self.phase = phase
            result = await operation(run, phase)
            if result.state == "runtime_changed":
                break
        self.phase = result.state
        return {"state": result.state, **self.identities, **result.detail}


@workflow.defn(name="applypilot.learning.v1")
class LearningWorkflow:
    """A failed program cannot prevent discovery of unrelated learning cohorts."""
    @workflow.run
    async def run(self, command: Command) -> dict:
        command.token = command.token or workflow.uuid4().hex
        if command.kind != "learning_collection":
            collected = await operation(command, "learning_collect")
            if collected.state != "collected":
                return {"state": collected.state}
            command.reference = collected.detail["reference"]
            command.kind = "learning_collection"
            command.cursor = "wait" if collected.detail.get("wait_for_result") else ""
        while True:
            cohort = await operation(command, "learning_build_dataset", sequence=command.page)
            if cohort.state == "complete":
                return {"state": "accepted", "cohorts": command.page, "outcomes": command.totals}
            child = Command(cohort.detail["reference"], command.policy, "learning_cohort", token=command.token)
            options = dict(id=identity(command.policy["workspace"], "learning-program", cohort.detail["program_scope"]),
                task_queue=task_queue(command.policy["queue_prefix"], command.policy["workspace"], "workflow"),
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                parent_close_policy=workflow.ParentClosePolicy.ABANDON)
            if command.cursor == "wait":
                # Explicit imported evaluations never promote, and must retain
                # their own dataset even if automatic learning is already active.
                options['id'] = identity(command.policy['workspace'], 'learning-import', cohort.detail['learning_run_id'])
                return await workflow.execute_child_workflow(LearningRunWorkflow.run, child, **options)
            try:
                await workflow.start_child_workflow(LearningRunWorkflow.run, child, **options)
                state = "started"
            except WorkflowAlreadyStartedError:
                state = "already_running"
            command.totals[state] = command.totals.get(state, 0) + 1
            command.page += 1
            if command.page % command.policy["pages_per_run"] == 0 or workflow.info().is_continue_as_new_suggested():
                workflow.continue_as_new(command)


LEARNING_WORKFLOWS = [LearningWorkflow, LearningRunWorkflow]
