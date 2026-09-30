"""Builders for Stage 13 commercial tests. Everything goes through the real Stage 6
inbound flow, the Stage 12 operator pipeline and the Stage 13 operator commands; prices
come from the seeded internal knowledge fixture (plan.basic.monthly_price = 100 EUR)."""

from datetime import datetime, timedelta
from decimal import Decimal

from app.commercial import (
    CommercialConfig,
    CommercialExtraction,
    CommercialProfile,
    CommercialService,
    ObjectionProposal,
    RequestedTerm,
)
from app.commercial.fake import FakeCommercialExtractor
from app.commercial.terms import term_row_id
from app.core.enums import LeadIntent, ObjectionCategory, ObjectionStatus, TermType, ValueKind
from app.core.enums import DNCReason, DNCScope, RefKind
from app.core.models import CommercialValue, DoNotContactEntry, EntityRef, Money, Opportunity, ProposalRevision
from app.inbound import InboundResult
from app.llm import LLMTask
from app.operator import (
    ApproveProposal,
    ApproveTermRequest,
    CommandResult,
    CreateProposal,
    MarkProposalAccepted,
    MarkProposalDeclined,
    MarkProposalPresented,
    OperatorService,
    ProposalLineInput,
    RejectTermRequest,
    ReviseProposal,
    SetCommercialTerm,
    TermOverrideInput,
    UpdateObjection,
    UpdateProposal,
    WithdrawProposal,
)
from app.persistence import Database, FrozenClock
from tests.inbound.builders import NOW, ScriptedTransport, classification, envelope, process
from tests.operator.builders import AS_ALICE, FakeAuthenticator
from tests.operator.builders import config as operator_config
from tests.pipeline.builders import active_opportunity, opportunity_lead

BASIC = "plan.basic.monthly_price"  # 100 EUR in the approved knowledge fixture
TEAM = "plan.team.monthly_price"  # 250 EUR
LATER = NOW + timedelta(minutes=10)


def text(value: str) -> CommercialValue:
    return CommercialValue(kind=ValueKind.TEXT, text=value)


def money(amount: str, currency: str = "EUR") -> CommercialValue:
    return CommercialValue(kind=ValueKind.MONEY, money=Money(amount=Decimal(amount), currency=currency))


def pct(value: str) -> CommercialValue:
    return CommercialValue(kind=ValueKind.PERCENT, percent=Decimal(value))


def ops(db: Database, *, profile: CommercialProfile | None = None, at: datetime = NOW) -> OperatorService:
    return OperatorService(db, FrozenClock(at), operator_config(), FakeAuthenticator(),
                           commercial=CommercialConfig(profile=profile) if profile else None)


def commercial(db: Database, extractor: FakeCommercialExtractor | None = None, *, profile: CommercialProfile | None = None,
               at: datetime = NOW) -> CommercialService:
    return CommercialService(db, FrozenClock(at), CommercialConfig(profile=profile) if profile else CommercialConfig(),
                             extractor=extractor)


def opportunity(db: Database, opportunity_id: str) -> Opportunity:
    with db.transaction() as uow:
        found = uow.opportunities.get(opportunity_id)
    assert found is not None
    return found


def opportunity_for(db: Database) -> tuple[str, str]:
    """A qualified lead with an open opportunity: (lead_id, opportunity_id)."""
    lead_id = opportunity_lead(db)
    return lead_id, active_opportunity(db, lead_id).opportunity_id


def revisions(db: Database, opportunity_id: str) -> list[ProposalRevision]:
    with db.transaction() as uow:
        return uow.proposal_revisions.list_for_opportunity(opportunity_id)


def current(db: Database, opportunity_id: str) -> ProposalRevision:
    return revisions(db, opportunity_id)[-1]


# ---- Operator commands -----------------------------------------------------------------------


def create_proposal(db: Database, opportunity_id: str, currency: str = "EUR", command_id: str = "cmd-proposal",
                    profile: CommercialProfile | None = None) -> CommandResult:
    return ops(db, profile=profile).create_proposal(AS_ALICE, CreateProposal(
        command_id=command_id, correlation_id="c", opportunity_id=opportunity_id,
        expected_opportunity_version=opportunity(db, opportunity_id).version, currency=currency))


def line(line_id: str = "l1", item_ref: str = BASIC, quantity: str = "12", *, unit_price: str | None = None,
         currency: str = "EUR", discount: str | None = None) -> ProposalLineInput:
    return ProposalLineInput(line_id=line_id, item_ref=item_ref, quantity=Decimal(quantity), unit="month",
                             unit_price=Money(amount=Decimal(unit_price), currency=currency) if unit_price else None,
                             discount_percent=Decimal(discount) if discount else None)


def update(db: Database, opportunity_id: str, *lines: ProposalLineInput, overrides: tuple[TermOverrideInput, ...] = (),
           command_id: str = "cmd-update", profile: CommercialProfile | None = None) -> CommandResult:
    revision = current(db, opportunity_id)
    return ops(db, profile=profile).update_proposal(AS_ALICE, UpdateProposal(
        command_id=command_id, correlation_id="c", revision_id=revision.revision_id,
        expected_revision_version=revision.version, lines=lines or (line(),), term_overrides=overrides,
        assumptions=("Pricing per seat per month",), exclusions=("On-site training",)))


