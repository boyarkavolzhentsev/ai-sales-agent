"""Deterministic identities for conversation state (same scheme as app.inbound.stable_id,
defined here because app.inbound depends on this package, not the other way round)."""

import hashlib

# Outbound messages produced by a follow-up job carry this idempotency-key prefix.
FOLLOW_UP_KEY_PREFIX = "follow-up:"


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:40]}"


def conversation_id_for(thread_id: str) -> str:
    return stable_id("cv", thread_id)


def follow_up_id_for(conversation_id: str, anchor_outbound_id: str, sequence_no: int) -> str:
    """One logical follow-up: number ``sequence_no`` after outbound ``anchor_outbound_id``."""
    return stable_id("fu", conversation_id, anchor_outbound_id, str(sequence_no))


def follow_up_key(follow_up_id: str) -> str:
    return f"{FOLLOW_UP_KEY_PREFIX}{follow_up_id}"
