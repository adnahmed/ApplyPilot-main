"""Durable rolling LinkedIn limits; submission workers never wait for a quota."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError
from .contracts import ActivityRequest, ApplicationAccountCommand, AccountSubmission, Observation


@workflow.defn(name='applypilot.linkedin-applications.v1')
class ApplicationAccountWorkflow:
    def __init__(self):
        self.pending = []
        self.handled = {}
        self.active = ''
        self.cancelled = set()
        self.dispatch = None
        self.pacing = {}
        self.pacing_revision = 0
        self.provider_available_at = 0.0
        self.provider_observations = []
        self.configured = False

    @workflow.update
    def set_pacing(self, pacing: dict) -> dict:
        self._validate_pacing(pacing)
        self.pacing = {**self.pacing, **pacing}
        self.pacing_revision += 1
        return dict(self.pacing)

    @set_pacing.validator
    def _validate_pacing(self, pacing: dict) -> None:
        import math
        required = {'spacing_seconds', 'hourly_limit', 'daily_limit'}
        if not isinstance(pacing, dict) or not required <= set(pacing) or set(pacing) - required - {'restriction_retry_seconds'}:
            raise ValueError('Provide spacing_seconds, hourly_limit and daily_limit')
        if (type(pacing['spacing_seconds']) not in (int, float) or
                not math.isfinite(pacing['spacing_seconds']) or pacing['spacing_seconds'] <= 0):
            raise ValueError('Spacing must be positive and finite')
        if any(type(pacing[key]) is not int or pacing[key] <= 0 for key in ('hourly_limit', 'daily_limit')):
            raise ValueError('Account limits must be positive integers')
        if 'restriction_retry_seconds' in pacing:
            value = pacing['restriction_retry_seconds']
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError('Restriction recheck delay must be positive and finite')

    @workflow.signal
    def provider_restriction(self, observation: dict):
        import math
        if not isinstance(observation, dict):
            return
        stamp, delay = observation.get('observed_at'), observation.get('retry_after_seconds')
        reference = observation.get('evidence_reference', '')
        if (observation.get('workflow_id') != workflow.info().workflow_id or
                type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp <= 0 or
                not isinstance(reference, str) or len(reference) != 64 or
                any(char not in '0123456789abcdef' for char in reference) or
                (delay is not None and (type(delay) not in (int, float) or not math.isfinite(delay) or delay <= 0))):
            workflow.logger.warning('Ignored invalid or misrouted provider observation')
            return
        if not self.configured:
            self.provider_observations.append(observation)
            return
        until = stamp + (delay if delay is not None else self.pacing['restriction_retry_seconds'])
        if not math.isfinite(until) or until > 253402300799:
            workflow.logger.warning('Ignored provider deadline outside the supported timestamp range')
            return
        if until > self.provider_available_at:
            self.provider_available_at = until
            self.pacing_revision += 1

    @workflow.signal
    def enqueue(self, submission: AccountSubmission):
        attempt = submission.command.attempt_id
        if attempt != self.active and all(
                item.command.attempt_id != attempt for item in self.pending):
            self.pending.append(submission)

    @workflow.signal
    def cancel_attempt(self, attempt_id: str):
        self.cancelled.add(attempt_id)
        if self.active == attempt_id and self.dispatch is not None:
            self.dispatch.cancel()

    @workflow.query
    def status(self) -> dict:
        return {'pending': len(self.pending), 'active_attempt': self.active, 'pacing': dict(self.pacing),
                'provider_available_at': self.provider_available_at}

    @workflow.run
    async def run(self, command: ApplicationAccountCommand):
        from .workflows import activity_options, operation
        by_attempt = {item.command.attempt_id: item for item in [*command.pending, *self.pending]}
        self.pending = list(by_attempt.values())
        self.handled.update(command.handled)
        self.cancelled.update(command.cancelled)
        if not self.pacing:
            self.pacing = {key: command.policy['linkedin_applications'][key]
                           for key in ('spacing_seconds', 'hourly_limit', 'daily_limit')}
        self.pacing.setdefault('restriction_retry_seconds',
            command.policy['linkedin_applications'].get('restriction_retry_seconds', 86400))
        self.provider_available_at = max(self.provider_available_at, command.provider_available_at)
        self.configured = True
        for observation in self.provider_observations:
            self.provider_restriction(observation)
        self.provider_observations = []
        if not command.initialized:
            cfg = command.policy['activities']['maintenance']
            history = await workflow.execute_activity('applypilot.operation.v1',
                ActivityRequest('', 'application_account_history', options={
                    'heartbeat_interval_seconds': cfg['heartbeat_interval_seconds'],
                    'observed_after': workflow.time() - 86400,
                    'account_workflow_id': workflow.info().workflow_id}),
                result_type=Observation, **activity_options(command.policy, 'application_account_history'))
            command.submissions = history.detail['submissions']
            self.provider_available_at = max(self.provider_available_at, history.detail.get('provider_available_at', 0))
            for observation in history.detail.get('provider_observations', []):
                self.provider_restriction(observation)
            command.initialized = True
        while True:
            if workflow.cancellation_reason() is not None:
                raise asyncio.CancelledError('Account Workflow cancelled')
            await workflow.wait_condition(lambda: bool(self.pending))
            now = workflow.time()
            pacing = self.pacing
            revision = self.pacing_revision
            command.submissions = sorted(t for t in command.submissions if t > now - 86400)
            self.handled = {key: stamp for key, stamp in self.handled.items() if stamp > now - 86400}
            hourly = [stamp for stamp in command.submissions if stamp > now - 3600]
            delays = [0.0, self.provider_available_at - now]
            if command.submissions:
                delays.append(command.submissions[-1] + pacing['spacing_seconds'] - now)
            if len(hourly) >= pacing['hourly_limit']:
                delays.append(hourly[-pacing['hourly_limit']] + 3600 - now)
            if len(command.submissions) >= pacing['daily_limit']:
                delays.append(command.submissions[-pacing['daily_limit']] + 86400 - now)
            attempt = self.pending[0].command.attempt_id
            if attempt not in self.handled and attempt not in self.cancelled and max(delays) > 0:
                try:
                    await workflow.wait_condition(lambda: attempt in self.cancelled or self.pacing_revision != revision,
                        timeout=timedelta(seconds=max(delays)))
                except TimeoutError:
                    pass
                continue
            item = self.pending.pop(0)
            attempt = item.command.attempt_id
            if attempt not in self.handled:
                self.active = attempt
                if attempt not in self.cancelled:
                    command.submissions.append(workflow.time())
                    self.dispatch = asyncio.create_task(operation(item.command, 'application_attempt'))
                    try:
                        result = await self.dispatch
                        if observation := result.detail.get('provider_restriction'):
                            self.provider_restriction(observation)
                    except ActivityError as exc:
                        if isinstance(exc.cause, CancelledError) and attempt not in self.cancelled:
                            raise
                        # The application entity reconciles uncertainty before any new attempt.
                        pass
                    except asyncio.CancelledError:
                        if attempt not in self.cancelled:
                            raise
                    finally:
                        self.dispatch = None
                    command.submissions[-1] = workflow.time()
                self.handled[attempt] = workflow.time()
                self.cancelled.discard(attempt)
                self.active = ''
            try:
                await workflow.get_external_workflow_handle(item.requester).signal(
                    'account_dispatch_completed', attempt)
            except ApplicationError:
                pass  # A closed application cannot authorize another submission.
            if workflow.info().is_continue_as_new_suggested():
                command.pending = self.pending
                command.handled = self.handled
                command.cancelled = sorted(self.cancelled)
                command.provider_available_at = self.provider_available_at
                command.policy = {**command.policy, 'linkedin_applications': {
                    **command.policy['linkedin_applications'], **self.pacing}}
                workflow.continue_as_new(command)


ACCOUNT_WORKFLOWS = [ApplicationAccountWorkflow]
