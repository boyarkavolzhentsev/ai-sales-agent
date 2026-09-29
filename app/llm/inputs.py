"""Shared input shapes for LLM tasks."""

from typing import Annotated

from pydantic import AwareDatetime, StringConstraints

from app.core.enums import EmailDirection
from app.core.models.base import CoreModel

MAX_EMAIL_BODY_CHARS = 20_000

UntrustedText = Annotated[str, StringConstraints(max_length=MAX_EMAIL_BODY_CHARS)]


class UntrustedEmail(CoreModel):
    """Email text as the model sees it. Always placed in an UNTRUSTED_DATA section.

    Fields are raw strings on purpose (not validated addresses): they are data to read,
    never identities to act on. Over-long bodies are rejected, not truncated; trimming is
    the caller's explicit decision.
    """

    direction: EmailDirection
    sender: Annotated[str, StringConstraints(max_length=320)] = ""
    subject: Annotated[str, StringConstraints(max_length=1000)] = ""
    body: UntrustedText
    sent_at: AwareDatetime | None = None
