"""Deterministic post-draft claim check. Pure: no LLM, no database, no network.

This is mechanical matching, not semantic fact verification. It extracts high-risk,
mechanically checkable tokens from a draft and accepts each only if the same normalized
value occurs in the supplied KnowledgeEvidence (or in app-supplied trusted references,
e.g. the sender's own company name). The model cannot influence the rules or the
evidence the checker sees.

Extraction, in precedence order (an overlapping span goes to the first type):
URL, EMAIL, DATE, MONEY, PERCENTAGE, PHONE, NUMBER; then ORGANIZATION names with a legal
suffix ("Acme Ltd"); separately, forbidden COMMITMENT phrases (always a failure).

Normalization is deliberately narrow (see the module functions): numbers compare by
decimal value ("100" == "100.00"), US-style thousands separators are understood, any other
comma form is compared verbatim; currencies must match explicitly (€ = EUR, £ = GBP,
₴ = UAH, "US$" = USD, while a bare "$" stays its own currency and never equals USD);
"10%" == "10 percent"; dates normalize to ISO, dates without a year only to month-day,
and numeric slash dates are compared verbatim; URLs ignore scheme, host case and a trailing
slash; emails ignore case; phones compare digits (plus a leading "+"). Word numbers
("two weeks") are not detected.
"""

import re
from collections.abc import Callable, Collection, Iterable, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import AfterValidator, NonNegativeInt

from app.core.enums import ClaimCheckStatus
from app.core.models import KnowledgeEvidence
from app.core.models.base import CoreModel
from app.core.models.types import EntityId, NonEmptyStr, Sha256Hex
from app.core.validation import unique_items
from app.llm.models import sha256_hex


class ClaimType(StrEnum):
    URL = "URL"
    EMAIL = "EMAIL"
    DATE = "DATE"
    MONEY = "MONEY"
    PERCENTAGE = "PERCENTAGE"
    PHONE = "PHONE"
    NUMBER = "NUMBER"
    ORGANIZATION = "ORGANIZATION"
    COMMITMENT = "COMMITMENT"


class FindingReason(StrEnum):
    SUPPORTED_BY_EVIDENCE = "SUPPORTED_BY_EVIDENCE"
    SUPPORTED_BY_TRUSTED_REFERENCE = "SUPPORTED_BY_TRUSTED_REFERENCE"
    NOT_IN_EVIDENCE = "NOT_IN_EVIDENCE"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    FORBIDDEN_COMMITMENT = "FORBIDDEN_COMMITMENT"


class ClaimFinding(CoreModel):
    claim_type: ClaimType
    extracted: NonEmptyStr
    normalized: NonEmptyStr
    supported: bool
    evidence_ids: Annotated[tuple[EntityId, ...], AfterValidator(unique_items)] = ()
    reason: FindingReason
    start: NonNegativeInt


class ClaimCheckResult(CoreModel):
    passed: bool
    findings: tuple[ClaimFinding, ...]
    evidence_ids_checked: Annotated[tuple[EntityId, ...], AfterValidator(unique_items)]
    draft_hash: Sha256Hex

    @property
    def status(self) -> ClaimCheckStatus:
        return ClaimCheckStatus.PASS if self.passed else ClaimCheckStatus.FAIL

    @property
    def unsupported(self) -> tuple[ClaimFinding, ...]:
        return tuple(f for f in self.findings if not f.supported)


# ---- Normalization ------------------------------------------------------------------------

_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?"
_US_THOUSANDS = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?")
_CODES = "EUR|USD|GBP|UAH|CHF|PLN|CAD|AUD|JPY|SEK|NOK|DKK|CZK"
_SYMBOLS = {"€": "EUR", "£": "GBP", "₴": "UAH", "$": "$", "US$": "USD"}
_WORDS = {"euro": "EUR", "euros": "EUR", "hryvnia": "UAH", "hryvnias": "UAH"}
_MONTHS = {
    name: index
    for index, names in enumerate(
        [
            ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"),
            ("may",), ("june", "jun"), ("july", "jul"), ("august", "aug"),
            ("september", "sep", "sept"), ("october", "oct"), ("november", "nov"), ("december", "dec"),
        ],
        start=1,
    )
    for name in names
}
_MONTH = "|".join(sorted(_MONTHS, key=len, reverse=True))


