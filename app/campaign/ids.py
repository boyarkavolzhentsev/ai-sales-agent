"""Deterministic identities for campaign execution."""

from app.conversation.ids import stable_id

# Outbound messages produced by a campaign job carry this idempotency-key prefix.
CAMPAIGN_KEY_PREFIX = "campaign:"


def member_id_for(campaign_id: str, contact_id: str) -> str:
    """One logical membership per (campaign, contact)."""
    return stable_id("cm", campaign_id, contact_id)


def job_id_for(member_id: str, touch_no: int) -> str:
    """One logical touch: number ``touch_no`` of membership ``member_id``."""
    return stable_id("cj", member_id, str(touch_no))


def lead_id_for(member_id: str) -> str:
    return stable_id("ld", "campaign", member_id)


def thread_id_for(member_id: str) -> str:
    return stable_id("th", "campaign", member_id)


def plan_id_for(member_id: str) -> str:
    return stable_id("fp", "campaign", member_id)


def campaign_key(job_id: str) -> str:
    return f"{CAMPAIGN_KEY_PREFIX}{job_id}"


__all__ = ["CAMPAIGN_KEY_PREFIX", "campaign_key", "job_id_for", "lead_id_for", "member_id_for", "plan_id_for", "stable_id", "thread_id_for"]
