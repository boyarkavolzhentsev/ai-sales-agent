"""Builders for Stage 14 tests: the real Stage 11 runtime with deterministic fakes only
(fake email transport and reconciler, scripted LLM, fake extractors, fake operator
authenticator), FrozenClock and a file-backed temporary SQLite database.

Operator steps go through the real Stage 7 OperatorService with the fake credential; the
coordinator itself never receives a credential."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.commercial.fake import FakeCommercialExtractor
from app.core.enums import DNCReason, DNCScope, LeadIntent, RefKind, TermType
from app.core.models import DoNotContactEntry, EntityRef, Lead, OutboundMessage
from app.dispatch import FakeEmailTransport, FakeReconciler
from app.inbound import InboundResult
from app.llm import LLMTask
from app.operator import (
    ApproveProposal,
    MarkProposalAccepted,
    MarkProposalPresented,
    OperatorService,
)
from app.campaign import CampaignExecutor, CampaignScheduler
from app.conversation import FollowUpExecutor, FollowUpScheduler
from app.dispatch import DispatchService
from app.orchestration import ExecutionCapabilities, ExecutionResult, OrchestratorConfig, SalesExecutionPlan, SalesOrchestrator
from app.policy import KillSwitchState
from app.persistence import Database, FrozenClock
from app.pipeline.fake import FakeQualificationExtractor
from app.runtime import Adapters, SalesAgentRuntime
from tests.campaign.builders import CAMPAIGN_ID, PROSPECT, add_campaign, add_prospect
from tests.commercial.builders import line, text
from tests.inbound.builders import NOW, PRICE_QUESTION, ComposerScript, ScriptedTransport, classification, envelope, sufficiency
from tests.operator.builders import AS_ALICE, FakeAuthenticator, approve_command
from tests.pipeline.builders import REQUIRED, extraction
from tests.runtime.builders import activate_campaign, app_db, runtime, runtime_config

ANSWER = "Hi, the Basic plan costs 100 EUR per month."


def answering_llm(replies: int = 4) -> ScriptedTransport:
    """``replies`` genuine pricing questions, each answered from approved knowledge."""
    llm = ScriptedTransport()
    for _ in range(replies):
        llm.script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.PRICING_REQUEST, PRICE_QUESTION))
        llm.script(LLMTask.KNOWLEDGE_SUFFICIENCY, sufficiency())
        llm.compose(ComposerScript(body=ANSWER, cite=lambda e: "100 EUR" in e["excerpt"]))
    return llm


@dataclass
class World:
    app: SalesAgentRuntime
    clock: FrozenClock
    transport: FakeEmailTransport
    llm: ScriptedTransport
    qualification: FakeQualificationExtractor
    commercial: FakeCommercialExtractor
    member_id: str | None = None
    lead_id: str | None = None
    rfc_ids: list[str] = field(default_factory=list)

    @property
    def db(self) -> Database:
        return app_db(self.app)

    @property
    def ops(self) -> OperatorService:
        return self.app.services.operator

    def plan(self, lead_id: str | None = None) -> SalesExecutionPlan:
        return self.app.execution_plan(lead_id or self.lead)

    def execute(self, *, dispatch: bool = False, lead_id: str | None = None, **kwargs: object) -> ExecutionResult:
        current = self.plan(lead_id)
        return self.app.execution_once(lead_id or self.lead, current.fingerprint, dispatch_approved=dispatch, **kwargs)  # type: ignore[arg-type]

    @property
    def lead(self) -> str:
        assert self.lead_id is not None
        return self.lead_id

    def lead_row(self) -> Lead:
        with self.db.transaction() as uow:
            found = uow.leads.get(self.lead)
        assert found is not None
        return found

    def messages(self) -> list[OutboundMessage]:
        with self.db.transaction() as uow:
            return uow.outbound.list_by_lead(self.lead)

    def advance(self, delta: timedelta) -> None:
        self.clock.set(self.clock.now() + delta)


def world(db_path: object, *, transport: FakeEmailTransport | None = None, reconciler: bool = True, llm: bool = True,
          dispatch: bool = True, at: datetime = NOW, llm_transport: ScriptedTransport | None = None,
          facts: dict[str, str] | None = None, **config: object) -> World:
    clock = FrozenClock(at)
    transport = transport or FakeEmailTransport()
    scripted = llm_transport or answering_llm()
    qualification = FakeQualificationExtractor(default=extraction(**(REQUIRED if facts is None else facts)))
    commercial = FakeCommercialExtractor()
    adapters = Adapters(
        email_transport=transport if dispatch else None,
        reconciler=FakeReconciler(transport) if dispatch and reconciler else None,
        llm_transport=scripted if llm else None, authenticator=FakeAuthenticator(),
        qualification_extractor=qualification, commercial_extractor=commercial,
    )
    app = runtime(db_path, clock=clock, adapters=adapters, **config)
    app.start()
    return World(app=app, clock=clock, transport=transport, llm=scripted, qualification=qualification, commercial=commercial)


def enrolled(w: World) -> str:
    """An ACTIVE campaign with one enrolled prospect; sets and returns the lead id."""
    add_campaign(w.db)
    contact = add_prospect(w.db)
    activate_campaign(w.app)
    result = w.app.services.campaign_enroller.enroll(CAMPAIGN_ID, contact.contact_id, correlation_id="enroll")
    assert result.member_id is not None
    w.member_id = result.member_id
    with w.db.transaction() as uow:
        member = uow.campaign_members.get(result.member_id)
    assert member is not None and member.lead_id is not None
    w.lead_id = member.lead_id
    return member.lead_id


def approve_pending(w: World) -> list[str]:
    ids = [d.outbound_id for d in w.ops.list_pending_drafts(AS_ALICE)]
    for outbound_id in ids:
        w.ops.approve_draft(AS_ALICE, approve_command(w.ops.get_draft(AS_ALICE, outbound_id), f"cmd-approve-{outbound_id}"))
    return ids


def customer_replies(w: World, provider_message_id: str, *, body: str = "How much is the Basic plan per month?",
                     offset: timedelta = timedelta(minutes=1)) -> InboundResult:
    """A genuine customer reply in the lead's thread, through runtime.handle_inbound."""
    sent = [m for m in w.messages() if m.rfc_message_id is not None]
    assert sent, "nothing was sent to reply to"
    latest = max(sent, key=lambda m: (m.sending_at or m.created_at, m.outbound_id))
    w.advance(offset)
    return w.app.handle_inbound(envelope(provider_message_id, sender=PROSPECT, body=body, in_reply_to=latest.rfc_message_id,
                                         received_at=w.clock.now()), correlation_id=f"corr-{provider_message_id}")


