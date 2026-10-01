"""Stage 14 execution vocabulary: owners, actions, normalized blockers, the per-lead plan,
the canonical execution view, execution results, queues and metrics.

IDs, statuses, versions and codes only: no message bodies, no provider identifiers.
"""

from collections.abc import Mapping
from enum import StrEnum

from pydantic import AwareDatetime

from app.core.enums import (
    CampaignMemberStatus,
    CampaignStatus,
    CloseReason,
    CommercialStage,
    ConversationStatus,
    FollowUpJobStatus,
    LeadIntent,
    LeadStage,
    LeadStatus,
    OpportunityStatus,
    QualificationStatus,
    RevisionStatus,
)
from app.core.models.base import CoreModel
from app.core.models.types import EntityId
from app.operator.models import CommandKind


class ExecutionOwner(StrEnum):
    """Who holds the authority to progress the lead right now. Exactly one per plan.

    There is deliberately no PIPELINE or COMMERCIAL owner: every Stage 12/13 progression is
    an operator command by design (no automatic transition exists there), so those steps
    are owned by OPERATOR with ``subsystem`` PIPELINE or COMMERCIAL."""

    DISPATCH_RECOVERY = "DISPATCH_RECOVERY"  # Stage 8 reconciliation must resolve an attempt first
    OPERATOR = "OPERATOR"  # a human decision or command is required
    CAMPAIGN = "CAMPAIGN"  # Stage 10 pre-reply automation
    CONVERSATION = "CONVERSATION"  # Stage 9 post-reply automation
    CUSTOMER = "CUSTOMER"  # waiting for the customer
    NONE = "NONE"  # nobody: closed, suppressed or nothing to do


class ExecutionSubsystem(StrEnum):
    """The subsystem whose existing operation the action points to."""

    DISPATCH = "DISPATCH"
    CAMPAIGN = "CAMPAIGN"
    CONVERSATION = "CONVERSATION"
    ESCALATION = "ESCALATION"  # Stage 6/7 escalation and hold handling
    PIPELINE = "PIPELINE"
    COMMERCIAL = "COMMERCIAL"
    NONE = "NONE"


class ExecutionAction(StrEnum):
    NO_ACTION = "NO_ACTION"  # closed lead, or nothing pending
    NO_AUTOMATION = "NO_AUTOMATION"  # do-not-contact: history readable, nothing progresses
    WAIT_FOR_CUSTOMER = "WAIT_FOR_CUSTOMER"
    WAIT_FOR_OPERATOR = "WAIT_FOR_OPERATOR"  # paused conversation, no active conversation, ...
    RECONCILE_DISPATCH = "RECONCILE_DISPATCH"
    PREPARE_CAMPAIGN_TOUCH = "PREPARE_CAMPAIGN_TOUCH"
    COMPLETE_CAMPAIGN_SEQUENCE = "COMPLETE_CAMPAIGN_SEQUENCE"  # every touch sent, final wait over
    REVIEW_CAMPAIGN_DRAFT = "REVIEW_CAMPAIGN_DRAFT"
    REVIEW_REPLY_DRAFT = "REVIEW_REPLY_DRAFT"  # a Stage 6 reply or Stage 9 follow-up draft
    SEND_APPROVED_MESSAGE = "SEND_APPROVED_MESSAGE"
    PROCESS_FOLLOW_UP = "PROCESS_FOLLOW_UP"
    RESPOND_TO_CUSTOMER = "RESPOND_TO_CUSTOMER"
    QUALIFY_LEAD = "QUALIFY_LEAD"
    REVIEW_QUALIFICATION = "REVIEW_QUALIFICATION"
    CREATE_OPPORTUNITY = "CREATE_OPPORTUNITY"
    PREPARE_PROPOSAL = "PREPARE_PROPOSAL"
    REVIEW_PROPOSAL = "REVIEW_PROPOSAL"
    REVISE_PROPOSAL = "REVISE_PROPOSAL"
    PRESENT_PROPOSAL = "PRESENT_PROPOSAL"
    REVIEW_TERM_REQUEST = "REVIEW_TERM_REQUEST"
    HANDLE_OBJECTION = "HANDLE_OBJECTION"
    CONFIRM_ACCEPTANCE = "CONFIRM_ACCEPTANCE"
    MARK_WON = "MARK_WON"
    DECIDE_LOSS = "DECIDE_LOSS"
    ESCALATION_REVIEW = "ESCALATION_REVIEW"


