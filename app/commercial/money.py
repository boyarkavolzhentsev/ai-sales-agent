"""Deterministic proposal arithmetic. Decimal only; rounding ROUND_HALF_UP to the currency's
configured decimal places at every monetary step (line subtotal, line discount, line
total, proposal discount, proposal total). One currency per proposal. Tax is never
computed: totals are net of tax (``tax_included`` is always False).

A total exists only when every line has an approved price in the proposal currency;
otherwise there is no total at all (never a partial one).
"""

from decimal import ROUND_HALF_UP, Decimal

from app.core.models import LineTotal, Money, ProposalLine, ProposalTotals

HUNDRED = Decimal(100)


def quantize(amount: Decimal, decimals: int) -> Decimal:
    return amount.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)


def line_total(line: ProposalLine, currency: str, decimals: int) -> LineTotal | None:
    if line.unit_price is None or line.unit_price.currency != currency:
        return None
    subtotal = quantize(line.quantity * line.unit_price.amount, decimals)
    discount = quantize(subtotal * line.discount_percent / HUNDRED, decimals) if line.discount_percent else None
    total = subtotal - (discount or Decimal(0))
    return LineTotal(line_id=line.line_id, subtotal=Money(amount=subtotal, currency=currency),
                     discount=Money(amount=discount, currency=currency) if discount is not None else None,
                     total=Money(amount=total, currency=currency))


def proposal_totals(lines: tuple[ProposalLine, ...], currency: str, decimals: int,
                    discount_percent: Decimal | None) -> ProposalTotals | None:
    if not lines:
        return None
    computed = [line_total(line, currency, decimals) for line in lines]
    if any(total is None for total in computed):
        return None
    totals = [t for t in computed if t is not None]
    subtotal = sum((t.total.amount for t in totals), Decimal(0))
    discount = quantize(subtotal * discount_percent / HUNDRED, decimals) if discount_percent else None
    total = subtotal - (discount or Decimal(0))
    return ProposalTotals(
        currency=currency, lines=tuple(totals), subtotal=Money(amount=subtotal, currency=currency),
        discount_percent=discount_percent, discount=Money(amount=discount, currency=currency) if discount is not None else None,
        total=Money(amount=total, currency=currency), tax_included=False,
    )
