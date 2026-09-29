"""Deterministic follow-up draft text. No LLM, no knowledge claims: a follow-up only
restates availability, so it cannot introduce unsupported facts. It still passes the
Stage 5 deterministic claim check, and the operator reviews it like any draft (V1)."""

from app.core.models import EmailThread
from app.llm import SenderIdentity
from app.llm.claim_check import ClaimCheckResult, check_draft_claims


def compose_follow_up(thread: EmailThread, sender: SenderIdentity) -> tuple[str, str]:
    subject = f"Re: {thread.subject_normalized}" if thread.subject_normalized.strip() else "Following up"
    body = (
        "Hello,\n\n"
        "I wanted to follow up on my previous message. If you have any questions, "
        "I would be glad to help.\n\n"
        f"Best regards,\n{sender.sender_name}\n{sender.company_name}"
    )
    return subject, body


def check_follow_up(subject: str, body: str, sender: SenderIdentity) -> ClaimCheckResult:
    return check_draft_claims(subject, body, (), trusted_references=(sender.company_name, sender.sender_name))
