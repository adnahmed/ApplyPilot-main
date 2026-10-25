"""Owner-target research state carried exclusively in Temporal history."""
from dataclasses import dataclass, field
from typing import Any

TARGET_ROLES = {'target_prepare': 'maintenance', 'target_search': 'research',
                'target_read': 'research', 'target_synthesize': 'models',
                'target_vacancy': 'discovery', 'target_publish': 'maintenance',
                'target_complete': 'maintenance'}


@dataclass
class TargetPage:
    reference: str
    identity: str
    depth: int = 0
    backend: str = 'http'


@dataclass
class TargetCommand:
    reference: str
    policy: dict[str, Any]
    token: str = ''
    phase: str = 'prepare'
    query_reference: str = ''
    searches: list[str] = field(default_factory=list)
    reads: list[TargetPage] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    read_seen: list[str] = field(default_factory=list)
    pending: list[TargetPage] = field(default_factory=list)
    seen: list[str] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    publication_reference: str = ''
    found: int = 0
    new: int = 0
    coverage_clean: bool = True