def set_term(db: Database, opportunity_id: str, term_type: TermType, value: CommercialValue, command_id: str = "cmd-term",
             expected: int | None = None, profile: CommercialProfile | None = None) -> CommandResult:
    return ops(db, profile=profile).set_commercial_term(AS_ALICE, SetCommercialTerm(
        command_id=command_id, correlation_id="c", opportunity_id=opportunity_id, term_type=term_type, value=value,
        expected_term_version=expected))


def revision_command(db: Database, opportunity_id: str, cls: type, command_id: str, **fields: object) -> CommandResult:
    revision = current(db, opportunity_id)
    command = cls(command_id=command_id, correlation_id="c", revision_id=revision.revision_id,
                  expected_revision_version=revision.version, **fields)
    method = {ApproveProposal: "approve_proposal", ReviseProposal: "revise_proposal", WithdrawProposal: "withdraw_proposal",
              MarkProposalPresented: "mark_proposal_presented", MarkProposalAccepted: "mark_proposal_accepted",
              MarkProposalDeclined: "mark_proposal_declined"}[cls]
    return getattr(ops(db), method)(AS_ALICE, command)


def approve(db: Database, opportunity_id: str, command_id: str = "cmd-approve-proposal") -> CommandResult:
    return revision_command(db, opportunity_id, ApproveProposal, command_id)


def present(db: Database, opportunity_id: str, command_id: str = "cmd-present") -> CommandResult:
    return revision_command(db, opportunity_id, MarkProposalPresented, command_id)


def revise(db: Database, opportunity_id: str, command_id: str = "cmd-revise") -> CommandResult:
    return revision_command(db, opportunity_id, ReviseProposal, command_id)


def approve_request(db: Database, request_id: str, command_id: str = "cmd-approve-request") -> CommandResult:
    with db.transaction() as uow:
        request = uow.term_requests.get(request_id)
        assert request is not None
        term = uow.commercial_terms.get(term_row_id(request.opportunity_id, request.term_type, request.term_key))
    return ops(db).approve_term_request(AS_ALICE, ApproveTermRequest(
        command_id=command_id, correlation_id="c", request_id=request_id, expected_request_version=request.version,
        expected_term_version=term.version if term else None))


def reject_request(db: Database, request_id: str, command_id: str = "cmd-reject-request") -> CommandResult:
    with db.transaction() as uow:
        request = uow.term_requests.get(request_id)
    assert request is not None
    return ops(db).reject_term_request(AS_ALICE, RejectTermRequest(
        command_id=command_id, correlation_id="c", request_id=request_id, expected_request_version=request.version,
        reason="Outside our payment policy."))


def update_objection(db: Database, objection_id: str, status: ObjectionStatus, command_id: str = "cmd-objection") -> CommandResult:
    with db.transaction() as uow:
        objection = uow.objections.get(objection_id)
    assert objection is not None
    return ops(db).update_objection(AS_ALICE, UpdateObjection(
        command_id=command_id, correlation_id="c", objection_id=objection_id, expected_objection_version=objection.version,
        status=status, resolution="Explained the annual plan." if status is ObjectionStatus.RESOLVED else None))


# ---- Positions ------------------------------------------------------------------------------------


def ready_draft(db: Database) -> tuple[str, str]:
    """An opportunity with a priced draft and every required term: READY_FOR_REVIEW."""
    lead_id, opportunity_id = opportunity_for(db)
    create_proposal(db, opportunity_id)
    update(db, opportunity_id)
    set_term(db, opportunity_id, TermType.PAYMENT_TERM, text("NET_30"))
    return lead_id, opportunity_id


def presented(db: Database) -> tuple[str, str]:
    lead_id, opportunity_id = ready_draft(db)
    approve(db, opportunity_id)
    present(db, opportunity_id)
    return lead_id, opportunity_id


# ---- Customer messages ------------------------------------------------------------------------------


def customer_message(db: Database, provider_message_id: str, extraction: CommercialExtraction, *,
                     received_at: datetime = LATER, intent: LeadIntent = LeadIntent.NEGOTIATION) -> tuple[InboundResult, object]:
    """A customer message through Stage 6, then the commercial hook with a scripted extraction."""
    result = process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(intent)),
                     envelope(provider_message_id, body="About your proposal.", in_reply_to="<p-1@prospect.example>",
                              received_at=received_at))
    outcome = commercial(db, FakeCommercialExtractor(default=extraction)).record_inbound(result, correlation_id=f"c-{provider_message_id}")
    return result, outcome


def asks(*terms: tuple[TermType, CommercialValue], objections: tuple[tuple[ObjectionCategory, str], ...] = (),
         accept: bool = False, decline: bool = False) -> CommercialExtraction:
    return CommercialExtraction(
        requested_terms=tuple(RequestedTerm(term_type=t, value=v) for t, v in terms),
        objections=tuple(ObjectionProposal(category=c, summary=s) for c, s in objections),
        acceptance_signal=accept, decline_signal=decline)


def suppress(db: Database, email: str) -> None:
    with db.transaction() as uow:
        uow.dnc.add(DoNotContactEntry(entry_id="dnc-" + email.split("@")[0], scope=DNCScope.EMAIL, value=email,
                                      reason=DNCReason.OPERATOR, source_ref=EntityRef(kind=RefKind.OPERATOR_COMMAND, id="c"),
                                      created_by="op", created_at=NOW))