# The only actions Stage 14 may run itself, each through an existing subsystem operation
# that revalidates everything again. Everything else is operator- or customer-gated.
AUTOMATIC_ACTIONS = frozenset({
    ExecutionAction.RECONCILE_DISPATCH,
    ExecutionAction.PREPARE_CAMPAIGN_TOUCH,
    ExecutionAction.COMPLETE_CAMPAIGN_SEQUENCE,
    ExecutionAction.SEND_APPROVED_MESSAGE,
    ExecutionAction.PROCESS_FOLLOW_UP,
})
# Actions that involve outbound automation: refused while the global kill switch is on.
OUTBOUND_ACTIONS = frozenset({
    ExecutionAction.PREPARE_CAMPAIGN_TOUCH,
    ExecutionAction.SEND_APPROVED_MESSAGE,
    ExecutionAction.PROCESS_FOLLOW_UP,
})

C = CommandKind
# The existing Stage 7 operator commands an operator-gated action points to.
OPERATOR_COMMANDS: Mapping[ExecutionAction, tuple[CommandKind, ...]] = {
    ExecutionAction.WAIT_FOR_OPERATOR: (C.RESUME_CONVERSATION, C.CLOSE_CONVERSATION, C.MARK_LEAD_LOST),
    ExecutionAction.REVIEW_CAMPAIGN_DRAFT: (C.APPROVE_DRAFT, C.REJECT_DRAFT),
    ExecutionAction.REVIEW_REPLY_DRAFT: (C.APPROVE_DRAFT, C.REJECT_DRAFT),
    ExecutionAction.RESPOND_TO_CUSTOMER: (C.TAKE_OWNERSHIP, C.CLOSE_CONVERSATION),
    ExecutionAction.QUALIFY_LEAD: (C.RECORD_QUALIFICATION_FACT, C.TAKE_OWNERSHIP),
    ExecutionAction.REVIEW_QUALIFICATION: (C.APPROVE_QUALIFICATION, C.RESOLVE_QUALIFICATION_CONFLICT,
                                           C.RECORD_QUALIFICATION_FACT, C.DISQUALIFY_LEAD),
    ExecutionAction.CREATE_OPPORTUNITY: (C.CREATE_OPPORTUNITY, C.DISQUALIFY_LEAD),
    ExecutionAction.PREPARE_PROPOSAL: (C.CREATE_PROPOSAL, C.UPDATE_PROPOSAL, C.SET_COMMERCIAL_TERM),
    ExecutionAction.REVIEW_PROPOSAL: (C.APPROVE_PROPOSAL, C.UPDATE_PROPOSAL, C.WITHDRAW_PROPOSAL),
    ExecutionAction.REVISE_PROPOSAL: (C.REVISE_PROPOSAL, C.WITHDRAW_PROPOSAL),
    ExecutionAction.PRESENT_PROPOSAL: (C.MARK_PROPOSAL_PRESENTED, C.WITHDRAW_PROPOSAL),
    ExecutionAction.REVIEW_TERM_REQUEST: (C.APPROVE_TERM_REQUEST, C.REJECT_TERM_REQUEST),
    ExecutionAction.HANDLE_OBJECTION: (C.UPDATE_OBJECTION, C.REVISE_PROPOSAL),
    ExecutionAction.CONFIRM_ACCEPTANCE: (C.MARK_PROPOSAL_ACCEPTED, C.DISMISS_COMMERCIAL_SIGNAL),
    ExecutionAction.MARK_WON: (C.MARK_LEAD_WON,),
    ExecutionAction.DECIDE_LOSS: (C.MARK_PROPOSAL_DECLINED, C.MARK_LEAD_LOST, C.DISMISS_COMMERCIAL_SIGNAL),
    ExecutionAction.ESCALATION_REVIEW: (C.RESOLVE_ESCALATION, C.TAKE_OWNERSHIP, C.APPROVE_DRAFT, C.REJECT_DRAFT),
}
# The existing automated operation each automatic action runs (one per action).
AUTOMATIC_OPERATIONS: Mapping[ExecutionAction, str] = {
    ExecutionAction.RECONCILE_DISPATCH: "dispatch.reconcile",
    ExecutionAction.PREPARE_CAMPAIGN_TOUCH: "campaign.schedule_member/claim/execute",
    ExecutionAction.COMPLETE_CAMPAIGN_SEQUENCE: "campaign.schedule_member",
    ExecutionAction.SEND_APPROVED_MESSAGE: "dispatch.dispatch",
    ExecutionAction.PROCESS_FOLLOW_UP: "follow_up.schedule/claim/execute",
}

