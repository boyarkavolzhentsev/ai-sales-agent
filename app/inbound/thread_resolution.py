"""Deterministic thread resolution by message identity plus participant ownership.

In-Reply-To and References are looked up as known Internet Message-IDs in the same
mailbox. Several different threads: ambiguous (new thread, escalate). Exactly one thread:
joined only if the normalized sender is already one of that thread's participants; a
matching Message-ID alone never grants access to another contact's history or lead, so a
non-participant is unverified (new thread, own lead, escalate, no LLM call). No match: a
new thread. Subject, sender and body similarity are never used to merge threads.
Provider thread references are recorded on the envelope but not used yet.
"""

from dataclasses import dataclass

from app.core.models import EmailThread
from app.inbound.models import InboundEnvelope
from app.persistence import UnitOfWork


@dataclass(frozen=True)
class ThreadMatch:
    thread: EmailThread | None
    ambiguous: bool
    unverified: bool = False


def find_thread(uow: UnitOfWork, envelope: InboundEnvelope) -> ThreadMatch:
    candidates = [envelope.in_reply_to, *envelope.references]
    found: dict[str, EmailThread] = {}
    for rfc_message_id in dict.fromkeys(c for c in candidates if c is not None):
        known = uow.messages.get_by_rfc_message_id(rfc_message_id)
        if known is None:
            continue
        thread = uow.threads.get(known.thread_id)
        if thread is not None and thread.mailbox == envelope.mailbox:
            found[thread.thread_id] = thread
    if len(found) > 1:
        return ThreadMatch(thread=None, ambiguous=True)
    thread = next(iter(found.values()), None)
    if thread is not None and envelope.from_address not in thread.participant_addresses:
        return ThreadMatch(thread=None, ambiguous=False, unverified=True)
    return ThreadMatch(thread=thread, ambiguous=False)
