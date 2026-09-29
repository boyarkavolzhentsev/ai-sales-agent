"""Pure DNC evaluation. No database access: entries are passed in."""

from collections.abc import Iterable
from datetime import datetime

from pydantic import AwareDatetime

from app.core.enums import DNCReason, DNCScope
from app.core.models import DoNotContactEntry
from app.core.models.base import CoreModel
from app.core.models.types import EntityId, NonEmptyStr
from app.core.validation import normalize_domain, normalize_email
from app.policy.models import PolicyReason


class SuppressionMatch(CoreModel):
    """The DNC entry that blocks contacting an address."""

    entry_id: EntityId
    scope: DNCScope
    value: NonEmptyStr
    reason: DNCReason
    expires_at: AwareDatetime | None = None

    @property
    def policy_reason(self) -> PolicyReason:
        return PolicyReason.DNC_EMAIL if self.scope is DNCScope.EMAIL else PolicyReason.DNC_DOMAIN


def is_entry_in_force(entry: DoNotContactEntry, now: datetime) -> bool:
    """An entry blocks until it expires; ``expires_at <= now`` means inactive.

    ``created_at`` is deliberately not compared with ``now``: an entry that appears to be
    from the future (clock skew) still blocks, which is the safe direction.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return entry.expires_at is None or entry.expires_at > now


def evaluate_suppression(
    email: str,
    domain: str | None,
    entries: Iterable[DoNotContactEntry],
    now: datetime,
) -> SuppressionMatch | None:
    """Return the blocking match for ``email``, or None.

    Checked scopes: EMAIL on the normalized address; DOMAIN on the email's own domain
    and, if given, on ``domain`` (e.g. the company domain). When several entries match,
    the most specific wins (EMAIL over DOMAIN), then the most restrictive (permanent
    over expiring, later expiry over earlier), then ``entry_id`` for determinism.
    """
    normalized_email = normalize_email(email)
    domains = {normalized_email.split("@", 1)[1]}
    if domain is not None:
        domains.add(normalize_domain(domain))

    matches: list[DoNotContactEntry] = []
    for entry in entries:
        if not is_entry_in_force(entry, now):
            continue
        if entry.scope is DNCScope.EMAIL and entry.value == normalized_email:
            matches.append(entry)
        elif entry.scope is DNCScope.DOMAIN and entry.value in domains:
            matches.append(entry)
    if not matches:
        return None
    best = min(matches, key=_specificity_key)
    return SuppressionMatch(
        entry_id=best.entry_id,
        scope=best.scope,
        value=best.value,
        reason=best.reason,
        expires_at=best.expires_at,
    )


def _specificity_key(entry: DoNotContactEntry) -> tuple[int, int, float, str]:
    scope_rank = 0 if entry.scope is DNCScope.EMAIL else 1
    if entry.expires_at is None:
        return (scope_rank, 0, 0.0, entry.entry_id)
    return (scope_rank, 1, -entry.expires_at.timestamp(), entry.entry_id)
