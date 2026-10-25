"""One immutable configuration/applicant/runtime snapshot per discovery cycle."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from uuid import uuid4

from .models import ApplicantSnapshot, SearchIntent


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class DiscoveryContext:
    intent: SearchIntent
    applicant: ApplicantSnapshot
    runtime: Mapping
    cycle_id: str
    reserve_budget: Callable | None = field(default=None, repr=False, compare=False)
    reserve_budgets: Callable | None = field(default=None, repr=False, compare=False)
    capture_signal: Callable | None = field(default=None, repr=False, compare=False)
    llm_json: Callable | None = field(default=None, repr=False, compare=False)
    failed_task: object | None = field(default=None, repr=False, compare=False)
    recovery_authorization: Mapping | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        from applypilot.config import validate_discovery_runtime

        if not isinstance(self.intent, SearchIntent) or not isinstance(self.applicant, ApplicantSnapshot):
            raise TypeError("DiscoveryContext requires compiled intent and applicant snapshots")
        if not isinstance(self.cycle_id, str) or not self.cycle_id.strip():
            raise ValueError("cycle_id must be a nonempty string")
        object.__setattr__(self, "runtime", _freeze(validate_discovery_runtime(_thaw(self.runtime))))
        if self.recovery_authorization is not None:
            object.__setattr__(self, "recovery_authorization", _freeze(self.recovery_authorization))


def build_context(raw_config=None, profile=None, runtime=None, *, sources=None) -> DiscoveryContext:
    """Read each omitted input once, compile once; no bootstrap, sync or DB IO."""
    from applypilot.config import (
        load_compiled_intent, load_discovery_runtime, read_discovery_config,
    )

    raw = read_discovery_config() if raw_config is None else raw_config
    intent, applicant = load_compiled_intent(raw, profile=profile)
    if sources is not None:
        from .intent import KNOWN_SOURCES, _resolve_source_alias, compile_source_selection

        selected, boards = compile_source_selection({"sites": sources})
        # CLI selectors narrow the configured portfolio, never enable a disabled board.
        has_boards = any(_resolve_source_alias(str(name).strip().lower()) not in KNOWN_SOURCES
                         for name in sources)
        intent = replace(intent,
            sources_enabled=tuple(s for s in intent.sources_enabled if s in selected),
            jobspy_sites=tuple(b for b in intent.jobspy_sites if not has_boards or b in boards))
    policy = load_discovery_runtime(runtime, legacy_defaults=raw.get("defaults", {}))
    from applypilot.targets.intent import load_snapshot
    intent = replace(intent, target_policies_json=load_snapshot())
    return DiscoveryContext(intent, applicant, policy, uuid4().hex)
