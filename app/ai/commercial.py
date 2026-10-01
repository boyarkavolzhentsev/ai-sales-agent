"""LLM-backed Stage 13 ``CommercialExtractor``.

Returns exactly the existing ``CommercialExtraction``. Every requested term, objection,
scope change and acceptance/decline signal must be grounded in a verbatim quote of the
customer's message; money and percentages must be the numbers the customer wrote, in a
currency they named (or the proposal's own). The commercial service then records them as
customer REQUESTS and SIGNALS only: nothing becomes an approved term, a price, WON or LOST.
"""

from typing import Annotated

from pydantic import Field, StringConstraints

from app.ai.grounding import (
    bounded,
    require_amount_in_quote,
    require_currency,
    require_numbers_in_quote,
    require_quote,
)
from app.ai.prompts import COMMERCIAL_EXTRACTOR_PROMPT_V1
from app.commercial.contracts import (
    CommercialExtraction,
    CommercialExtractionRequest,
    ObjectionProposal,
    RequestedTerm,
)
from app.core.enums import ObjectionCategory, TermType
from app.core.models import CommercialValue
from app.core.models.base import CoreModel
from app.core.models.commercial import MAIN_KEY, TermKey
from app.core.models.pipeline import ShortText
from app.llm import SectionKind, StructuredLLM
from app.llm.prompts import build_request, section

Quote = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class GroundedTerm(CoreModel):
    term_type: TermType
    term_key: TermKey = MAIN_KEY
    value: CommercialValue
    quote: Quote


class GroundedObjection(CoreModel):
    category: ObjectionCategory
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    quote: Quote


class GroundedScopeChange(CoreModel):
    change: ShortText
    quote: Quote


class CommercialCandidates(CoreModel):
    """What the model returns (validated strictly): the contract plus a quote per item."""

    requested_terms: Annotated[tuple[GroundedTerm, ...], Field(max_length=20)] = ()
    objections: Annotated[tuple[GroundedObjection, ...], Field(max_length=20)] = ()
    scope_changes: Annotated[tuple[GroundedScopeChange, ...], Field(max_length=10)] = ()
    acceptance_quote: Quote | None = None
    decline_quote: Quote | None = None


class LLMCommercialExtractor:
    def __init__(self, llm: StructuredLLM, *, locale: str = "en") -> None:
        self._llm = llm
        self._locale = locale

    def extract(self, request: CommercialExtractionRequest) -> CommercialExtraction:
        message = bounded(request.message_text)
        call = build_request(
            COMMERCIAL_EXTRACTOR_PROMPT_V1, CommercialCandidates, correlation_id=request.message_id, locale=self._locale,
            sections=[
                section(SectionKind.TRUSTED_METADATA, "proposal", {
                    "currency": request.currency,
                    "current_terms": [t.model_dump(mode="json") for t in request.known_terms],
                }),
                section(SectionKind.UNTRUSTED_DATA, "customer_message", message),
            ],
        )
        found = self._llm.complete_structured(call).output
        for term in found.requested_terms:
            what = f"requested {term.term_type.value}"
            require_quote(term.quote, message, what)
            if term.value.money is not None:
                require_amount_in_quote(term.value.money.amount, term.quote, what)
                require_currency(term.value.money.currency, term.quote, request.currency, what)
            if term.value.percent is not None:
                require_amount_in_quote(term.value.percent, term.quote, what)
            if term.value.text is not None:
                require_numbers_in_quote(term.value.text, term.quote, what)
        for objection in found.objections:
            require_quote(objection.quote, message, f"{objection.category.value} objection")
        for change in found.scope_changes:
            require_quote(change.quote, message, "scope change")
            require_numbers_in_quote(change.change, change.quote, "scope change")
        for quote, what in ((found.acceptance_quote, "acceptance"), (found.decline_quote, "decline")):
            if quote is not None:
                require_quote(quote, message, what)
        return CommercialExtraction(
            requested_terms=tuple(RequestedTerm(term_type=t.term_type, term_key=t.term_key, value=t.value)
                                  for t in found.requested_terms),
            objections=tuple(ObjectionProposal(category=o.category, summary=o.summary) for o in found.objections),
            scope_changes=tuple(c.change for c in found.scope_changes),
            acceptance_signal=found.acceptance_quote is not None,
            decline_signal=found.decline_quote is not None,
        )
