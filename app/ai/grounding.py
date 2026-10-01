"""Deterministic grounding checks for model proposals about one customer message.

A model may only report what the customer actually wrote: every proposal carries a short
verbatim quote, which must occur in the message (case and whitespace are normalized,
nothing else), and every number it states must occur in that quote ("twenty percent" is
never 20). A proposal that fails is a contract violation: the whole output is refused,
never repaired or partially applied.
"""

import re
from decimal import Decimal

from app.llm import LLMContractViolationError
from app.llm.claim_check import normalize_number

MAX_MESSAGE_CHARS = 8000  # customer text sent to the model; the quote check uses the same text
_NUMBER = re.compile(r"(?<![\w.,])\d+(?:[.,]\d+)*(?![\w])")
_CURRENCY_WORDS = {
    "EUR": ("€", "eur", "euro"), "USD": ("us$", "usd", "dollar"), "GBP": ("£", "gbp", "pound"),
    "UAH": ("₴", "uah", "грн", "hryvn"), "CHF": ("chf", "franc"), "PLN": ("pln", "zł", "zloty"),
}


def bounded(text: str) -> str:
    return text[:MAX_MESSAGE_CHARS]


def _norm(text: str) -> str:
    return " ".join(text.casefold().split())


def numbers_in(text: str) -> set[str]:
    return {normalize_number(m.group(0)) for m in _NUMBER.finditer(text)}


def require_quote(quote: str, message: str, what: str) -> None:
    if not _norm(quote) or _norm(quote) not in _norm(message):
        raise LLMContractViolationError(f"{what}: the quote does not occur in the customer's message")


def require_numbers_in_quote(value: str, quote: str, what: str) -> None:
    missing = numbers_in(value) - numbers_in(quote)
    if missing:
        raise LLMContractViolationError(f"{what}: number(s) not stated by the customer")


def require_amount_in_quote(amount: Decimal, quote: str, what: str) -> None:
    if normalize_number(format(amount, "f")) not in numbers_in(quote):
        raise LLMContractViolationError(f"{what}: amount not stated by the customer")


def require_currency(currency: str, quote: str, expected: str | None, what: str) -> None:
    """A money value's currency is the proposal's currency, or one the customer named."""
    if currency == expected:
        return
    words = (currency.casefold(), *_CURRENCY_WORDS.get(currency, ()))
    if not any(word in quote.casefold() for word in words):
        raise LLMContractViolationError(f"{what}: currency not stated by the customer")
