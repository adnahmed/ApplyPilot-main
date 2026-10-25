"""Fixed owner-requested evidence and profile operations."""

PHASE_ROLES = {
    'referral_inventory': 'maintenance', 'referral_case_prepare': 'maintenance',
    'referral_case_revise': 'models', 'referral_prepare_finish': 'maintenance',
    'referral_discovery': 'network', 'referral_revise': 'models',
    'referral_reconcile': 'email', 'github_person': 'research',
    'impact_source_draft': 'models', 'impact_bundle_draft': 'models', 'impact_brief': 'models',
    'impact_dedup': 'models', 'impact_remeasure': 'models', 'impact_reconcile': 'models', 'evidence_prune': 'models',
    'impact_extract': 'models', 'impact_revise': 'models', 'suggest': 'models',
    'intake': 'documents', 'material_url': 'research', 'github_inventory': 'research',
    'github_repository': 'models', 'github_finish': 'maintenance',
}
OPERATIONS = frozenset(PHASE_ROLES) - {'github_inventory', 'github_repository', 'github_finish',
    'referral_inventory', 'referral_case_prepare', 'referral_case_revise', 'referral_prepare_finish'} | {
    'github', 'evidence_collect', 'referral_plan', 'referral_prepare'}
ONBOARDING_OPERATIONS = frozenset({'impact_extract', 'impact_revise', 'suggest', 'intake', 'material_url', 'github'})
