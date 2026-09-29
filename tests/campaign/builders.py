"""Builders for Stage 10 tests: imported prospects, a campaign activated by an operator,
the real Stage 7 approval and Stage 8 dispatch with the fake transport, FrozenClock and a
file-backed temporary SQLite database. All data is fictional."""

from datetime import datetime, timedelta

from app.campaign import (
    CampaignClaim,
    CampaignEnroller,
    CampaignExecutionConfig,
    CampaignExecutor,
    CampaignScheduler,
    ExecutionResult,
    member_id_for,
)
from app.core.enums import CampaignStatus, ContactDepartment, ContactSource, ContactType, KnowledgeDomain
from app.core.models import Campaign, CampaignMember, CampaignTargetFilter, OutboundMessage, ProspectCompany, ProspectContact
from app.dispatch import DispatchResult, FakeEmailTransport
from app.llm import SenderIdentity
from app.operator import ActivateCampaign
from app.persistence import Database, FrozenClock
from app.policy import KillSwitchState, LimitPolicy
from tests.dispatch.builders import dispatcher, send
from tests.inbound.builders import MAILBOX, NOW
from tests.operator.builders import AS_ALICE, approve_command, operator
from tests.policy import builders as policy

CAMPAIGN_ID = "camp-1"
INTERVAL = timedelta(days=2)
PROSPECT = "lena@acme-prospect.example"
VALUE_QUESTION = "Which integrations does the Sample Widget have?"


def config(**overrides: object) -> CampaignExecutionConfig:
    base: dict[str, object] = {
        "sender": SenderIdentity(sender_name="Alex Seller", company_name="Samplewidget Co"),
        "limits": policy.limits(sends=20, new_contacts=20, follow_ups=20),
        "window": policy.window(),
        "kill_switch": KillSwitchState(enabled=False, changed_at=NOW, changed_by="ops"),
        "value_question": VALUE_QUESTION,
    }
    return CampaignExecutionConfig.model_validate(base | overrides)


def one_new_contact() -> LimitPolicy:
    return policy.limits(sends=20, new_contacts=1, follow_ups=20)


def add_campaign(db: Database, campaign_id: str = CAMPAIGN_ID, *, max_follow_ups: int = 1, status: CampaignStatus = CampaignStatus.DRAFT,
                 domains: tuple[KnowledgeDomain, ...] = (KnowledgeDomain.PRODUCTS_SERVICES, KnowledgeDomain.COMPANY)) -> Campaign:
    campaign = Campaign(
        campaign_id=campaign_id, name="Spring outreach", status=status, target_filter=CampaignTargetFilter(),
        allowed_knowledge_domains=domains, sending_mailbox=MAILBOX, max_follow_ups=max_follow_ups,
        min_interval_between_follow_ups=INTERVAL, created_by="op", activated_by="op" if status is not CampaignStatus.DRAFT else None,
        created_at=NOW, updated_at=NOW,
    )
    with db.transaction() as uow:
        uow.campaigns.add(campaign)
    return campaign


def add_prospect(db: Database, email: str = PROSPECT, *, name: str | None = "Lena", company_name: str | None = "Acme Prospect Ltd",
                 contact_id: str | None = None) -> ProspectContact:
    domain = email.split("@", 1)[1]
    slug = email.replace("@", "-at-").replace(".", "-")
    company_id = f"co-{domain.replace('.', '-')}" if company_name else None
    contact = ProspectContact(
        contact_id=contact_id or f"ct-{slug}", company_id=company_id, email=email, name=name, department=ContactDepartment.SALES,
        contact_type=ContactType.NAMED_BUSINESS, source=ContactSource.IMPORT, collected_at=NOW, created_at=NOW, updated_at=NOW,
    )
    with db.transaction() as uow:
        if company_name and uow.companies.get(company_id or "") is None:
            uow.companies.add(ProspectCompany(company_id=company_id or "", name=company_name, domain=domain,
                                              source=ContactSource.IMPORT, created_at=NOW, updated_at=NOW))
        uow.contacts.add(contact)
    return contact