A = ExecutionAction
# Batch order (lower first): safety and operator escalations, reconciliation, customer
# replies needing handling, operator review queues, approved sends, due follow-ups,
# campaign work, qualification work, commercial work, then waits.
PRIORITY: Mapping[ExecutionAction, int] = {
    A.ESCALATION_REVIEW: 0, A.RECONCILE_DISPATCH: 1,
    A.REVIEW_REPLY_DRAFT: 2, A.RESPOND_TO_CUSTOMER: 2, A.QUALIFY_LEAD: 2,
    A.REVIEW_CAMPAIGN_DRAFT: 3, A.REVIEW_QUALIFICATION: 3, A.CONFIRM_ACCEPTANCE: 3, A.DECIDE_LOSS: 3,
    A.REVIEW_TERM_REQUEST: 3, A.REVIEW_PROPOSAL: 3, A.HANDLE_OBJECTION: 3,
    A.SEND_APPROVED_MESSAGE: 4, A.PROCESS_FOLLOW_UP: 5, A.PREPARE_CAMPAIGN_TOUCH: 6, A.COMPLETE_CAMPAIGN_SEQUENCE: 6,
    A.CREATE_OPPORTUNITY: 7, A.PREPARE_PROPOSAL: 8, A.REVISE_PROPOSAL: 8, A.PRESENT_PROPOSAL: 8, A.MARK_WON: 8,
    A.WAIT_FOR_OPERATOR: 9, A.WAIT_FOR_CUSTOMER: 10, A.NO_AUTOMATION: 11, A.NO_ACTION: 12,
}


class ExecutionBlocker(StrEnum):
    """Normalized cross-subsystem blockers (source codes are kept in ``sources``)."""

    DNC = "DNC"
    LEAD_CLOSED = "LEAD_CLOSED"
    UNRESOLVED_DISPATCH = "UNRESOLVED_DISPATCH"
    DISPATCH_ACCEPTANCE_CONFLICT = "DISPATCH_ACCEPTANCE_CONFLICT"
    ESCALATION_OPEN = "ESCALATION_OPEN"
    LEAD_ON_HOLD = "LEAD_ON_HOLD"
    OPERATOR_REVIEW_REQUIRED = "OPERATOR_REVIEW_REQUIRED"
    WAITING_FOR_CUSTOMER = "WAITING_FOR_CUSTOMER"
    CAMPAIGN_PRE_REPLY_OWNERSHIP = "CAMPAIGN_PRE_REPLY_OWNERSHIP"
    CAMPAIGN_NOT_ACTIVE = "CAMPAIGN_NOT_ACTIVE"
    CONVERSATION_PAUSED = "CONVERSATION_PAUSED"
    NO_ACTIVE_CONVERSATION = "NO_ACTIVE_CONVERSATION"
    WORK_IN_PROGRESS = "WORK_IN_PROGRESS"  # another worker holds an unexpired lease on the job
    NOT_DUE = "NOT_DUE"
    QUALIFICATION_INCOMPLETE = "QUALIFICATION_INCOMPLETE"
    QUALIFICATION_CONFLICT = "QUALIFICATION_CONFLICT"
    CUSTOMER_DECLINED = "CUSTOMER_DECLINED"
    OPPORTUNITY_REQUIRED = "OPPORTUNITY_REQUIRED"
    COMMERCIAL_INPUT_MISSING = "COMMERCIAL_INPUT_MISSING"
    TERM_REQUEST_OPEN = "TERM_REQUEST_OPEN"
    OBJECTION_OPEN = "OBJECTION_OPEN"
    PROPOSAL_REVIEW_REQUIRED = "PROPOSAL_REVIEW_REQUIRED"
    PROPOSAL_NOT_PRESENTED = "PROPOSAL_NOT_PRESENTED"
    PROPOSAL_REVISION_REQUIRED = "PROPOSAL_REVISION_REQUIRED"
    ACCEPTANCE_REQUIRES_OPERATOR = "ACCEPTANCE_REQUIRES_OPERATOR"
    DECLINE_REQUIRES_OPERATOR = "DECLINE_REQUIRES_OPERATOR"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    SEND_BLOCKED_BY_POLICY = "SEND_BLOCKED_BY_POLICY"
    PROVIDER_CAPABILITY_MISSING = "PROVIDER_CAPABILITY_MISSING"
    LLM_CAPABILITY_MISSING = "LLM_CAPABILITY_MISSING"


