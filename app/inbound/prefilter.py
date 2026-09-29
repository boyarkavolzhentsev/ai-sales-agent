"""Deterministic prefilter. Runs before any LLM call; its outcomes are never classified.

Order: SELF_LOOP (from one of our own addresses), BOUNCE (mailer-daemon/postmaster or a
delivery-status report), AUTO_SUBMITTED (RFC 3834 Auto-Submitted other than "no",
Precedence bulk/junk/auto_reply, or an X-Autoreply header). DUPLICATE is decided by the
idempotency layer, not here.
"""

from collections.abc import Collection

from app.inbound.models import InboundEnvelope, PrefilterOutcome

BOUNCE_LOCAL_PARTS = frozenset({"mailer-daemon", "postmaster"})
AUTO_PRECEDENCE = frozenset({"bulk", "junk", "auto_reply"})


def prefilter(envelope: InboundEnvelope, own_addresses: Collection[str]) -> PrefilterOutcome:
    sender = envelope.from_address
    if sender == envelope.mailbox or sender in own_addresses:
        return PrefilterOutcome.SELF_LOOP
    content_type = (envelope.content_type or "").casefold().replace(" ", "")
    if sender.split("@", 1)[0] in BOUNCE_LOCAL_PARTS or "report-type=delivery-status" in content_type:
        return PrefilterOutcome.BOUNCE
    if envelope.auto_submitted is not None and envelope.auto_submitted.strip().casefold() != "no":
        return PrefilterOutcome.AUTO_SUBMITTED
    if envelope.precedence is not None and envelope.precedence.strip().casefold() in AUTO_PRECEDENCE:
        return PrefilterOutcome.AUTO_SUBMITTED
    if envelope.x_autoreply is not None:
        return PrefilterOutcome.AUTO_SUBMITTED
    return PrefilterOutcome.NONE
