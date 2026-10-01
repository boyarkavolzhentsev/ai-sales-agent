"""End-to-end business cycles through the real runtime and internal fakes only. Every path
advances one step at a time and checks the plan's owner and action at each step; the
coordinator only ever runs the automatic steps, an operator (Stage 7) takes every
decision."""

from pathlib import Path

from app.core.enums import (
    CampaignMemberStatus,
    CloseReason,
    ConversationStatus,
    DNCScope,
    FollowUpJobStatus,
    LeadIntent,
    LeadStage,
    OpportunityStatus,
    RevisionStatus,
    SignalStatus,
)
from app.dispatch import FakeBehavior, FakeEmailTransport
from app.llm import LLMTask
from app.operator import ApproveProposal, MarkProposalAccepted, MarkProposalPresented
from app.orchestration import ExecutionAction as A
from app.orchestration import ExecutionOutcome as X
from app.orchestration import ExecutionOwner as O
from app.orchestration import ExecutionQueue, SalesExecutionPlan
from tests.campaign.builders import PROSPECT, campaign_messages
from tests.commercial.builders import asks
from tests.inbound.builders import ScriptedTransport, classification
from tests.orchestration.builders import (
    World,
    approve_pending,
    approve_qualification,
    create_opportunity,
    current_revision,
    customer_replies,
    enrolled,
    mark_lost,
    mark_won,
    prepare_proposal,
    revision_command,
    suppress,
    world,
)
from tests.runtime.builders import runtime


def step(w: World, owner: O, action: A) -> SalesExecutionPlan:
    plan = w.plan()
    assert (plan.owner, plan.action) == (owner, action), plan
    return plan


def automatic(w: World, owner: O, action: A, expected: str, *, dispatch: bool = False) -> None:
    plan = step(w, owner, action)
    assert plan.executable
    result = w.app.execution_once(w.lead, plan.fingerprint, dispatch_approved=dispatch)
    assert (result.outcome, result.subsystem_outcome) == (X.EXECUTED, expected), result


def operator_gated(w: World, action: A) -> None:
    plan = step(w, O.OPERATOR, action)
    refused = w.app.execution_once(w.lead, plan.fingerprint, dispatch_approved=True)
    assert (refused.outcome, refused.state_changed) == (X.REQUIRES_OPERATOR, False)


def engaged_and_qualified(w: World) -> None:
    """Steps 1-11 of the business cycle: campaign touch, reply, qualification, opportunity."""
    enrolled(w)
    automatic(w, O.CAMPAIGN, A.PREPARE_CAMPAIGN_TOUCH, "DRAFT_CREATED")               # 2. campaign drafts
    operator_gated(w, A.REVIEW_CAMPAIGN_DRAFT)
    approve_pending(w)                                                                # 3. operator approves
    automatic(w, O.CAMPAIGN, A.SEND_APPROVED_MESSAGE, "ACCEPTED", dispatch=True)       # 4. fake dispatch accepted
    assert w.lead_row().stage is LeadStage.CONTACTED                                  # 5. lead CONTACTED
    step(w, O.CUSTOMER, A.WAIT_FOR_CUSTOMER)
    customer_replies(w, "p-reply")                                                    # 6. customer reply
    with w.db.transaction() as uow:                                                   # 7. campaign handoff
        member = uow.campaign_members.get(w.member_id or "")
    assert member is not None and member.status is CampaignMemberStatus.REPLIED
    operator_gated(w, A.REVIEW_REPLY_DRAFT)                                           # 8. conversation active
    approve_pending(w)
    automatic(w, O.CONVERSATION, A.SEND_APPROVED_MESSAGE, "ACCEPTED", dispatch=True)
    operator_gated(w, A.REVIEW_QUALIFICATION)                                         # 9. facts gathered
    approve_qualification(w)                                                          # 10. operator approves
    operator_gated(w, A.CREATE_OPPORTUNITY)
    create_opportunity(w)                                                             # 11. opportunity
    operator_gated(w, A.PREPARE_PROPOSAL)