class ExecutionCapabilities(CoreModel):
    """What the runtime has configured (Stage 11). A missing capability never changes
    business state: the logical action stays, it is just not executable now."""

    dispatch: bool = False  # an email transport
    reconciliation: bool = False  # a transport plus a read-only reconciler
    llm: bool = False  # an LLM transport (Stage 6 composition)


class EntityRefs(CoreModel):
    """The entities the next action concerns (IDs only)."""

    outbound_ids: tuple[EntityId, ...] = ()
    escalation_ids: tuple[EntityId, ...] = ()
    conversation_id: EntityId | None = None
    follow_up_id: EntityId | None = None
    campaign_id: EntityId | None = None
    member_id: EntityId | None = None
    job_id: EntityId | None = None
    opportunity_id: EntityId | None = None
    proposal_id: EntityId | None = None
    revision_id: EntityId | None = None
    request_ids: tuple[EntityId, ...] = ()
    objection_ids: tuple[EntityId, ...] = ()
    signal_ids: tuple[EntityId, ...] = ()
    conflict_ids: tuple[EntityId, ...] = ()


class SalesExecutionPlan(CoreModel):
    """The single next business action for one lead, derived read-only from one
    transactionally consistent snapshot. ``executable`` is True only for an automatic
    action with no blocker. ``fingerprint`` hashes the normalized durable state the
    decision rests on (never message bodies) and the decision itself; ``observed_at`` is
    not part of it, so the same durable state gives the same fingerprint."""

    lead_id: EntityId
    contact_id: EntityId
    owner: ExecutionOwner
    action: ExecutionAction
    subsystem: ExecutionSubsystem
    executable: bool
    requires_operator: bool
    requires_customer: bool
    blockers: tuple[ExecutionBlocker, ...] = ()
    sources: tuple[str, ...] = ()  # source subsystem codes behind the blockers ("policy:KILL_SWITCH", ...)
    conditions: tuple[ExecutionBlocker, ...] = ()  # every normalized cross-subsystem condition, for context
    reasons: tuple[str, ...] = ()
    refs: EntityRefs = EntityRefs()
    operator_commands: tuple[CommandKind, ...] = ()
    operation: str | None = None  # the existing automated operation, for automatic actions
    expected_versions: dict[str, int] = {}
    waiting_until: AwareDatetime | None = None
    priority: int
    last_activity_at: AwareDatetime
    observed_at: AwareDatetime
    fingerprint: str


