"""Explicit V1 sending limits for three scopes: global, mailbox and campaign.

A ``None`` scoped value means "no additional cap at this scope"; the global limits
always apply. A scoped value may never be looser than the global one.
"""

from datetime import timedelta
from typing import Self

from pydantic import NonNegativeInt, model_validator

from app.core.models import Campaign
from app.core.models.base import CoreModel
from app.core.models.types import EmailAddress, EntityId, NonEmptyStr
from app.core.validation import unique_items
from app.policy.windows import TimezoneName

_DAILY_FIELDS = ("max_sends_per_day", "max_new_contacts_per_day", "max_follow_ups_per_day")


class GlobalDailyLimits(CoreModel):
    max_sends_per_day: NonNegativeInt
    max_new_contacts_per_day: NonNegativeInt
    max_follow_ups_per_day: NonNegativeInt


class ScopedDailyLimits(CoreModel):
    max_sends_per_day: NonNegativeInt | None = None
    max_new_contacts_per_day: NonNegativeInt | None = None
    max_follow_ups_per_day: NonNegativeInt | None = None


class MailboxLimits(CoreModel):
    mailbox: EmailAddress
    limits: ScopedDailyLimits


class CampaignLimits(CoreModel):
    campaign_id: EntityId
    limits: ScopedDailyLimits


class LimitPolicy(CoreModel):
    """Sending limits. Daily counts are taken over local calendar days in ``timezone``."""

    policy_version: NonEmptyStr
    timezone: TimezoneName
    global_limits: GlobalDailyLimits
    mailboxes: tuple[MailboxLimits, ...] = ()
    campaigns: tuple[CampaignLimits, ...] = ()
    max_follow_ups_per_contact: NonNegativeInt
    min_interval_between_follow_ups: timedelta

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.min_interval_between_follow_ups <= timedelta(0):
            raise ValueError("min_interval_between_follow_ups must be positive")
        unique_items(tuple(entry.mailbox for entry in self.mailboxes))
        unique_items(tuple(entry.campaign_id for entry in self.campaigns))
        scoped = [(f"mailbox {m.mailbox}", m.limits) for m in self.mailboxes]
        scoped += [(f"campaign {c.campaign_id}", c.limits) for c in self.campaigns]
        for label, limits in scoped:
            for field in _DAILY_FIELDS:
                value = getattr(limits, field)
                cap = getattr(self.global_limits, field)
                if value is not None and value > cap:
                    raise ValueError(f"{label} {field}={value} exceeds the global cap {cap}")
        return self

    def limits_for_mailbox(self, mailbox: str) -> ScopedDailyLimits | None:
        return next((m.limits for m in self.mailboxes if m.mailbox == mailbox), None)

    def limits_for_campaign(self, campaign_id: str) -> ScopedDailyLimits | None:
        return next((c.limits for c in self.campaigns if c.campaign_id == campaign_id), None)


def effective_max_follow_ups(limits: LimitPolicy, campaign: Campaign) -> int:
    """The stricter of the global per-contact cap and the campaign's own sequence length."""
    return min(limits.max_follow_ups_per_contact, campaign.max_follow_ups)


def effective_min_interval(limits: LimitPolicy, campaign: Campaign) -> timedelta:
    """The longer (stricter) of the global minimum and the campaign's interval."""
    return max(limits.min_interval_between_follow_ups, campaign.min_interval_between_follow_ups)
