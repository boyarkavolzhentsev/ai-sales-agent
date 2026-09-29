"""SQLite implementations of the repository protocols. Parameterized SQL only."""

from app.persistence.repositories.sqlite.audit import SqliteAuditRepository
from app.persistence.repositories.sqlite.campaigns import SqliteCampaignRepository
from app.persistence.repositories.sqlite.companies import SqliteProspectCompanyRepository
from app.persistence.repositories.sqlite.contacts import SqliteProspectContactRepository
from app.persistence.repositories.sqlite.dispatch_attempts import SqliteDispatchAttemptRepository
from app.persistence.repositories.sqlite.dnc import SqliteDoNotContactRepository
from app.persistence.repositories.sqlite.escalations import SqliteEscalationRepository
from app.persistence.repositories.sqlite.followups import SqliteFollowUpPlanRepository
from app.persistence.repositories.sqlite.idempotency import SqliteIdempotencyRepository
from app.persistence.repositories.sqlite.knowledge_index import SqliteKnowledgeIndexRepository
from app.persistence.repositories.sqlite.knowledge_meta import SqliteKnowledgeSourceMetaRepository
from app.persistence.repositories.sqlite.leads import SqliteLeadRepository
from app.persistence.repositories.sqlite.messages import SqliteEmailMessageRepository
from app.persistence.repositories.sqlite.operator import (
    SqliteOperatorCommandRepository,
    SqliteOperatorResponseRepository,
)
from app.persistence.repositories.sqlite.outbound import SqliteOutboundMessageRepository
from app.persistence.repositories.sqlite.provenance import SqliteProvenanceRepository
from app.persistence.repositories.sqlite.quota_reservations import (
    SqliteQuotaReservationRepository,
)
from app.persistence.repositories.sqlite.threads import SqliteEmailThreadRepository

__all__ = [
    "SqliteAuditRepository",
    "SqliteCampaignRepository",
    "SqliteDispatchAttemptRepository",
    "SqliteDoNotContactRepository",
    "SqliteEmailMessageRepository",
    "SqliteEmailThreadRepository",
    "SqliteEscalationRepository",
    "SqliteFollowUpPlanRepository",
    "SqliteIdempotencyRepository",
    "SqliteKnowledgeIndexRepository",
    "SqliteKnowledgeSourceMetaRepository",
    "SqliteLeadRepository",
    "SqliteOperatorCommandRepository",
    "SqliteOperatorResponseRepository",
    "SqliteOutboundMessageRepository",
    "SqliteProspectCompanyRepository",
    "SqliteProspectContactRepository",
    "SqliteProvenanceRepository",
    "SqliteQuotaReservationRepository",
]
