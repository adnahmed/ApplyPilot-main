"""Impact Evidence Ledger.

Typed, JSON-backed store of the user's measured accomplishments
(problem → intervention → measurement → business outcome), plus the
matcher that turns them into immutable per-job claim packs and the
prompt integrations that consume them.

Scope (CLAUDE.md decision #65, 2026-09-01): stages 1 (ledger), 3 (matcher),
5 (impact validator). The full Impact Spec extractor (`jobs.impact_spec`) and
scorer outcome-fit remain deferred. Interview defense is now supported via
``applypilot impact brief <ID>`` (deterministic ``defense.py`` facts +
LLM-rendered ``brief.py`` answers/follow-ups) and — since 2026-09-02 — as a
first-class non-blocking pipeline stage (``run brief``) via
``impact/brief_stage.py`` for the apply cohort.
"""

from applypilot.impact import ledger, models  # noqa: F401
from applypilot.impact.ledger import (  # noqa: F401
    JsonImpactLedgerRepository,
    LedgerCorruptionError,
    LedgerStatus,
    LedgerValidationError,
    default_repository,
    repository_for_path,
)
from applypilot.impact.models import (  # noqa: F401
    ClaimPack,
    DecisionConsequence,
    ImpactIntent,
    ImpactRecord,
    InterviewEvidence,
    Measurement,
    MeasurementMethod,
    Outcome,
    Scope,
    SelectedEvidenceSnapshot,
    Tradeoff,
)

__version__ = "0.1.0"
# Model-backed helpers load only when requested. Opening/editing the diary needs
# neither a model client nor provider SDK imports.
_LAZY = {name: module for module, names in {
    "brief":("generate_director_followups","generate_interview_brief","render_defense_card"),
    "defense":("build_defense_facts",),
    "matcher":("build_evidence_pack_text","build_proof_core_text","extract_impact_intent","is_eligible","score_record","select_evidence"),
}.items() for name in names}

def __getattr__(name):
    import importlib
    if name in {"matcher","pipeline"}:
        value=importlib.import_module("applypilot.impact."+name)
    elif name in _LAZY:
        value=getattr(importlib.import_module("applypilot.impact."+_LAZY[name]),name)
    else: raise AttributeError(name)
    globals()[name]=value
    return value

__all__ = ["ledger", "models", "matcher", "pipeline", "JsonImpactLedgerRepository",
    "LedgerCorruptionError", "LedgerStatus", "LedgerValidationError", "default_repository", "repository_for_path",
    "ClaimPack", "DecisionConsequence", "ImpactIntent", "ImpactRecord", "InterviewEvidence", "Measurement",
    "MeasurementMethod", "Outcome", "Scope", "SelectedEvidenceSnapshot", "Tradeoff", *_LAZY]
