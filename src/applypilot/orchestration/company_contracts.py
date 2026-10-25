"""Serializable company traversal state; private page data stays behind references."""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CrawlPage:
    reference: str
    depth: int
    backend: str
    previous_reference: str = ""


@dataclass
class CompanyCrawlCommand:
    reference: str
    policy: dict[str, Any]
    prepared: bool = False
    backend: str = "http"
    allow_browseros: bool = False
    max_pages: int = 1
    max_depth: int = 0
    pending: list[CrawlPage] = field(default_factory=list)
    seen: list[str] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)


@dataclass
class CompanyPage:
    reference: str
    identity: str = ""
    depth: int = 0
    backend: str = 'http'


@dataclass
class IssuerObservation:
    reference: str
    issuer: str
    operation: str
    provider: str = ""
    backend: str = 'http'


@dataclass
class CompanySourceCommand:
    reference: str
    policy: dict[str, Any]
    initialized: bool = False
    phase: str = "read"
    pending: list[CompanyPage] = field(default_factory=list)
    current: CompanyPage | None = None
    seen: list[str] = field(default_factory=list)
    enrichment: list[IssuerObservation] = field(default_factory=list)
    enrichment_seen: list[str] = field(default_factory=list)
    identified_issuers: list[str] = field(default_factory=list)
    companies: int = 0


@dataclass
class CompanyEvidenceCommand:
    reference: str
    policy: dict[str, Any]
    prepared: bool = False
    pending: list[CompanyPage] = field(default_factory=list)
    seen: list[str] = field(default_factory=list)
    backend: str = 'http'
