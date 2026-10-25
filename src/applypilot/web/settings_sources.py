"""Readable settings projections without changing learning or provider policy."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone


TASK_LABELS = {
    'agent_context': ('Applications', 'Prepare working context'),
    'application_agent': ('Applications', 'Complete applications'),
    'application_assist': ('Applications', 'Assist with applications'),
    'application_inspect': ('Applications', 'Inspect application pages'),
    'application_form': ('Applications', 'Answer application questions'),
    'browser_observation': ('Applications', 'Read browser pages'),
    'score': ('Discovery', 'Score job fit'),
    'signal_roles': ('Discovery', 'Identify hiring signals'),
    'detail_extract': ('Discovery', 'Extract job details'),
    'company_research': ('Discovery', 'Research companies'),
    'unattended_research_extract_person': ('Discovery', 'Research contacts'),
    'factual_review': ('Documents', 'Verify document claims'),
    'tailor_resume': ('Documents', 'Tailor résumés'),
    'cover_letter': ('Documents', 'Write cover letters'),
    'outreach_draft': ('Conversations', 'Draft outreach'),
    'network_reply': ('Conversations', 'Draft network replies'),
    'outcome_classify': ('Conversations', 'Recognize application outcomes'),
    'outcome_link': ('Conversations', 'Match replies to applications'),
    'interview_brief': ('Conversations', 'Prepare interview briefs'),
    'decline_feedback': ('Conversations', 'Request interview feedback'),
    'evidence_draft_draft_capability_claims': ('Evidence & impact', 'Extract project capabilities'),
    'evidence_draft_judge_entries_for_prune': ('Evidence & impact', 'Review project evidence'),
    'backup_diary_edit_revise': ('Evidence & impact', 'Revise work notes'),
    'impact_brief_generate_director_followups': ('Evidence & impact', 'Ask achievement follow-ups'),
    'impact_defense_llm_extract_signals': ('Evidence & impact', 'Extract achievement signals'),
    'impact_defense_semantic_job_conditioning': ('Evidence & impact', 'Relate achievements to roles'),
    'impact_draft_gate_skeletons': ('Evidence & impact', 'Review achievement outlines'),
    'impact_draft_llm_same_accomplishment': ('Evidence & impact', 'Find duplicate achievements'),
    'impact_draft_measurement_entailed': ('Evidence & impact', 'Verify achievement measurements'),
    'impact_draft_outcome_entailed': ('Evidence & impact', 'Verify achievement outcomes'),
    'impact_draft_repair_measurement_sources': ('Evidence & impact', 'Repair measurement references'),
    'impact_draft_run_keyed_stage': ('Evidence & impact', 'Develop achievement details'),
    'impact_draft_skeleton_pass': ('Evidence & impact', 'Draft achievement outlines'),
    'impact_matcher_extract_impact_intent_cached': ('Evidence & impact', 'Understand achievement intent'),
    'impact_matcher_semantic_match_records': ('Evidence & impact', 'Match supporting achievements'),
    'onboarding_planning_propose': ('Profile', 'Extract profile details'),
}


def learning_overview(status, bundle):
    """Inventory registered tasks and saved policies; never imply runtime compatibility."""
    from applypilot.reasoning.settings import DSPY_TASKS

    installed = defaultdict(list)
    for policy in bundle.get('revisions', []):
        if policy.get('state') == 'active':
            installed[policy['task']].append({**policy, 'source': 'Installed revision'})
    for policy in bundle.get('artifacts', []):
        installed[policy['task']].append({**policy, 'source': 'Saved instructions'})
    examples = defaultdict(int)
    for row in status.get('corpus', []):
        examples[row.get('task')] += int(row.get('examples') or 0)
    enabled = set(status.get('inference', {}).get('dspy_enabled_tasks', []))
    groups = defaultdict(list)
    policies = []
    for task in sorted(DSPY_TASKS):
        group, label = TASK_LABELS.get(task, ('Other tasks', task.replace('_', ' ').capitalize()))
        saved = installed.get(task, [])
        row = {'task': task, 'label': label, 'examples': examples[task],
               'enabled': task in enabled, 'installed_count': len(saved),
               'instruction_source': 'Installed' if saved else 'Built-in'}
        groups[group].append(row)
        for policy in saved:
            created = policy.get('created')
            if isinstance(created, (int, float)):
                created = datetime.fromtimestamp(created, timezone.utc).isoformat()
            policies.append({**policy, 'label': label, 'group': group, 'created': created})
    return {'groups': [{'label': group, 'tasks': sorted(rows, key=lambda row: row['label'])}
                       for group, rows in sorted(groups.items())],
            'installed': policies, 'task_count': len(DSPY_TASKS),
            'enabled_count': len(enabled & DSPY_TASKS), 'installed_task_count': len(installed)}