class SalesExecutionView(CoreModel):
    """The canonical, provider-neutral business state of one lead and its plan."""

    lead_id: EntityId
    contact_id: EntityId
    lead_stage: LeadStage
    lead_status: LeadStatus
    lead_close_reason: CloseReason | None = None
    last_intent: LeadIntent | None = None
    suppressed: bool
    campaign_id: EntityId | None = None
    campaign_status: CampaignStatus | None = None
    campaign_member_status: CampaignMemberStatus | None = None
    conversation_id: EntityId | None = None
    conversation_status: ConversationStatus | None = None
    follow_up_status: FollowUpJobStatus | None = None
    follow_up_due_at: AwareDatetime | None = None
    unresolved_outbound_ids: tuple[EntityId, ...] = ()
    pending_review_outbound_ids: tuple[EntityId, ...] = ()
    approved_outbound_ids: tuple[EntityId, ...] = ()
    open_escalation_ids: tuple[EntityId, ...] = ()
    qualification_status: QualificationStatus
    opportunity_id: EntityId | None = None
    opportunity_status: OpportunityStatus | None = None
    commercial_stage: CommercialStage | None = None
    revision_status: RevisionStatus | None = None
    plan: SalesExecutionPlan
    last_activity_at: AwareDatetime


class ExecutionOutcome(StrEnum):
    EXECUTED = "EXECUTED"  # the subsystem operation ran (its own outcome is in ``subsystem_outcome``)
    SKIPPED = "SKIPPED"  # nothing to run now (e.g. another worker holds the job)
    BLOCKED = "BLOCKED"  # the plan has blockers, or dispatch was not requested
    STALE_PLAN = "STALE_PLAN"  # durable state changed since planning: replan required
    NO_ACTION = "NO_ACTION"
    REQUIRES_OPERATOR = "REQUIRES_OPERATOR"  # never impersonated: an operator command is needed
    REQUIRES_CUSTOMER = "REQUIRES_CUSTOMER"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    REPLAYED = "REPLAYED"  # this execution id already executed exactly this plan
    ERROR = "ERROR"  # an unexpected exception (type name only) or an execution-id collision


class ExecutionResult(CoreModel):
    lead_id: EntityId
    planned_action: ExecutionAction
    outcome: ExecutionOutcome
    state_changed: bool
    reason: str | None = None
    subsystem_outcome: str | None = None
    reason_codes: tuple[str, ...] = ()
    correlation_id: EntityId
    execution_id: EntityId | None = None
    plan_fingerprint: str  # the fingerprint the caller expected
    resulting_fingerprint: str | None = None  # after re-planning


class ExecutionQueue(StrEnum):
    ACTIONABLE = "ACTIONABLE"  # executable now by automation
    OPERATOR = "OPERATOR"  # an operator must act
    CUSTOMER = "CUSTOMER"  # waiting for the customer
    RECOVERY = "RECOVERY"  # dispatch reconciliation required
    BLOCKED = "BLOCKED"  # neither executable nor waiting on a person (DNC, kill switch, capability, not due)
    COMMERCIAL = "COMMERCIAL"  # the next step is commercial (proposal, terms, objections, signals)
    CONVERSATION = "CONVERSATION"  # conversation automation owns the lead
    CAMPAIGN = "CAMPAIGN"  # pre-reply campaign automation owns the lead
    UNOWNED = "UNOWNED"  # an open lead with no current owner


class ExecutionPassResult(CoreModel):
    """One bounded pass: at most ``limit`` leads, at most one action each, no loop."""

    correlation_id: EntityId
    considered: int  # actionable leads found
    attempted: int
    results: tuple[ExecutionResult, ...] = ()

    def count(self, outcome: ExecutionOutcome) -> int:
        return sum(1 for r in self.results if r.outcome is outcome)


class ExecutionMetrics(CoreModel):
    """Derived from current plans of open leads (closed leads are counted from the lead
    table). No durations: entry timestamps per ownership are not recorded."""

    open_leads: int
    closed_leads: int
    by_owner: dict[ExecutionOwner, int]
    by_action: dict[ExecutionAction, int]
    actionable: int
    waiting_operator: int
    waiting_customer: int
    blocked: int
    recovery_required: int
    no_action: int