def suppress(w: World, email: str = PROSPECT) -> None:
    with w.db.transaction() as uow:
        uow.dnc.add(DoNotContactEntry(entry_id="dnc-" + email.split("@")[0], scope=DNCScope.EMAIL, value=email,
                                      reason=DNCReason.OPERATOR, source_ref=EntityRef(kind=RefKind.OPERATOR_COMMAND, id="c"),
                                      created_by="op", created_at=w.clock.now()))


# ---- Operator commands (through Stage 7, built from current state) --------------------------------


def approve_qualification(w: World) -> None:
    from app.operator import ApproveQualification
    with w.db.transaction() as uow:
        q = uow.qualifications.get(w.lead)
    assert q is not None
    w.ops.approve_qualification(AS_ALICE, ApproveQualification(
        command_id="cmd-approve-q", correlation_id="c", lead_id=w.lead, expected_lead_version=w.lead_row().version,
        expected_qualification_version=q.version))


def create_opportunity(w: World) -> str:
    from app.operator import CreateOpportunity
    w.ops.create_opportunity(AS_ALICE, CreateOpportunity(command_id="cmd-opportunity", correlation_id="c", lead_id=w.lead,
                                                         expected_lead_version=w.lead_row().version))
    return opportunity_id(w)


def opportunity_id(w: World) -> str:
    with w.db.transaction() as uow:
        found = uow.opportunities.get_active_for_lead(w.lead)
    assert found is not None
    return found.opportunity_id


def prepare_proposal(w: World) -> None:
    from app.operator import CreateProposal, SetCommercialTerm, UpdateProposal
    opp = opportunity_id(w)
    with w.db.transaction() as uow:
        version = uow.opportunities.get(opp).version  # type: ignore[union-attr]
    w.ops.create_proposal(AS_ALICE, CreateProposal(command_id="cmd-proposal", correlation_id="c", opportunity_id=opp,
                                                   expected_opportunity_version=version, currency="EUR"))
    revision = current_revision(w)
    w.ops.update_proposal(AS_ALICE, UpdateProposal(command_id="cmd-update", correlation_id="c", revision_id=revision.revision_id,
                                                   expected_revision_version=revision.version, lines=(line(),)))
    w.ops.set_commercial_term(AS_ALICE, SetCommercialTerm(command_id="cmd-term", correlation_id="c", opportunity_id=opp,
                                                          term_type=TermType.PAYMENT_TERM, value=text("NET_30"),
                                                          expected_term_version=None))


def current_revision(w: World):  # noqa: ANN201 - ProposalRevision
    opp = opportunity_id(w)
    with w.db.transaction() as uow:
        revisions = uow.proposal_revisions.list_for_opportunity(opp)
    return revisions[-1]


def revision_command(w: World, cls: type, command_id: str) -> None:
    revision = current_revision(w)
    method = {ApproveProposal: "approve_proposal", MarkProposalPresented: "mark_proposal_presented",
              MarkProposalAccepted: "mark_proposal_accepted"}[cls]
    getattr(w.ops, method)(AS_ALICE, cls(command_id=command_id, correlation_id="c", revision_id=revision.revision_id,
                                          expected_revision_version=revision.version))