def activate(db: Database, campaign_id: str = CAMPAIGN_ID, clock: FrozenClock | None = None) -> None:
    service = operator(db, clock)
    with db.transaction() as uow:
        campaign = uow.campaigns.get(campaign_id)
    assert campaign is not None
    service.activate_campaign(AS_ALICE, ActivateCampaign(command_id=f"cmd-activate-{campaign_id}", correlation_id="c",
                                                         campaign_id=campaign_id, expected_campaign_version=campaign.version))


def enroller(db: Database, clock: FrozenClock | None = None) -> CampaignEnroller:
    return CampaignEnroller(db, clock or FrozenClock(NOW))


def scheduler(db: Database, clock: FrozenClock | None = None, **overrides: object) -> CampaignScheduler:
    return CampaignScheduler(db, clock or FrozenClock(NOW), config(**overrides))


def executor(db: Database, clock: FrozenClock | None = None, **overrides: object) -> CampaignExecutor:
    return CampaignExecutor(db, clock or FrozenClock(NOW), config(**overrides))


def claim_all(db: Database, clock: FrozenClock, worker: str = "w1") -> tuple[CampaignClaim, ...]:
    return scheduler(db, clock).claim_due(worker, correlation_id=f"corr-claim-{worker}")


def enrolled(db: Database, email: str = PROSPECT, *, name: str | None = "Lena", company_name: str | None = "Acme Prospect Ltd") -> str:
    contact = add_prospect(db, email, name=name, company_name=company_name)
    result = enroller(db).enroll(CAMPAIGN_ID, contact.contact_id, correlation_id="corr-enroll")
    assert result.member_id is not None
    return result.member_id


def ready_campaign(db: Database, email: str = PROSPECT, *, max_follow_ups: int = 1) -> str:
    """A campaign activated by an operator with one enrolled prospect; returns the member id."""
    add_campaign(db, max_follow_ups=max_follow_ups)
    activate(db)
    return enrolled(db, email)


def draft_touch(db: Database, at: datetime = NOW, **overrides: object) -> ExecutionResult:
    """Schedule, claim and execute at ``at``: exactly one draft expected."""
    clock = FrozenClock(at)
    scheduler(db, clock, **overrides).schedule(CAMPAIGN_ID, correlation_id="corr-schedule")
    [claim] = claim_all(db, clock)
    result = executor(db, clock, **overrides).execute(claim, correlation_id="corr-exec")
    assert result.outcome.value == "DRAFT_CREATED", result
    return result


def approve(db: Database, outbound_id: str, at: datetime = NOW) -> None:
    service = operator(db, FrozenClock(at))
    service.approve_draft(AS_ALICE, approve_command(service.get_draft(AS_ALICE, outbound_id), f"cmd-approve-{outbound_id}"))


def send_touch(db: Database, outbound_id: str, at: datetime = NOW, transport: FakeEmailTransport | None = None) -> DispatchResult:
    approve(db, outbound_id, at)
    return send(dispatcher(db, transport, clock=FrozenClock(at)), outbound_id, f"corr-send-{outbound_id}")


def member(db: Database, member_id: str) -> CampaignMember:
    with db.transaction() as uow:
        found = uow.campaign_members.get(member_id)
    assert found is not None
    return found


def outbound(db: Database, outbound_id: str) -> OutboundMessage:
    with db.transaction() as uow:
        found = uow.outbound.get(outbound_id)
    assert found is not None
    return found


def campaign_messages(db: Database, lead_id: str) -> list[OutboundMessage]:
    with db.transaction() as uow:
        return [m for m in uow.outbound.list_by_lead(lead_id) if m.idempotency_key.startswith("campaign:")]


__all__ = ["member_id_for"]
