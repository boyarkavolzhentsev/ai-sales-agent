from app.persistence.repositories.protocols import (
    AuditRepository,
    CampaignRepository,
    DoNotContactRepository,
    EmailMessageRepository,
    EmailThreadRepository,
    EscalationRepository,
    FollowUpPlanRepository,
    IdempotencyRepository,
    KnowledgeSourceMetaRepository,
    LeadRepository,
    OperatorCommandRepository,
    OperatorResponseRepository,
    OutboundMessageRepository,
    ProspectCompanyRepository,
    ProspectContactRepository,
    ProvenanceRepository,
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
    SqliteKnowledgeSourceMetaRepository,
    SqliteLeadRepository,
    SqliteOperatorCommandRepository,
    SqliteOperatorResponseRepository,
    SqliteOutboundMessageRepository,
    SqliteProspectCompanyRepository,
    SqliteProspectContactRepository,
    SqliteProvenanceRepository,
)
from app.persistence.transaction import Transaction


class UnitOfWork:
    """All repositories bound to one transaction. Everything done through a UnitOfWork
    commits or rolls back together; obtain one only via ``Database.transaction()``.

    Attributes are typed as the repository protocols, so callers depend on interfaces.
    """

    def __init__(self, tx: Transaction) -> None:
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
        self.idempotency: IdempotencyRepository = SqliteIdempotencyRepository(tx)
