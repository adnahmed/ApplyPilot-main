"""Independent signal lifecycle; a model failure cannot hold a provider reader."""
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from applypilot.signals.contracts import SignalCommand, SignalInput, SignalResult


@workflow.defn(name='applypilot.signal.v1')
class SignalWorkflow:
    def __init__(self):
        self.state = 'captured'

    @workflow.query
    def status(self) -> str:
        return self.state

    @workflow.run
    async def run(self, command: SignalCommand) -> SignalResult:
        from temporalio.exceptions import ApplicationError
        try:
            command.validate()
        except (TypeError, ValueError):
            raise ApplicationError('Invalid signal Workflow input', type='InvalidSignalInput', non_retryable=True) from None
        request = SignalInput(command.signal_id, command.heartbeat_interval_seconds)
        request.validate()
        self.state = 'extracting'
        options = {'heartbeat_timeout': timedelta(seconds=command.heartbeat_seconds),
                   'retry_policy': RetryPolicy(non_retryable_error_types=['InvalidSignalInput'])}
        await workflow.execute_activity('applypilot.extract_signal.v1', request,
            result_type=SignalResult, task_queue=command.models_queue,
            start_to_close_timeout=timedelta(seconds=command.model_attempt_seconds), **options)
        self.state = 'publishing'
        result = await workflow.execute_activity('applypilot.publish_signal.v1', request,
            result_type=SignalResult, task_queue=command.discovery_queue,
            start_to_close_timeout=timedelta(seconds=command.publish_attempt_seconds), **options)
        self.state = result.state
        return result


SIGNAL_WORKFLOWS = [SignalWorkflow]
