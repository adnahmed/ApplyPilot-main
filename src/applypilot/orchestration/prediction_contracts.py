"""Prediction traversal in Temporal history; private research remains referenced."""
from dataclasses import dataclass, field
from typing import Any

from .company_contracts import CompanyPage


@dataclass
class PredictionSearch:
    reference: str
    provider: str


@dataclass
class PredictionPage:
    reference: str
    backend: str = 'http'
    request_reference: str = ''


@dataclass
class PredictionVacancyPage(CompanyPage):
    request_reference: str = ''


@dataclass
class PredictionCommand:
    reference: str
    policy: dict[str, Any]
    phase: str = 'prepare'
    pending: list[PredictionPage] = field(default_factory=list)
    seen: list[str] = field(default_factory=list)
    searches: list[PredictionSearch] = field(default_factory=list)
    homepage_observed: bool = False
    vacancies: list[PredictionVacancyPage] = field(default_factory=list)
    vacancy_seen: list[str] = field(default_factory=list)
