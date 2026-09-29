from typing import Self

from pydantic import AwareDatetime, NonNegativeInt, model_validator

from app.core.enums import FollowUpCancelReason, FollowUpStatus
from app.core.models.base import CoreModel
from app.core.models.types import EntityId, Version
from app.core.validation import ensure_not_before

_TERMINAL = frozenset({FollowUpStatus.EXHAUSTED, FollowUpStatus.CANCELLED})


class FollowUpPlan(CoreModel):
    """No-response follow-up sequence for one lead in one campaign.

    ``status`` is authoritative. ``steps_sent`` is a cache of the outbound ledger.
    While ACTIVE with ``steps_sent == max_steps``, ``next_due_at`` is the end of the
    final wait before the plan becomes EXHAUSTED.
    """

    plan_id: EntityId
    lead_id: EntityId
    campaign_id: EntityId
    anchor_outbound_id: EntityId
    max_steps: NonNegativeInt
    steps_sent: NonNegativeInt = 0
    next_due_at: AwareDatetime | None = None
    status: FollowUpStatus = FollowUpStatus.ACTIVE
    cancel_reason: FollowUpCancelReason | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    version: Version = 1

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.steps_sent > self.max_steps:
            raise ValueError("steps_sent must not exceed max_steps")
        if (self.cancel_reason is not None) != (self.status is FollowUpStatus.CANCELLED):
            raise ValueError("cancel_reason is required for, and only allowed on, CANCELLED")
        if self.status is FollowUpStatus.EXHAUSTED and self.steps_sent != self.max_steps:
            raise ValueError("an EXHAUSTED plan must have sent all steps")
        if self.status is FollowUpStatus.ACTIVE and self.next_due_at is None:
            raise ValueError("an ACTIVE plan requires next_due_at")
        if self.status in _TERMINAL and self.next_due_at is not None:
            raise ValueError("a terminal plan must not have next_due_at")
        ensure_not_before(self.updated_at, self.created_at, "updated_at", "created_at")
        return self
