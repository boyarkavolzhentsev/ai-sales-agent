"""Deterministic unsubscribe recognition, independent of the LLM.

Runs on every human-looking message before any early escalation path or LLM call, so an
explicit unsubscribe request is honored even when the message is otherwise escalated
(attachments, ambiguous or unverified threading, classifier failure). Patterns are
conservative explicit requests; negated wording ("please don't unsubscribe me") does not
match. Recording suppression is the safe direction (Stage 0 ratchet).
"""

import re

_NOT = r"(?<!not )(?<!n't )(?<!dont )(?<!never )"
_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        rf"{_NOT}\bunsubscribe me\b",
        rf"{_NOT}\bplease unsubscribe\b",
        r"\bremove me from (?:your|the|this) (?:list|mailing list|emails?|distribution list)\b",
        rf"{_NOT}\bstop (?:emailing|contacting|messaging|writing to) me\b",
        r"\bopt me out\b",
        r"\b(?:do not|don't|dont) (?:contact|email|e-mail) me\b",
    )
)
_SUBJECTS = frozenset({"unsubscribe", "remove me", "stop"})


def is_unsubscribe_request(subject: str, body: str) -> bool:
    def fold(text: str) -> str:
        return " ".join(text.replace("’", "'").casefold().split())

    if fold(subject).rstrip(".!") in _SUBJECTS:
        return True
    text = fold(f"{subject}\n{body}")
    return any(pattern.search(text) for pattern in _PATTERNS)
