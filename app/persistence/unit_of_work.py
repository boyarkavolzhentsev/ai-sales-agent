from contextlib import AbstractContextManager

from app.persistence.repositories.protocols import (
    AuditRepository,
    CampaignRepository,
    DoNotContactRepository,
    EmailMessageRepository,
    EmailThreadRepository,
    EscalationRepository,
    FollowUpPlanRepository,
    IdempotencyRepository,
    KnowledgeIndexRepository,
    KnowledgeSourceMetaRepository,
    LeadRepository,
    OperatorCommandRepository,
    OperatorResponseRepository,
    OutboundMessageRepository,
    ProspectCompanyRepository,
    ProspectContactRepository,
    ProvenanceRepository,
    QuotaReservationRepository,
)
from app.persistence.repositories.sqlite import (
    SqliteAuditRepository,
    SqliteCampaignRepository,
    SqliteDoNotContactRepository,
    SqliteEmailMessageRepository,
    SqliteEmailThreadRepository,
    SqliteEscalationRepository,
    SqliteFollowUpPlanRepository,
    SqliteIdempotencyRepository,
    SqliteKnowledgeIndexRepository,
    SqliteKnowledgeSourceMetaRepository,
    SqliteLeadRepository,
    SqliteOperatorCommandRepository,
    SqliteOperatorResponseRepository,
    SqliteOutboundMessageRepository,
    SqliteProspectCompanyRepository,
    SqliteProspectContactRepository,
    SqliteProvenanceRepository,
    SqliteQuotaReservationRepository,
)
from app.persistence.transaction import Transaction


class UnitOfWork:
    """All repositories bound to one transaction. Everything done through a UnitOfWork
    commits or rolls back together; obtain one only via ``Database.transaction()``.

    Attributes are typed as the repository protocols, so callers depend on interfaces.
    """

    def __init__(self, tx: Transaction) -> None:
        self._tx = tx
        self.companies: ProspectCompanyRepository = SqliteProspectCompanyRepository(tx)
        self.contacts: ProspectContactRepository = SqliteProspectContactRepository(tx)
        self.leads: LeadRepository = SqliteLeadRepository(tx)
        self.threads: EmailThreadRepository = SqliteEmailThreadRepository(tx)
        self.messages: EmailMessageRepository = SqliteEmailMessageRepository(tx)
        self.campaigns: CampaignRepository = SqliteCampaignRepository(tx)
        self.outbound: OutboundMessageRepository = SqliteOutboundMessageRepository(tx)
        self.follow_ups: FollowUpPlanRepository = SqliteFollowUpPlanRepository(tx)
        self.dnc: DoNotContactRepository = SqliteDoNotContactRepository(tx)
        self.escalations: EscalationRepository = SqliteEscalationRepository(tx)
        self.operator_commands: OperatorCommandRepository = SqliteOperatorCommandRepository(tx)
        self.operator_responses: OperatorResponseRepository = SqliteOperatorResponseRepository(tx)
        self.audit: AuditRepository = SqliteAuditRepository(tx)
        self.provenance: ProvenanceRepository = SqliteProvenanceRepository(tx)
        self.knowledge_sources: KnowledgeSourceMetaRepository = SqliteKnowledgeSourceMetaRepository(tx)
        self.knowledge_index: KnowledgeIndexRepository = SqliteKnowledgeIndexRepository(tx)
        self.idempotency: IdempotencyRepository = SqliteIdempotencyRepository(tx)
        self.quota_reservations: QuotaReservationRepository = SqliteQuotaReservationRepository(tx)

    def savepoint(self) -> AbstractContextManager[None]:
        """All-or-nothing sub-unit: writes inside it are undone if it raises, while the
        surrounding transaction continues."""
        return self._tx.savepoint()
