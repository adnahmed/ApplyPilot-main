"""Temporal owns prediction search, page traversal, and vacancy continuation."""
from temporalio import workflow

from .prediction_contracts import PredictionCommand, PredictionSearch, PredictionPage, PredictionVacancyPage
from .company_operations import operation


@workflow.defn(name='applypilot.company_prediction.v1')
class PredictiveCompanyWorkflow:
    @workflow.run
    async def run(self, command: PredictionCommand) -> dict:
        for _ in range(64):
            if command.phase == 'prepare':
                prepared = await operation(command, 'prediction_prepare')
                if prepared.get('state') == 'paused':
                    return prepared
                command.reference = prepared['reference']
                command.pending = [PredictionPage(prepared['homepage'])]
                command.vacancies = [PredictionVacancyPage(**page) for page in prepared['vacancies']]
                command.phase = 'research'
            elif command.phase == 'research':
                if command.searches:
                    search = command.searches[0]
                    observed = await operation(command, 'prediction_search', options={
                        'query_reference': search.reference, 'provider': search.provider})
                    command.searches.pop(0)
                    existing = {page.reference: page for page in command.pending}
                    references = list(dict.fromkeys([*observed['pages'], *existing]))
                    command.pending = [existing.get(ref, PredictionPage(ref)) for ref in references if ref not in command.seen]
                elif not command.pending:
                    command.phase = 'vacancies'
                else:
                    page = command.pending[0]
                    if page.reference in command.seen:
                        command.pending.pop(0)
                        continue
                    observed = await operation(command, 'prediction_read', options={
                        'page_reference': page.reference, 'home': not command.homepage_observed,
                        'backend': page.backend, 'request_reference': page.request_reference})
                    if observed.get('challenge_reference'):
                        page.backend = 'flaresolverr'
                        page.request_reference = observed['challenge_reference']
                        continue
                    command.pending.pop(0)
                    command.seen = sorted(set([*command.seen, *observed['identities']]))
                    command.homepage_observed = True
                    known = set(command.seen) | {item.reference for item in command.pending}
                    for ref in observed['pages']:
                        if ref not in known:
                            command.pending.append(PredictionPage(ref))
                            known.add(ref)
                    command.searches.extend(PredictionSearch(query, provider)
                        for query in observed['queries'] for provider in ('searxng', 'ddgs'))
            elif command.phase == 'vacancies':
                if not command.vacancies:
                    command.phase = 'analyze'
                    continue
                page = command.vacancies[0]
                if page.identity in command.vacancy_seen:
                    command.vacancies.pop(0)
                    continue
                observed = await operation(command, 'prediction_vacancy', options={
                    'page_reference': page.reference, 'depth': page.depth,
                    'backend': page.backend, 'request_reference': page.request_reference})
                if observed.get('challenge_reference'):
                    page.backend = 'flaresolverr'
                    page.request_reference = observed['challenge_reference']
                    continue
                command.reference = observed['reference']
                command.vacancies.pop(0)
                command.vacancy_seen.append(page.identity)
                known = set(command.vacancy_seen) | {item.identity for item in command.vacancies}
                following = []
                for item in observed['pages']:
                    if item['identity'] not in known:
                        following.append(PredictionVacancyPage(**item))
                        known.add(item['identity'])
                command.vacancies[0:0] = following
            elif command.phase == 'analyze':
                observed = await operation(command, 'prediction_analyze')
                command.reference, command.phase = observed['reference'], 'audit'
            elif command.phase == 'audit':
                observed = await operation(command, 'prediction_audit')
                command.reference, command.phase = observed['reference'], 'publish'
            elif command.phase == 'publish':
                return await operation(command, 'prediction_publish')
            else:
                raise ValueError('Unknown prediction Workflow phase')
            if workflow.info().is_continue_as_new_suggested():
                workflow.continue_as_new(command)
        workflow.continue_as_new(command)
