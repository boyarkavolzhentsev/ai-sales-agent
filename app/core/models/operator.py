from typing import Annotated, Self

from pydantic import AwareDatetime, NonNegativeInt, StringConstraints, model_validator

from app.core.enums import OperatorCommandKind, OperatorResponseStatus
from app.core.models.base import CoreModel
from app.core.models.types import EntityId, JsonObject, NonEmptyStr
from app.core.validation import ensure_after, ensure_not_before

# Command name without the leading slash, e.g. "stats", "pause".
CommandName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]


class OperatorCommand(CoreModel):
    """One authenticated operator command received via Telegram. Immutable.

    Only MUTATE commands may carry a confirmation. The nonce and its expiry are set
    together; ``confirmed_at`` requires a nonce and must fall before expiry.
    """

    command_id: EntityId
    telegram_update_id: NonNegativeInt
    operator_user_id: int
    chat_id: int
    name: CommandName
    args: tuple[str, ...] = ()
    kind: OperatorCommandKind
    confirmation_nonce: NonEmptyStr | None = None
    confirmation_expires_at: AwareDatetime | None = None
    confirmed_at: AwareDatetime | None = None
    received_at: AwareDatetime

    @model_validator(mode="after")
    def _check_confirmation(self) -> Self:
        has_nonce = self.confirmation_nonce is not None
        if has_nonce != (self.confirmation_expires_at is not None):
            raise ValueError("confirmation_nonce and confirmation_expires_at must be set together")
        if self.kind is OperatorCommandKind.READ and has_nonce:
            raise ValueError("READ commands must not require confirmation")
        if self.confirmed_at is not None and not has_nonce:
            raise ValueError("confirmed_at requires a confirmation_nonce")
        ensure_after(
            self.confirmation_expires_at, self.received_at, "confirmation_expires_at", "received_at"
        )
        ensure_not_before(self.confirmed_at, self.received_at, "confirmed_at", "received_at")
        ensure_not_before(
            self.confirmation_expires_at, self.confirmed_at, "confirmation_expires_at", "confirmed_at"
        )
        return self


class OperatorResponse(CoreModel):
    """Reply to an operator command. ``rendered_text`` is template-rendered, never LLM-written."""

    command_id: EntityId
    status: OperatorResponseStatus
    payload: JsonObject = {}
    rendered_text: NonEmptyStr
    created_at: AwareDatetime