def normalize_number(raw: str) -> str:
    """Decimal value as plain text. Only unambiguous forms are converted: "1,200.50" (US
    thousands) and "100.00"; any other comma form ("1,5", "1.200,50") stays verbatim."""
    text = raw.replace(",", "") if _US_THOUSANDS.fullmatch(raw) else raw
    if "," in text:
        return raw
    try:
        value = Decimal(text)
    except InvalidOperation:
        return raw
    normalized = format(value.normalize(), "f")
    return "0" if normalized in ("-0", "") else normalized


def _currency(token: str) -> str:
    token = token.strip()
    return _SYMBOLS.get(token) or _WORDS.get(token.casefold()) or token.upper()


def _url(raw: str) -> str:
    candidate = raw if "://" in raw else f"http://{raw}"
    parts = urlsplit(candidate)
    path = parts.path.rstrip("/")
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.netloc.casefold()}{path}{query}"


def _iso(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


# ---- Extraction -----------------------------------------------------------------------------


class _Claim:
    __slots__ = ("claim_type", "end", "extracted", "normalized", "start")

    def __init__(self, claim_type: ClaimType, start: int, end: int, extracted: str, normalized: str) -> None:
        self.claim_type, self.start, self.end = claim_type, start, end
        self.extracted, self.normalized = extracted, normalized


def _date_claims(text: str) -> Iterable[tuple[re.Match[str], str]]:
    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", text):
        yield m, _iso(int(m[1]), int(m[2]), int(m[3])) or m[0]
    for m in re.finditer(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH})\.?,?\s+(\d{{4}})\b", text, re.I):
        yield m, _iso(int(m[3]), _MONTHS[m[2].casefold()], int(m[1])) or m[0]
    for m in re.finditer(rf"\b({_MONTH})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", text, re.I):
        yield m, _iso(int(m[3]), _MONTHS[m[1].casefold()], int(m[2])) or m[0]
    for m in re.finditer(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH})\b", text, re.I):
        yield m, f"--{_MONTHS[m[2].casefold()]:02d}-{int(m[1]):02d}"
    for m in re.finditer(rf"\b({_MONTH})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", text, re.I):
        yield m, f"--{_MONTHS[m[1].casefold()]:02d}-{int(m[2]):02d}"
    for m in re.finditer(r"\b\d{1,2}[/.]\d{1,2}[/.]\d{2,4}\b", text):
        yield m, m[0]


def _money_claims(text: str) -> Iterable[tuple[re.Match[str], str]]:
    for m in re.finditer(rf"(US\$|[€£₴$])\s?({_NUM})", text):
        yield m, f"{_currency(m[1])} {normalize_number(m[2])}"
    for m in re.finditer(rf"\b({_CODES})\s?({_NUM})", text):
        yield m, f"{_currency(m[1])} {normalize_number(m[2])}"
    for m in re.finditer(rf"({_NUM})\s?({_CODES}|euros?|hryvnias?)\b", text, re.I):
        yield m, f"{_currency(m[2])} {normalize_number(m[1])}"
    for m in re.finditer(rf"({_NUM})\s?([€£₴])", text):
        yield m, f"{_currency(m[2])} {normalize_number(m[1])}"


Extractor = Callable[[str], Iterable[tuple[re.Match[str], str]]]

