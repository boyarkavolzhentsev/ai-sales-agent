"""Immutable core contracts. No repositories or service methods live here."""

from app.core.models.audit import AuditEvent, ProvenanceRecord
from app.core.models.base import CoreModel
from app.core.models.campaign import Campaign, CampaignTargetFilter
from app.core.models.campaign_member import (
    OPEN_CAMPAIGN_JOB_STATUSES,
    TERMINAL_MEMBER_STATUSES,
    CampaignJob,
    CampaignMember,
)
from app.core.models.classification import IntentClassification
from app.core.models.conversation import (
    OPEN_FOLLOW_UP_JOB_STATUSES,
    TERMINAL_CONVERSATION_STATUSES,
    Conversation,
    FollowUpJob,
)
from app.core.models.dnc import DoNotContactEntry
from app.core.models.draft import EvidenceCitation, MessageDraft
from app.core.models.email import EmailMessage, EmailThread
from app.core.models.escalation import Escalation
from app.core.models.follow_up import FollowUpPlan
from app.core.models.knowledge import (
    KnowledgeAssessment,
    KnowledgeChunk,
    KnowledgeEvidence,
    KnowledgeQuery,
    KnowledgeSource,
    QuestionAssessment,
)
from app.core.models.lead import Lead
from app.core.models.operator import OperatorCommand, OperatorResponse
from app.core.models.outbound import OutboundMessage
from app.core.models.permit import SendPermit
from app.core.models.prospect import ProspectCompany, ProspectContact
from app.core.models.refs import Actor, EntityRef

__all__ = [
    "OPEN_CAMPAIGN_JOB_STATUSES",
    "OPEN_FOLLOW_UP_JOB_STATUSES",
    "TERMINAL_CONVERSATION_STATUSES",
    "TERMINAL_MEMBER_STATUSES",
    "Actor",
    "AuditEvent",
    "Campaign",
    "CampaignJob",
    "CampaignMember",
    "CampaignTargetFilter",
    "CoreModel",
    "DoNotContactEntry",
    "EmailMessage",
    "EmailThread",
    "EntityRef",
    "Escalation",
    "EvidenceCitation",
    "Conversation",
    "FollowUpJob",
    "FollowUpPlan",
    "IntentClassification",
    "KnowledgeAssessment",
    "KnowledgeChunk",
    "KnowledgeEvidence",
    "KnowledgeQuery",
    "KnowledgeSource",
    "Lead",
    "MessageDraft",
    "OperatorCommand",
    "OperatorResponse",
    "OutboundMessage",
    "ProspectCompany",
    "ProspectContact",
    "ProvenanceRecord",
    "QuestionAssessment",
    "SendPermit",
]
