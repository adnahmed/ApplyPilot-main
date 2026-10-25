"""Provider protocol for evidence sources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class CandidateSource:
    """A candidate evidence source (e.g., a GitHub repo)."""
    provider: str
    ref: str           # provider-specific locator (e.g., "adnahmed/repo")
    title: str
    url: str
    metadata: dict | None = None  # raw signals (stars, last push, languages, etc.)

    @property
    def sort_key(self) -> tuple:
        """Key for top-K selection: (recency desc, signal strength desc)."""
        # Default: sort by last_push_date descending, then stars descending
        last_push = self.metadata.get("last_push_date", "") if self.metadata else ""
        stars = self.metadata.get("stars", 0) if self.metadata else 0
        return (last_push, stars)


@dataclass
class RawSignals:
    """Raw signals fetched from a source, used to draft claims."""
    title: str
    description: str
    languages: list[str]
    topics: list[str]
    url: str
    last_activity: str
    extra: dict | None = None

    @property
    def observations(self) -> list[dict]:
        """Detached, revision-bound observations available to claim drafting."""
        rows = (self.extra or {}).get("observations") or []
        return [dict(row) for row in rows if isinstance(row, dict)]


class EvidenceProvider(Protocol):
    """Protocol for evidence providers."""

    name: str

    def discover(self, identity: str, limit: int | None = None, *, include_private: bool = False) -> list[CandidateSource]:
        """Discover candidate sources for a given identity (user/org/repo)."""
        ...

    def fetch(self, source: CandidateSource) -> RawSignals:
        """Fetch raw signals from a specific source."""
        ...