_EXTRACTORS: tuple[tuple[ClaimType, Extractor], ...] = (
    (ClaimType.URL, lambda t: ((m, _url(m[0])) for m in _trimmed(re.finditer(r"\b(?:https?://|www\.)[^\s<>()\"']+", t)))),
    (ClaimType.EMAIL, lambda t: ((m, m[0].casefold()) for m in re.finditer(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", t))),
    (ClaimType.DATE, _date_claims),
    (ClaimType.MONEY, _money_claims),
    (ClaimType.PERCENTAGE, lambda t: ((m, f"{normalize_number(m[1])}%") for m in re.finditer(rf"({_NUM})\s?(?:%|per\s?cent\b)", t, re.I))),
    (ClaimType.PHONE, lambda t: ((m, ("+" if m[0].startswith("+") else "") + re.sub(r"\D", "", m[0])) for m in re.finditer(r"(?<![\w+])\+?\d[\d\s().-]{5,}\d(?!\w)", t) if len(re.sub(r"\D", "", m[0])) >= 7)),
    (ClaimType.NUMBER, lambda t: ((m, normalize_number(m[0])) for m in re.finditer(r"(?<![\w.,])\d+(?:[.,]\d+)?(?![\w])", t))),
)

# Capitalized words ending in a legal suffix, on one line (words never span a line break).
_ORGANIZATION = re.compile(
    r"\b[A-Z][\w&'-]*(?:[ \t]+[A-Z][\w&'-]*)*[ \t]+"
    r"(?:Inc|Ltd|LLC|GmbH|Corp|Co|AG|SA|BV|PLC|LLP|Limited|Corporation|Company)\b\.?"
)

# Unauthorized commitments: conservative phrase patterns, each with a stable code.
COMMITMENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GUARANTEE", re.compile(r"\bguarantee(?:d|s)?\b", re.I)),
    ("LEGALLY_BINDING", re.compile(r"\blegally\s+binding\b", re.I)),
    ("REFUND_PROMISE", re.compile(r"\b(?:we|i)\s*(?:will|'ll|can)\s+(?:issue\s+(?:you\s+)?a\s+|give\s+you\s+a\s+)?refund\b|\bfull\s+refund\b", re.I)),
    (
        "DISCOUNT_PROMISE",
        re.compile(
            r"\b(?:we|i)\s*(?:will|'ll|can)\s+(?:offer|give|provide|extend|apply)\s+(?:you\s+)?(?:a\s+|an\s+)?"
            r"(?:special\s+|custom\s+|exclusive\s+|additional\s+|extra\s+)?(?:\d+\s?%\s+)?discount\b"
            r"|\b(?:special|custom|exclusive)\s+(?:price|pricing|discount|deal)\s+for\s+you\b",
            re.I,
        ),
    ),
    ("CONTRACT_ACCEPTANCE", re.compile(r"\b(?:we|i)\s+(?:accept|agree\s+to)\s+(?:your|the)\s+(?:terms|contract|proposal|offer)\b", re.I)),
    (
        "MEETING_CONFIRMED",
        re.compile(
            r"\b(?:i|we)\s+(?:have|'ve)\s+(?:booked|scheduled)\b"
            r"|\b(?:your|the|our)\s+(?:meeting|call|demo)\s+(?:is|has\s+been)\s+(?:confirmed|booked|scheduled)\b",
            re.I,
        ),
    ),
)


def _trimmed(matches: Iterable[re.Match[str]]) -> Iterable[re.Match[str]]:
    """URL matches with trailing sentence punctuation removed (via a re-match)."""
    for m in matches:
        end = m.end()
        while end > m.start() and m.string[end - 1] in ".,;:!?)]'\"":
            end -= 1
        trimmed = re.compile(re.escape(m.string[m.start() : end])).match(m.string, m.start())
        if trimmed is not None:
            yield trimmed


def extract_claims(text: str) -> list[_Claim]:
    taken: list[tuple[int, int]] = []
    claims: list[_Claim] = []

    def free(start: int, end: int) -> bool:
        return all(end <= s or start >= e for s, e in taken)

    for claim_type, extractor in _EXTRACTORS:
        for match, normalized in extractor(text):
            if normalized and free(match.start(), match.end()):
                taken.append((match.start(), match.end()))
                claims.append(_Claim(claim_type, match.start(), match.end(), match[0], normalized))
    for match in _ORGANIZATION.finditer(text):
        claims.append(_Claim(ClaimType.ORGANIZATION, match.start(), match.end(), match[0], _org(match[0])))
    return claims


def _org(raw: str) -> str:
    return " ".join(raw.rstrip(".").split()).casefold()


def _numbers_of(claim: _Claim) -> str | None:
    if claim.claim_type is ClaimType.NUMBER:
        return claim.normalized
    if claim.claim_type is ClaimType.MONEY:
        return claim.normalized.split(" ", 1)[1]
    if claim.claim_type is ClaimType.PERCENTAGE:
        return claim.normalized.rstrip("%")
    return None


# ---- Checking -------------------------------------------------------------------------------


def draft_hash(subject: str, body: str) -> str:
    return sha256_hex(f"{subject}\n\n{body}")


def check_draft_claims(
    subject: str,
    body: str,
    evidence: Sequence[KnowledgeEvidence],
    *,
    trusted_references: Collection[str] = (),
) -> ClaimCheckResult:
    """Check every mechanically checkable claim in a draft against the given evidence.

    ``trusted_references`` are deterministic values the application vouches for (never
    model output), e.g. the sender's company name. Fails when any claim is unsupported
    or any forbidden commitment appears. Findings are ordered by position, then type.
    """
    supported: dict[tuple[ClaimType, str], set[str]] = {}
    evidence_numbers: dict[str, set[str]] = {}
    evidence_text: list[tuple[str, str]] = []
    for item in evidence:
        evidence_text.append((item.evidence_id, " ".join(item.excerpt.split()).casefold()))
        for claim in extract_claims(item.excerpt):
            supported.setdefault((claim.claim_type, claim.normalized), set()).add(item.evidence_id)
            number = _numbers_of(claim)
            if number is not None:
                evidence_numbers.setdefault(number, set()).add(item.evidence_id)
    trusted: set[tuple[ClaimType, str]] = set()
    trusted_text: list[str] = []
    for reference in trusted_references:
        trusted_text.append(" ".join(reference.split()).casefold())
        trusted.update((c.claim_type, c.normalized) for c in extract_claims(reference))

    text = f"{subject}\n\n{body}"
    findings: list[ClaimFinding] = []
    for claim in extract_claims(text):
        key = (claim.claim_type, claim.normalized)
        if claim.claim_type is ClaimType.ORGANIZATION:
            ids = {eid for eid, content in evidence_text if claim.normalized in content}
            by_trust = any(claim.normalized in reference for reference in trusted_text)
        elif claim.claim_type is ClaimType.NUMBER:
            ids = evidence_numbers.get(claim.normalized, set())
            by_trust = key in trusted
        else:
            ids = supported.get(key, set())
            by_trust = key in trusted
        if ids:
            reason = FindingReason.SUPPORTED_BY_EVIDENCE
        elif by_trust:
            reason = FindingReason.SUPPORTED_BY_TRUSTED_REFERENCE
        elif claim.claim_type is ClaimType.MONEY and claim.normalized.split(" ", 1)[1] in evidence_numbers:
            reason = FindingReason.CURRENCY_MISMATCH
        else:
            reason = FindingReason.NOT_IN_EVIDENCE
        findings.append(
            ClaimFinding(
                claim_type=claim.claim_type,
                extracted=claim.extracted,
                normalized=claim.normalized,
                supported=bool(ids) or by_trust,
                evidence_ids=tuple(sorted(ids)),
                reason=reason,
                start=claim.start,
            )
        )
    for code, pattern in COMMITMENT_PATTERNS:
        for match in pattern.finditer(text):
            findings.append(
                ClaimFinding(
                    claim_type=ClaimType.COMMITMENT,
                    extracted=match[0],
                    normalized=code,
                    supported=False,
                    reason=FindingReason.FORBIDDEN_COMMITMENT,
                    start=match.start(),
                )
            )
    findings.sort(key=lambda f: (f.start, f.claim_type.value, f.extracted))
    return ClaimCheckResult(
        passed=all(f.supported for f in findings),
        findings=tuple(findings),
        evidence_ids_checked=tuple(sorted({e.evidence_id for e in evidence})),
        draft_hash=draft_hash(subject, body),
    )
