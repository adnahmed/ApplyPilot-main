"""Evidence provider registry."""

from applypilot.evidence.base import EvidenceProvider, CandidateSource, RawSignals
from applypilot.evidence.github import GitHubProvider

PROVIDERS: dict[str, type[EvidenceProvider]] = {
    "github": GitHubProvider,
}


def get_provider(name: str) -> EvidenceProvider:
    if name not in PROVIDERS:
        raise ValueError(f"Unknown evidence provider: {name}. Available: {list(PROVIDERS.keys())}")
    return PROVIDERS[name]()
