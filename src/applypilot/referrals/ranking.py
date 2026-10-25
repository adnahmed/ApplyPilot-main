"""Explainable referral priorities. Scores are heuristics, NOT hiring probabilities."""
from __future__ import annotations

import math
import re

RELATIONSHIP_WEIGHT = {'knows_work': 1.0, 'professional': .8, 'acquaintance': .6, 'mutual': .4, 'cold': .15}
ALIGNMENT_WEIGHT = {'declared_same_team': 1.0, 'declared_same_function': .7,
                    'title_overlap_unverified': .3, 'different_function': .2, 'unknown': .2}


def unit(value, name):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f'{name} must be a finite number from 0 to 1')
    return float(value)


def evaluate(contact, job, *, job_value=1.0, mechanism_confidence=.5):
    family = str(job.get('role_family') or '').strip().casefold()
    team = str(job.get('referral_team') or '').strip().casefold()
    function = contact['role_function'].casefold().strip()
    same_team = bool(team and team == contact.get('team', '').casefold().strip())
    same_function = bool(family and family == function)
    def words(value):
        return set(re.findall(r'[a-z0-9]+', value.casefold())) - {'senior', 'lead', 'the', 'and', 'of', 'at'}
    overlap = sorted(words(str(job.get('title', ''))) & words(function))
    alignment = ('declared_same_team' if same_team else 'declared_same_function' if same_function
                 else 'title_overlap_unverified' if overlap else 'different_function' if family and function else 'unknown')
    relation = contact['relationship']
    aligned = same_team or same_function
    if relation == 'knows_work' and aligned:
        tier = 0
    elif relation == 'professional' and aligned:
        tier = 1
    elif (relation == 'acquaintance' and aligned) or relation == 'knows_work':
        tier = 2
    elif relation == 'mutual' and aligned:
        tier = 3
    elif relation != 'cold':
        tier = 4
    elif aligned:
        tier = 5
    else:
        tier = 6
    # Never make recruiters or introducers look like confirmed employee referrers.
    if contact['kind'] in {'recruiter', 'introducer'}:
        tier = max(tier, 4)
    factors = {'job_value': unit(job_value, 'job_value'),
               'candidate_fit': unit(float(job.get('fit_score') or 0) / 10, 'candidate_fit'),
               'relationship_quality': RELATIONSHIP_WEIGHT[relation],
               'function_alignment': ALIGNMENT_WEIGHT[alignment],
               'mechanism_confidence': unit(mechanism_confidence, 'mechanism_confidence')}
    score = round(100 * math.prod(factors.values()), 3)
    return {'contact_id': contact['id'], 'relationship': relation,
            'relationship_note': contact['relationship_note'], 'alignment': alignment, 'overlap': overlap,
            'source': contact['source'], 'contact_verified_at': contact['verified_at'], 'contact_kind': contact['kind'],
            'team': contact.get('team', ''), 'tier': tier, 'opportunity_score': score, 'factors': factors,
            'score_version': 1, 'score_interpretation': 'Priority heuristic, not a probability or causal uplift.',
            'hold_eligible': relation in {'knows_work', 'professional', 'acquaintance'}
                             and (aligned or relation == 'knows_work')
                             and contact['kind'] in {'employee', 'hiring_manager'}}
