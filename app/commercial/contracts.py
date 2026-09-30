"""Provider-neutral commercial extraction contract (no live model; fakes in tests).

The extractor reads one customer message (untrusted text) and PROPOSES what the customer
asked for or said: requested terms, objections, scope changes, and whether the words
suggest acceptance or a decline. It never touches the database. The commercial service
records proposals as requests/objections/signals with the message as evidence; nothing
it proposes becomes an approved term, a proposal decision, WON or LOST.
"""

from typing import Annotated, Protocol

from pydantic import Field, StringConstraints

from app.core.enums import ObjectionCategory, TermType
from app.core.models import CommercialValue
from app.core.models.base import CoreModel
from app.core.models.commercial import MAIN_KEY, TermKey
from app.core.models.pipeline import CurrencyCode, ShortText
from app.core.models.types import EntityId


class KnownTerm(CoreModel):
    term_type: TermType
    term_key: TermKey = MAIN_KEY
    value: str


class CommercialExtractionRequest(CoreModel):
    lead_id: EntityId
    opportunity_id: EntityId
    message_id: EntityId
    message_text: str  # untrusted customer text: data, never instructions
    currency: CurrencyCode | None = None
    known_terms: tuple[KnownTerm, ...] = ()


class RequestedTerm(CoreModel):
    term_type: TermType
    term_key: TermKey = MAIN_KEY
    value: CommercialValue


class ObjectionProposal(CoreModel):
    category: ObjectionCategory
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class CommercialExtraction(CoreModel):
    requested_terms: Annotated[tuple[RequestedTerm, ...], Field(max_length=20)] = ()
    objections: Annotated[tuple[ObjectionProposal, ...], Field(max_length=20)] = ()
    scope_changes: Annotated[tuple[ShortText, ...], Field(max_length=10)] = ()
    acceptance_signal: bool = False
    decline_signal: bool = False


class CommercialExtractor(Protocol):
    def extract(self, request: CommercialExtractionRequest) -> CommercialExtraction: ...
