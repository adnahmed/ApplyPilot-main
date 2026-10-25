"""Owner-facing recovery routes based on observed source failures."""
from __future__ import annotations


LABELS = {'ddgs': 'Web search', 'playwright': 'Public-page browser', 'searxng': 'SearXNG search',
          'twikit': 'X · Twikit', 'linkedin_scraper': 'LinkedIn · Browser reader',
          'linkedin_rs': 'LinkedIn · Native reader', 'freehire': 'FreeHire'}


def source_check_view(check):
    row = {**check, 'label': LABELS.get(check.get('component'), check.get('component', 'Source'))}
    if check.get('ok') and not check.get('skipped'):
        return row
    source = check.get('component')
    code = check.get('error_code', '')
    social = 'x' if source == 'twikit' else 'linkedin' if source in {'linkedin_scraper', 'linkedin_rs'} else None
    if social:
        name = 'X' if social == 'x' else 'LinkedIn'
        row.update(action_href='/ui/settings?section=connections#connection-' + social,
                   action_label='Open ' + name + ' connection')
        if check.get('skipped'):
            row['next_action'] = 'Connect your ' + name + ' account to enable this optional reader.'
        elif code in {'auth', 'connector_auth', 'identity_mismatch'}:
            row['next_action'] = ('Reconnect ' + name + ', complete sign-in or the account challenge in the browser, '
                                  'then check the connection again.')
        elif code == 'rate_limit':
            row['next_action'] = 'Wait for the service limit to reset, then check again. A new login does not reset rate limits.'
        elif code in {'configuration', 'unavailable', 'dependency'}:
            row['next_action'] = ('Review the saved account and reader configuration. Connecting ' + name
                                  + ' saves a session for its compatible readers.')
        elif code == 'access_denied':
            row['next_action'] = 'Open ' + name + ' in your browser and review account restrictions before checking this reader again.'
        elif code == 'schema':
            row['next_action'] = 'The reader could not handle the service response. Review the reader diagnostics and compatibility; reconnecting does not repair a reader format error.'
        else:
            row['next_action'] = ('Check ' + name + ' access in your browser, then retry this reader. '
                                  'Reconnect only if the account asks you to sign in; this failure does not confirm an expired login.')
    else:
        row.update(action_href='/ui/settings?section=connections#form-connections-search-and-profile-readers',
                   action_label='Review source settings',
                   next_action='Review the selected search service and its address, then check again.')
        if source == 'freehire':
            row.update(action_href='/ui/settings?section=connections#form-connections-freehire', action_label='Review FreeHire settings',
                       next_action='Review the FreeHire token and service address, then check again.')
    return row
