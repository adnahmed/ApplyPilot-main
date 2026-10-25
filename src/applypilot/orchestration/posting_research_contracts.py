"""Posting research execution state; private queries and documents stay referenced."""
from dataclasses import dataclass, field
from typing import Any

from .company_contracts import CompanyPage


@dataclass
class ResearchSearch:
    reference: str
    provider: str


@dataclass
class ResearchPage:
    reference: str
    backend: str
    request_reference: str = ""


@dataclass
class SignalPage(CompanyPage):
    backend: str = "http"
    request_reference: str = ""


@dataclass
class PostingResearchCommand:
    reference: str
    policy: dict[str, Any]
    entity_id: str = ""
    kind: str = "company_research"
    phase: str = "prepare"
    searches: list[ResearchSearch] = field(default_factory=list)
    pending: list[ResearchPage] = field(default_factory=list)
    seen: list[str] = field(default_factory=list)
    max_pages: int | None = None
    crawler: str = "http"
    signal_pages: list[SignalPage] = field(default_factory=list)


@dataclass
class SignalVacancyCommand:
    reference: str
    policy: dict[str, Any]
    pending: list[SignalPage] = field(default_factory=list)
    seen: list[str] = field(default_factory=list)
    observed: bool = False