def presented_proposal(w: World) -> None:
    prepare_proposal(w)                                                               # 12. proposal prepared
    operator_gated(w, A.REVIEW_PROPOSAL)
    revision_command(w, ApproveProposal, "cmd-approve-proposal")                     # 13. operator approves
    operator_gated(w, A.PRESENT_PROPOSAL)
    revision_command(w, MarkProposalPresented, "cmd-present")                        # 14. marks presented
    automatic(w, O.CONVERSATION, A.PROCESS_FOLLOW_UP, "SCHEDULED")                     # Stage 9 policy permits it
    step(w, O.CUSTOMER, A.WAIT_FOR_CUSTOMER)


def test_a_won_happy_path_one_step_at_a_time(db_path: Path) -> None:
    w = world(db_path)
    engaged_and_qualified(w)
    presented_proposal(w)
    w.commercial.default = asks(accept=True)
    customer_replies(w, "p-accept", body="We accept your proposal. What does the Basic plan cost per month?")  # 15.
    operator_gated(w, A.REVIEW_REPLY_DRAFT)
    approve_pending(w)
    automatic(w, O.CONVERSATION, A.SEND_APPROVED_MESSAGE, "ACCEPTED", dispatch=True)
    operator_gated(w, A.CONFIRM_ACCEPTANCE)
    assert w.lead_row().stage is not LeadStage.CLOSED  # an acceptance signal never closes the lead
    revision_command(w, MarkProposalAccepted, "cmd-accept")                          # 16. operator confirms
    operator_gated(w, A.MARK_WON)
    assert w.lead_row().stage is not LeadStage.CLOSED  # never auto-WON
    mark_won(w)                                                                       # 17. operator marks WON
    closed = step(w, O.NONE, A.NO_ACTION)                                             # 18. automation closed
    assert closed.reasons == ("LEAD_CLOSED:WON",) and w.lead_row().close_reason is CloseReason.WON
    assert w.app.execution_pass(dispatch_approved=True).attempted == 0
    assert len(w.transport.calls) == 3  # first touch, two replies: nothing else was ever sent
    metrics = w.app.execution_metrics()
    assert (metrics.open_leads, metrics.closed_leads) == (0, 1)
    w.app.stop()


def test_b_lost_path_needs_the_operator_and_cleans_up(db_path: Path) -> None:
    w = world(db_path)
    engaged_and_qualified(w)
    presented_proposal(w)
    w.commercial.default = asks(decline=True)
    customer_replies(w, "p-decline", body="We went with another vendor. What did the Basic plan cost per month?")
    approve_pending(w)
    automatic(w, O.CONVERSATION, A.SEND_APPROVED_MESSAGE, "ACCEPTED", dispatch=True)
    operator_gated(w, A.DECIDE_LOSS)
    assert w.lead_row().stage is not LeadStage.CLOSED  # no auto-LOST
    revision = current_revision(w)
    mark_lost(w)
    step(w, O.NONE, A.NO_ACTION)
    lead = w.lead_row()
    assert (lead.stage, lead.close_reason) == (LeadStage.CLOSED, CloseReason.LOST)
    with w.db.transaction() as uow:
        conversations = uow.conversations.list_by_lead(w.lead)
        member = uow.campaign_members.get(w.member_id or "")
        opportunities = uow.opportunities.list_by_lead(w.lead)
        revisions = uow.proposal_revisions.list_for_opportunity(revision.opportunity_id)
        signals = uow.commercial_signals.list_for_opportunity(revision.opportunity_id)
        jobs = [j for c in conversations for j in uow.follow_up_jobs.list_for_conversation(c.conversation_id)]
        dnc = uow.dnc.list_for_value(DNCScope.EMAIL, PROSPECT)
    assert all(c.status is ConversationStatus.CLOSED for c in conversations)
    assert member is not None and member.status is CampaignMemberStatus.REPLIED  # campaign history unchanged, inactive
    assert [o.status for o in opportunities] == [OpportunityStatus.LOST]
    assert [r.revision_id for r in revisions] == [revision.revision_id]  # history preserved
    assert revisions[0].status is RevisionStatus.CLOSED and revisions[0].approved_at is not None  # content kept
    assert all(s.status is not SignalStatus.OPEN for s in signals)
    assert jobs and all(j.status is not FollowUpJobStatus.SCHEDULED and j.status is not FollowUpJobStatus.CLAIMED for j in jobs)
    assert dnc == []  # losing a deal never adds do-not-contact
    w.app.stop()


