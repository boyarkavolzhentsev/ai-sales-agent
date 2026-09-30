"""All V1 core enums. Values equal member names (upper-case strings)."""

from app.core.enums.audit import ActorType, RefKind
from app.core.enums.campaign import (
    SUPPORTED_REVIEW_MODES_V1,
    CampaignReviewMode,
    CampaignStatus,
    is_review_mode_supported_v1,
)
from app.core.enums.campaign_member import CampaignJobStatus, CampaignMemberStatus
from app.core.enums.conversation import ConversationStatus, FollowUpJobStatus
from app.core.enums.dnc import DNCReason, DNCScope
from app.core.enums.email import EmailDirection
from app.core.enums.escalation import (
    EscalationReason,
    EscalationResolution,
    EscalationSeverity,
    EscalationStatus,
)
from app.core.enums.knowledge import (
    KnowledgeApprovalStatus,
    KnowledgeDecision,
    KnowledgeDomain,
    KnowledgeExternalUse,
    KnowledgePurpose,
)
from app.core.enums.lead import CloseReason, LeadIntent, LeadOrigin, LeadStage, LeadStatus
from app.core.enums.operator import OperatorCommandKind, OperatorResponseStatus
from app.core.enums.pipeline import (
    BlockerCode,
    ConflictResolution,
    ConflictStatus,
    DisqualificationReason,
    FactSource,
    LostReason,
    NextActionOwner,
    NextActionType,
    OpportunityStatus,
    PipelineTrigger,
    QualificationStatus,
)
from app.core.enums.outbound import (
    FollowUpCancelReason,
    FollowUpStatus,
    OutboundDecision,
    OutboundKind,
    OutboundStatus,
)
from app.core.enums.prospect import (
    ContactDepartment,
    ContactSource,
    ContactType,
    EmailValidity,
    IcpFit,
)
from app.core.enums.reply import (
    ClaimCheckStatus,
    ConfidenceBand,
    DraftPurpose,
    DraftReviewStatus,
    ReplyDecision,
    RiskFlag,
)

__all__ = [
    "BlockerCode",
    "ConflictResolution",
    "ConflictStatus",
    "DisqualificationReason",
    "FactSource",
    "LostReason",
    "NextActionOwner",
    "NextActionType",
    "OpportunityStatus",
    "PipelineTrigger",
    "QualificationStatus",
    "SUPPORTED_REVIEW_MODES_V1",
    "ActorType",
    "CampaignJobStatus",
    "CampaignMemberStatus",
    "CampaignReviewMode",
    "CampaignStatus",
    "ClaimCheckStatus",
    "CloseReason",
    "ConfidenceBand",
    "ContactDepartment",
    "ContactSource",
    "ContactType",
    "ConversationStatus",
    "DNCReason",
    "DNCScope",
    "DraftPurpose",
    "DraftReviewStatus",
    "EmailDirection",
    "EmailValidity",
    "EscalationReason",
    "EscalationResolution",
    "EscalationSeverity",
    "EscalationStatus",
    "FollowUpCancelReason",
    "FollowUpJobStatus",
    "FollowUpStatus",
    "IcpFit",
    "KnowledgeApprovalStatus",
    "KnowledgeDecision",
    "KnowledgeDomain",
    "KnowledgeExternalUse",
    "KnowledgePurpose",
    "LeadIntent",
    "LeadOrigin",
    "LeadStage",
    "LeadStatus",
    "OperatorCommandKind",
    "OperatorResponseStatus",
    "OutboundDecision",
    "OutboundKind",
    "OutboundStatus",
    "RefKind",
    "ReplyDecision",
    "RiskFlag",
    "is_review_mode_supported_v1",
]
