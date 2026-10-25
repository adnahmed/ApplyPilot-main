"""User-facing controls for search intent; YAML stays a storage detail."""


def search_forms(saved):
    from .forms import field, form
    from applypilot.discovery.intent import compile_search_intent
    from applypilot.discovery.capabilities import CAPABILITIES, JOBSPY_BOARDS
    from applypilot.onboarding.search_preferences import market_entries
    from applypilot.onboarding.storage import digest

    config = saved.get('searches') or {}
    intent = compile_search_intent(config)
    queries = [{'_index': str(i), 'query': row if isinstance(row, str) else row.get('query', ''),
                'tier': str(1 if isinstance(row, str) else row.get('tier', 1)),
                'exclude': [] if isinstance(row, str) else row.get('exclude', [])}
               for i, row in enumerate(config.get('queries', []))]
    priorities = sorted({1, 2, 3, *(int(row['tier']) for row in queries)})
    priority_labels = {1: 'High', 2: 'Normal', 3: 'Explore'}
    query_columns = [field('query', 'Role or keywords', required=True),
                     field('tier', 'Priority', '1', 'select', [(str(tier), priority_labels.get(tier, f'Priority {tier}')) for tier in priorities]),
                     field('exclude', 'Exclude these words', '', 'tokens')]
    markets = [{'_index': str(i), **{key: row.get(key, [] if key != 'label' else '')
                for key in ('label', 'countries', 'regions', 'cities', 'work_modes')}} for i, row in enumerate(market_entries(config))]
    market_columns = [field('label', 'Market identifier', required=True, help='For example, uk-remote. Spaces become hyphens; renaming keeps linked preferences.'), field('countries', 'Countries', [], 'list', help='Country names or two-letter codes.'),
                      field('cities', 'Cities (optional)', [], 'list'), field('regions', 'Regions (optional)', [], 'list', help='Use global for worldwide roles.'),
                      field('work_modes', 'Work arrangements', [], 'multiselect', [('remote', 'Remote'), ('hybrid', 'Hybrid'), ('onsite', 'On site')])]
    market_options = [(str(row.label), str(row.label).replace('-', ' ').capitalize()) for row in intent.markets]
    preference_rows = []
    for index, rule in enumerate(config.get('preferences', [])):
        match = rule.get('match') or {}
        preference_rows.append({'_index': str(index), 'id': rule['id'],
            **{key: match.get(key) or [] for key in ('markets', 'engagements', 'employer_regions')},
            **{key: match.get(key) or 'any' for key in ('employer_scope', 'pay_basis', 'eligible_scope')}})
    preference_columns = [
        field('id', 'Preference name', required=True, help='Give this preference a short name. Spaces become hyphens.'),
        field('markets', 'Markets to match', [], 'multiselect', market_options, required=True),
        field('employer_scope', 'Employer location', 'any', 'select',
              [('any', 'Any location'), ('foreign', 'Outside my country'), ('domestic', 'In my country')]),
        field('engagements', 'How I would be hired', [], 'multiselect',
              [('contractor', 'Independent contractor'), ('eor', 'Through an employer of record'),
               ('direct_employee', 'Direct employee')], help='Leave empty to accept any arrangement.'),
        field('pay_basis', 'How pay is set', 'any', 'select',
              [('any', 'Either pay approach'), ('global', 'Global pay bands'), ('localized', 'Adjusted to my location')]),
        field('employer_regions', 'Employer regions (optional)', [], 'list',
              help='Use region names from your search strategy, one per line. Leave empty for any region.'),
        field('eligible_scope', 'Where the employer can hire', 'any', 'select',
              [('any', 'Any verified hiring area'), ('worldwide', 'Worldwide hiring')]),
    ]
    defaults = config.get('defaults') or {}
    exploration = config.get('exploration') or {}
    source_names = list(intent.sources_enabled)
    specifications = [
        form('search-queries', 'Roles and keywords', 'setup/save-searches', [
            field('preferences.queries', 'Search queries', queries, 'rows', columns=query_columns, add_label='Add a search')],
             note='Edit, prioritize, or remove searches. Exclusions apply to that search.'),
        form('search-markets', 'Where you want to work', 'setup/save-searches', [
            field('preferences.markets', 'Markets', markets, 'rows', columns=market_columns, add_label='Add a market')],
             note='Search geography is separate from your work authorization.'),
        form('search-preferences', 'Which opportunities you prefer', 'setup/save-searches', [
            field('preferences.preference_rules', 'Your preferences, most important first', preference_rows, 'rows',
                  columns=preference_columns, add_label='Add a preference')],
             note='Move preferences up or down to set their priority. Each matches its selected markets and hiring conditions. These preferences rank opportunities; they do not establish work authorization.'),
        form('search-sources', 'Discovery preferences', 'setup/save-searches', [
            field('preferences.sources.enabled', 'Search these sources', source_names, 'multiselect', [(key, key.replace('_', ' ').title()) for key in CAPABILITIES]),
            field('preferences.jobspy_sites', 'Job boards through JobSpy', list(intent.jobspy_sites), 'multiselect', [(key, key.replace('_', ' ').title()) for key in JOBSPY_BOARDS], help='Used when JobSpy is selected above. Existing board restrictions are preserved.'),
            field('preferences.location_labels', 'Active markets', list(intent.active_market_labels), 'multiselect', market_options),
            field('preferences.defaults.hours_old', 'Maximum posting age (hours)', defaults.get('hours_old', 72), 'number', min=1, step=1),
            field('preferences.defaults.results_per_site', 'Results per source', defaults.get('results_per_site', -1), 'limit'),
            field('preferences.exclude_titles', 'Exclude job titles containing', config.get('exclude_titles', []), 'list')]),
        form('search-exploration', 'Explore beyond your main searches', 'setup/save-searches', [
            field('preferences.exploration.discovery_share', 'Discovery used for exploration (%)', exploration.get('discovery_share', 0) * 100, 'percent', min=0, max=100, step=1),
            field('preferences.exploration.application_share', 'Applications used for exploration (%)', exploration.get('application_share', 0) * 100, 'percent', min=0, max=100, step=1),
            field('preferences.probes', 'Exploration keywords', [row.get('query', '') if isinstance(row, dict) else row for row in config.get('probes', [])], 'list')]),
    ]
    for specification in specifications:
        specification['payload']['preferences_revision'] = digest(config)
    return specifications