def test_c_dnc_interrupts_every_lower_priority_owner(db_path: Path) -> None:
    w = world(db_path)
    engaged_and_qualified(w)
    presented_proposal(w)
    stale = w.plan()
    suppress(w)
    plan = step(w, O.NONE, A.NO_AUTOMATION)
    assert plan.blockers[0].value == "DNC" and not plan.executable
    assert w.app.execution_once(w.lead, stale.fingerprint).outcome is X.STALE_PLAN
    assert w.app.execution_once(w.lead, plan.fingerprint, dispatch_approved=True).outcome is X.NO_ACTION
    assert w.app.execution_pass(dispatch_approved=True).attempted == 0
    assert [p.lead_id for p in w.app.execution_queue(ExecutionQueue.BLOCKED)] == [w.lead]
    assert w.app.execution_queue(ExecutionQueue.ACTIONABLE) == ()
    w.app.stop()


def test_d_unknown_dispatch_is_reconciled_never_resent(db_path: Path) -> None:
    w = world(db_path, transport=FakeEmailTransport().script(FakeBehavior.ACCEPT_THEN_LOSE_RESPONSE))
    enrolled(w)
    automatic(w, O.CAMPAIGN, A.PREPARE_CAMPAIGN_TOUCH, "DRAFT_CREATED")
    approve_pending(w)
    automatic(w, O.CAMPAIGN, A.SEND_APPROVED_MESSAGE, "UNKNOWN", dispatch=True)
    assert w.lead_row().stage is LeadStage.NEW  # UNKNOWN never means sent
    recovery = step(w, O.DISPATCH_RECOVERY, A.RECONCILE_DISPATCH)
    assert [p.lead_id for p in w.app.execution_queue(ExecutionQueue.RECOVERY)] == [w.lead]
    calls = len(w.transport.calls)
    automatic(w, O.DISPATCH_RECOVERY, A.RECONCILE_DISPATCH, "ACCEPTED")
    assert len(w.transport.calls) == calls  # reconciliation never submits
    after = step(w, O.CUSTOMER, A.WAIT_FOR_CUSTOMER)  # the next legitimate owner
    assert after.fingerprint != recovery.fingerprint and w.lead_row().stage is LeadStage.CONTACTED
    assert len(campaign_messages(w.db, w.lead)) == 1
    w.app.stop()


def test_e_operator_review_interrupts_automation(db_path: Path) -> None:
    llm = ScriptedTransport().script(LLMTask.INTENT_CLASSIFICATION, classification(LeadIntent.NEGOTIATION))
    w = world(db_path, llm_transport=llm)
    enrolled(w)
    automatic(w, O.CAMPAIGN, A.PREPARE_CAMPAIGN_TOUCH, "DRAFT_CREATED")
    approve_pending(w)
    automatic(w, O.CAMPAIGN, A.SEND_APPROVED_MESSAGE, "ACCEPTED", dispatch=True)
    result = customer_replies(w, "p-nego", body="Can you do 30% off if we sign today?")
    assert result.escalation_id is not None
    plan = step(w, O.OPERATOR, A.ESCALATION_REVIEW)
    assert plan.refs.escalation_ids == (result.escalation_id,)
    assert w.execute(dispatch=True).outcome is X.REQUIRES_OPERATOR
    assert w.app.execution_pass(dispatch_approved=True).attempted == 0  # nothing lower-priority proceeds around it
    assert len(w.transport.calls) == 1 and len(campaign_messages(w.db, w.lead)) == 1
    w.app.stop()


def test_f_crash_and_replay(db_path: Path) -> None:
    w = world(db_path)
    enrolled(w)
    before = w.plan()
    w.app.stop()  # the process "crashes" after planning: nothing was written
    restarted = runtime(db_path, clock=w.clock, adapters=w.app._adapters)  # noqa: SLF001 - same adapters
    restarted.start()
    replanned = restarted.execution_plan(w.lead)
    assert replanned.fingerprint == before.fingerprint
    done = restarted.execution_once(w.lead, before.fingerprint)
    assert done.outcome is X.EXECUTED
    replay = restarted.execution_once(w.lead, before.fingerprint)  # the same old plan once more
    assert replay.outcome is X.STALE_PLAN
    from tests.runtime.builders import app_db
    assert len(campaign_messages(app_db(restarted), w.lead)) == 1  # no duplicate side effect
    restarted.stop()
