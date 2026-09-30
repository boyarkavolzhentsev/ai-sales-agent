"""The configurable commercial profile. Business policy is data, never invented here.

There is no default price, discount or payment term. A term default exists only when the
profile explicitly configures one. Without a discount policy every discount is still an
explicit operator decision (V1 never approves a discount automatically); a configured
policy only adds hard limits an operator cannot exceed.
"""

from decimal import Decimal
from typing import Annotated, Self

from pydantic import Field, model_validator

from app.core.enums import TermType, ValueKind
from app.core.models import CommercialValue
from app.core.models.base import CoreModel
from app.core.models.commercial import ItemRef, Percent
from app.core.models.pipeline import CurrencyCode
from app.core.models.types import NonEmptyStr

# The value kind every term type takes (V1: discounts are percentages).
TERM_KINDS: dict[TermType, ValueKind] = {t: ValueKind.TEXT for t in TermType} | {
    TermType.PRICE: ValueKind.MONEY, TermType.DISCOUNT: ValueKind.PERCENT,
}


class DiscountPolicy(CoreModel):
    max_percent: Percent
    forbidden_item_refs: tuple[ItemRef, ...] = ()


class CommercialProfile(CoreModel):
    profile_id: NonEmptyStr
    # Currency -> decimal places used for rounding. Only these currencies are proposable.
    currencies: Annotated[dict[CurrencyCode, Annotated[int, Field(ge=0, le=4)]], Field(min_length=1)]
    required_terms: tuple[TermType, ...] = ()
    optional_terms: tuple[TermType, ...] = ()
    # Terms only an operator may set (all Stage 13 approvals are operator actions anyway;
    # listing them makes the intent explicit and blocks any other source).
    operator_only_terms: tuple[TermType, ...] = (TermType.PRICE, TermType.DISCOUNT, TermType.SLA, TermType.WARRANTY,
                                                 TermType.LEGAL_TERM, TermType.DELIVERY_WINDOW, TermType.PAYMENT_TERM)
    term_defaults: dict[TermType, CommercialValue] = {}
    discount_policy: DiscountPolicy | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        for term, value in self.term_defaults.items():
            if value.kind is not TERM_KINDS[term]:
                raise ValueError(f"the default for {term} must be {TERM_KINDS[term]}")
            if term in (TermType.PRICE, TermType.DISCOUNT):
                raise ValueError("prices and discounts are never defaults")
        if TermType.PRICE in self.required_terms or TermType.CURRENCY in self.required_terms:
            raise ValueError("prices and the currency are proposal inputs, not terms to require")
        return self

    def decimals(self, currency: str) -> int:
        return self.currencies[currency]


# V1 generic profile: EUR proposals, an agreed payment term required, nothing defaulted.
GENERIC_COMMERCIAL_PROFILE = CommercialProfile(
    profile_id="generic-commercial-v1",
    currencies={"EUR": 2},
    required_terms=(TermType.PAYMENT_TERM,),
    optional_terms=(TermType.BILLING_CADENCE, TermType.CONTRACT_LENGTH, TermType.DELIVERY_WINDOW,
                    TermType.VALIDITY_PERIOD, TermType.IMPLEMENTATION_SCOPE, TermType.SLA, TermType.WARRANTY,
                    TermType.LEGAL_TERM, TermType.CUSTOM_TERM, TermType.DISCOUNT, TermType.TAX),
)


class CommercialConfig(CoreModel):
    profile: CommercialProfile = GENERIC_COMMERCIAL_PROFILE
    knowledge_locale: NonEmptyStr = "en"
    queue_limit: Annotated[int, Field(ge=1, le=5000)] = 500


def percent_ok(value: Decimal) -> bool:
    return Decimal(0) < value < Decimal(100)
