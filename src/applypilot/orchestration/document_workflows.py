"""Document lifecycles carry stable input and artifact IDs, never file contents."""
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from applypilot.documents.contracts import (DocumentCommand, JobDocumentCommand,
    DocumentResult, PrepareDocument, PreparedDocument, RenderDocument)


def _validate(command):
    try:
        command.validate()
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise ApplicationError('Invalid document Workflow input',
                               type='DocumentInputError', non_retryable=True) from None


def _options(command, *, heartbeat=False):
    result = {'task_queue': command.task_queue,
              'start_to_close_timeout': timedelta(seconds=command.start_to_close_seconds),
              'retry_policy': RetryPolicy(non_retryable_error_types=['DocumentInputError'])}
    if heartbeat:
        result['heartbeat_timeout'] = timedelta(seconds=command.heartbeat_seconds)
    return result


@workflow.defn(name='applypilot.document.v1')
class DocumentWorkflow:
    @workflow.run
    async def run(self, command: DocumentCommand) -> DocumentResult:
        _validate(command)
        return await workflow.execute_activity('applypilot.render_document.v1',
            RenderDocument(command.document_id, command.doc_format, command.heartbeat_interval_seconds),
            result_type=DocumentResult, **_options(command, heartbeat=True))


@workflow.defn(name='applypilot.job_document.v1')
class JobDocumentWorkflow:
    @workflow.run
    async def run(self, command: JobDocumentCommand) -> DocumentResult:
        _validate(command)
        prepared = await workflow.execute_activity('applypilot.prepare_document.v1',
            PrepareDocument(command.job_reference, command.stage), result_type=PreparedDocument,
            **_options(command))
        if prepared.document_id is None:
            return DocumentResult('', prepared.state)
        # An independent child keeps the approved generation's own durable
        # lifecycle. Retried rendering cannot regenerate applicant text.
        return await workflow.execute_child_workflow(DocumentWorkflow.run,
            DocumentCommand(prepared.document_id, prepared.doc_format, command.task_queue,
                command.start_to_close_seconds, command.heartbeat_seconds, command.heartbeat_interval_seconds),
            id=workflow.info().workflow_id + ':render:' + prepared.document_id)


DOCUMENT_WORKFLOWS = [DocumentWorkflow, JobDocumentWorkflow]
