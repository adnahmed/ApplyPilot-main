"""Reference-only contracts for independently durable search providers."""
from __future__ import annotations
from dataclasses import dataclass, field
import math
import re

PROVIDERS = ('searxng', 'ddgs')


@dataclass(frozen=True)
class SearchRequest:
    query: str
    page: int = 1
    category: str = 'general'
    page_size: int = 50

    def validate(self):
        if not isinstance(self.query, str) or not self.query.strip() or len(self.query) > 1000:
            raise ValueError('Search query must contain 1..1000 characters')
        if type(self.page) is not int or self.page < 1:
            raise ValueError('Search page must be a positive integer')
        if self.category not in ('general', 'news'):
            raise ValueError('Unsupported search category')
        if type(self.page_size) is not int or not 1 <= self.page_size <= 50:
            raise ValueError('Search response page size must be between 1 and 50')
        return self


@dataclass(frozen=True)
class SearchActivityInput:
    request_id: str
    observation_id: str
    provider: str

    def validate(self):
        for key in (self.request_id, self.observation_id):
            if not isinstance(key, str) or not re.fullmatch('[a-f0-9]{64}', key):
                raise ValueError('Search identities must be opaque SHA-256 references')
        if self.provider not in PROVIDERS:
            raise ValueError('Unsupported search provider')
        return self


@dataclass(frozen=True)
class SearchEvidence:
    provider: str
    evidence_id: str
    count: int
    state: str = 'observed'


@dataclass(frozen=True)
class SearchCommand:
    request_id: str
    observation_id: str
    workspace: str
    workflow_queue: str
    research_queue: str
    providers: tuple[str, ...] = PROVIDERS
    attempt_seconds: float = 600

    def validate(self):
        if (not isinstance(self.providers, (tuple, list)) or not self.providers
                or any(not isinstance(p, str) for p in self.providers)
                or len(set(self.providers)) != len(self.providers)):
            raise ValueError('Choose unique search providers')
        for provider in self.providers:
            SearchActivityInput(self.request_id, self.observation_id, provider).validate()
        if not isinstance(self.workspace, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', self.workspace):
            raise ValueError('Invalid workspace identifier')
        if any(not isinstance(q, str) or not q.strip() or len(q) > 255
               for q in (self.workflow_queue, self.research_queue)):
            raise ValueError('Both workflow and research queues are required')
        from datetime import timedelta
        try:
            valid = type(self.attempt_seconds) in (int, float) and math.isfinite(self.attempt_seconds) and self.attempt_seconds > 0
            timedelta(seconds=self.attempt_seconds)
        except (TypeError, ValueError, OverflowError):
            valid = False
        if not valid:
            raise ValueError('Native Activity timeouts must be positive finite representable seconds')
        return self


@dataclass(frozen=True)
class SearchDispatchResult:
    workflow_ids: list[str] = field(default_factory=list)
    state: str = 'accepted'
