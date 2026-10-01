from contextlib import AbstractContextManager

from app.persistence.repositories.protocols import (
    AuditRepository,
    CampaignJobRepository,
    CampaignMemberRepository,
    CampaignRepository,
    ConversationRepository,
    DispatchAttemptRepository,
    DoNotContactRepository,
    EmailMessageRepository,
    EmailThreadRepository,
    EscalationRepository,
    FollowUpJobRepository,
    FollowUpPlanRepository,
    IdempotencyRepository,
    KnowledgeIndexRepository,
    KnowledgeSourceMetaRepository,
    LeadQualificationRepository,
    LeadRepository,
    MailboxSyncRepository,
    OperatorCommandRepository,
    OperatorResponseRepository,
    OpportunityRepository,
    OutboundMessageRepository,
    ProspectCompanyRepository,
    ProspectContactRepository,
    ProvenanceRepository,
    QuotaReservationRepository,
)
from app.persistence.repositories.sqlite import (
    SqliteCommercialSignalRepository,
    SqliteCommercialTermRepository,
    SqliteObjectionRepository,
    SqliteProposalRevisionRepository,
    SqliteTermRequestRepository,
    SqliteAuditRepository,
    SqliteCampaignJobRepository,
    SqliteCampaignMemberRepository,
    SqliteCampaignRepository,
    SqliteConversationRepository,
    SqliteDispatchAttemptRepository,
    SqliteDoNotContactRepository,
    SqliteEmailMessageRepository,
    SqliteEmailThreadRepository,
    SqliteEscalationRepository,
    SqliteFollowUpJobRepository,
    SqliteFollowUpPlanRepository,
    SqliteIdempotencyRepository,
    SqliteKnowledgeIndexRepository,
    SqliteKnowledgeSourceMetaRepository,
    SqliteLeadQualificationRepository,
    SqliteLeadRepository,
    SqliteMailboxSyncRepository,
    SqliteOperatorChannelRepository,
    SqliteOperatorCommandRepository,
    SqliteOperatorResponseRepository,
    SqliteOpportunityRepository,
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
        self.dispatch_attempts: DispatchAttemptRepository = SqliteDispatchAttemptRepository(tx)
        self.conversations: ConversationRepository = SqliteConversationRepository(tx)
        self.follow_up_jobs: FollowUpJobRepository = SqliteFollowUpJobRepository(tx)
        self.campaign_members: CampaignMemberRepository = SqliteCampaignMemberRepository(tx)
        self.campaign_jobs: CampaignJobRepository = SqliteCampaignJobRepository(tx)
        self.qualifications: LeadQualificationRepository = SqliteLeadQualificationRepository(tx)
        self.opportunities: OpportunityRepository = SqliteOpportunityRepository(tx)
        # Commercial decisioning (Stage 13).
        self.proposal_revisions = SqliteProposalRevisionRepository(tx)
        self.commercial_terms = SqliteCommercialTermRepository(tx)
        self.term_requests = SqliteTermRequestRepository(tx)
        self.objections = SqliteObjectionRepository(tx)
        self.commercial_signals = SqliteCommercialSignalRepository(tx)
        # Inbound mailbox synchronization (Stage 16).
        self.mailbox_sync: MailboxSyncRepository = SqliteMailboxSyncRepository(tx)
        # Operator channel synchronization (Stage 17).
        self.operator_channel = SqliteOperatorChannelRepository(tx)

    def savepoint(self) -> AbstractContextManager[None]:
        """All-or-nothing sub-unit: writes inside it are undone if it raises, while the
        surrounding transaction continues."""
        return self._tx.savepoint()
