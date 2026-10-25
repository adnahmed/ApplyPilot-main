"""Provider restrictions and submission-rate facts, never browser worker ownership.

PARTIAL PART 2: these existing SQL-backed provider counters are intentionally
retained until native account pacing is implemented. They are a known remaining
migration seam, not a claim that account pacing has migrated.

The limits preserve the prior conservative LinkedIn policy. The intent check is
transactional so simultaneous Temporal workers cannot spend the same allowance.
A restriction returns a fact; only a Workflow timer decides when to try again.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta


class ProviderRestricted(RuntimeError):
    def __init__(self, reason, *, manual=False, retry_after_seconds=60):
        super().__init__(reason)
        self.reason = reason
        self.manual = manual
        self.retry_after_seconds = max(1, int(retry_after_seconds))


def enforce(attempt, *, conn, at_intent=False):
    from . import circuit_breaker
    blocked = circuit_breaker.target_status(attempt['target'], conn=conn)
    if blocked.get('open'):
        seconds = 60
        if blocked.get('retry_after'):
            until = datetime.fromisoformat(blocked['retry_after'])
            if until.tzinfo is None:
                until = until.replace(tzinfo=UTC)
            seconds = max(1, (until-datetime.now(UTC)).total_seconds())
        raise ProviderRestricted('provider_domain_restriction', manual=bool(blocked.get('manual_clear')),
                                 retry_after_seconds=seconds)
    if attempt.get('channel', attempt.get('application_route')) != 'linkedin-easy-apply':
        return
    row = conn.execute("SELECT manual_block_reason FROM linkedin_governor_state WHERE singleton_id=1").fetchone()
    if row and row['manual_block_reason']:
        raise ProviderRestricted('provider_account_manual_review', manual=True)