def mark_won(w: World) -> None:
    from app.operator import MarkLeadWon
    with w.db.transaction() as uow:
        opp = uow.opportunities.get_active_for_lead(w.lead)
    assert opp is not None
    w.ops.mark_lead_won(AS_ALICE, MarkLeadWon(command_id="cmd-won", correlation_id="c", lead_id=w.lead,
                                              expected_lead_version=w.lead_row().version,
                                              opportunity_id=opp.opportunity_id, expected_opportunity_version=opp.version))


def mark_lost(w: World, command_id: str = "cmd-lost") -> None:
    from app.core.enums import LostReason
    from app.operator import MarkLeadLost
    with w.db.transaction() as uow:
        opportunity = uow.opportunities.get_active_for_lead(w.lead)
    w.ops.mark_lead_lost(AS_ALICE, MarkLeadLost(command_id=command_id, correlation_id="c", lead_id=w.lead,
                                                expected_lead_version=w.lead_row().version, reason=LostReason.CHOSE_COMPETITOR,
                                                expected_opportunity_version=opportunity.version if opportunity else None))


# ---- A coordinator over a plain Database (reuses the Stage 12/13 position builders) -------------

FULL = ExecutionCapabilities(dispatch=True, reconciliation=True, llm=True)
OFFLINE = ExecutionCapabilities()


def orchestrator(db: Database, *, at: datetime = NOW, clock: FrozenClock | None = None,
                 capabilities: ExecutionCapabilities = FULL, transport: FakeEmailTransport | None = None,
                 kill_switch: bool = False, **config: object) -> SalesOrchestrator:
    clock = clock or FrozenClock(at)
    if kill_switch:
        config["kill_switch"] = KillSwitchState(enabled=True, reason="incident", changed_at=NOW, changed_by="ops")
    cfg = runtime_config(db.path, **config)
    transport = transport or FakeEmailTransport()
    campaign, follow_up = cfg.campaign_config(), cfg.follow_up_config()
    dispatch = (DispatchService(db, clock, cfg.dispatch_config(), transport, FakeReconciler(transport))
                if capabilities.dispatch else None)
    return SalesOrchestrator(
        db, clock,
        OrchestratorConfig(qualification=cfg.pipeline.profile, commercial=cfg.commercial.profile, follow_up=follow_up,
                           dispatch=cfg.dispatch_config(), kill_switch=cfg.kill_switch, worker_id="test-worker"),
        capabilities,
        campaign_scheduler=CampaignScheduler(db, clock, campaign), campaign_executor=CampaignExecutor(db, clock, campaign),
        follow_up_scheduler=FollowUpScheduler(db, clock, follow_up), follow_up_executor=FollowUpExecutor(db, clock, follow_up),
        dispatch=dispatch,
    )


def table_rows(db: Database) -> tuple[object, ...]:
    """Every row of every table: for 'planning wrote nothing' checks."""
    with db.transaction() as uow:
        names = [r[0] for r in uow._tx.fetch_all(  # noqa: SLF001
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return tuple((name, tuple(tuple(row) for row in uow._tx.fetch_all(f"SELECT * FROM {name} ORDER BY 1")))  # noqa: SLF001
                     for name in names)


def reject_pending(db: Database, at: datetime = NOW) -> list[str]:
    """The operator rejects every pending draft (they will reply themselves)."""
    from app.operator import RejectReason
    from tests.operator.builders import operator, reject_command
    service = operator(db, FrozenClock(at))
    ids = [d.outbound_id for d in service.list_pending_drafts(AS_ALICE)]
    for outbound_id in ids:
        service.reject_draft(AS_ALICE, reject_command(service.get_draft(AS_ALICE, outbound_id), f"cmd-reject-{outbound_id}",
                                                      reason=RejectReason.OPERATOR_WILL_REPLY))
    return ids


def quiet_message(db: Database, provider_message_id: str, extraction: object, *, at: datetime) -> None:
    """A customer message that Stage 6 neither answers nor escalates (out-of-office class),
    followed by the commercial hook with a scripted extraction: commercial facts only."""
    from app.commercial import CommercialService
    from tests.inbound.builders import process
    result = process(db, ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.OUT_OF_OFFICE)),
                     envelope(provider_message_id, body="Re: proposal", in_reply_to="<p-1@prospect.example>", received_at=at))
    CommercialService(db, FrozenClock(at), _commercial_config(), extractor=FakeCommercialExtractor(default=extraction)) \
        .record_inbound(result, correlation_id=f"c-{provider_message_id}")  # type: ignore[arg-type]


def _commercial_config():  # noqa: ANN202
    from app.commercial import CommercialConfig
    return CommercialConfig()
