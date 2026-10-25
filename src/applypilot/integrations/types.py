"""Small, serializable contracts shared by discovery, research and the CLI."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any


class SourceError(RuntimeError):
    """An actionable, credential-free connector failure, never an empty success."""

    def __init__(self, message: str, kind: str = "configuration", retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_id(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def bounded_int(value: int, name: str, low: int = 1, high: int = 100) -> int:
    if type(value) is not int or not low <= value <= high:
        raise SourceError(f"{name} must be an integer from {low} to {high}")
    return value


@dataclass(frozen=True)
class SearchHit:
    url: str
    title: str
    snippet: str
    provider: str
    rank: int
    published_at: str | None = None
    fetched_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Document:
    url: str
    title: str
    markdown: str
    links: tuple[str, ...]
    provider: str
    fetched_at: str = field(default_factory=now_iso)
    structured_data: tuple[dict, ...] = ()
    truncated: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Document":
        data = dict(data)
        data["links"] = tuple(data.get("links", ()))
        data["structured_data"] = tuple(data.get("structured_data", ()))
        return cls(**data)
