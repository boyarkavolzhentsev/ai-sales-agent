"""Campaign membership transitions, and the hooks through which Stages 6, 7 and 8 keep it
in step. Every function runs inside the caller's transaction (atomic with the cause).

Membership flow: ENROLLED -> DRAFTED (touch drafted) -> APPROVED (operator) ->
DISPATCHING (Stage 8 claim) -> WAITING (provider accepted) -> DRAFTED (next touch) ...
Terminal: REPLIED (the contact wrote: control passes to the Stage 6/9 conversation
workflow), CONVERTED, COMPLETED, SKIPPED, SUPPRESSED, FAILED, CANCELLED. Stopping a
membership cancels its open job, its undispatched drafts (releasing quota) and its
campaign FollowUpPlan, in the same transaction. Accepted sends are never rewritten.
"""

from datetime import datetime

from app.core.decisions import is_allowed_lead_transition
from app.core.enums import (
    CampaignJobStatus,
    CampaignMemberStatus,
    CampaignStatus,
    CloseReason,
    FollowUpCancelReason,
    FollowUpStatus,
    LeadStage,
    LeadStatus,
    OutboundKind,
    RefKind,
)
from app.core.models import (
    TERMINAL_CONVERSATION_STATUSES,
    TERMINAL_MEMBER_STATUSES,
    CampaignJob,
    CampaignMember,
    EntityRef,
    FollowUpPlan,
    Lead,
    OutboundMessage,
)
from app.campaign.audit import append_event, event_id_for, ref
from app.campaign.ids import CAMPAIGN_KEY_PREFIX, plan_id_for, stable_id
from app.conversation.cancellation import cancel_undispatched
from app.persistence import UnitOfWork
from app.policy.reply import suppression_entries
from app.policy.suppression import evaluate_suppression

M = CampaignMemberStatus
IN_SEQUENCE = frozenset({M.ENROLLED, M.DRAFTED, M.APPROVED, M.DISPATCHING, M.WAITING})


def is_campaign_message(message: OutboundMessage) -> bool:
    return message.idempotency_key.startswith(CAMPAIGN_KEY_PREFIX)


def job_for(uow: UnitOfWork, message: OutboundMessage) -> CampaignJob | None:
    if not is_campaign_message(message):
        return None
    return uow.campaign_jobs.get(message.idempotency_key[len(CAMPAIGN_KEY_PREFIX):])


def member_for(uow: UnitOfWork, message: OutboundMessage) -> CampaignMember | None:
    job = job_for(uow, message)
    return uow.campaign_members.get(job.member_id) if job is not None else None


def save_member(uow: UnitOfWork, member: CampaignMember, *, correlation_id: str, now: datetime, **changes: object) -> CampaignMember:
    status = changes.get("status", member.status)
    if status in TERMINAL_MEMBER_STATUSES:
        changes["next_action_at"] = None
    updated = CampaignMember.model_validate(
        member.model_dump() | changes | {"updated_at": max(now, member.updated_at), "version": member.version + 1}
    )
    uow.campaign_members.update(updated, member.version)
    if updated.status is not member.status:
        append_event(uow, key=(member.member_id, str(updated.version)), event_type="CAMPAIGN_MEMBER_STATUS_CHANGED",
                     subjects=_subjects(updated), correlation_id=correlation_id, now=now,
                     after={"from": member.status.value, "to": updated.status.value, "reason": updated.terminal_reason})
    return updated


def end_open_job(uow: UnitOfWork, member: CampaignMember, status: CampaignJobStatus, reason: str, *, correlation_id: str, now: datetime) -> CampaignJob | None:
    job = uow.campaign_jobs.get_open_for_member(member.member_id)
    if job is None:
        return None
    ended = CampaignJob.model_validate(
        job.model_dump()
        | {"status": status, "reason": reason, "claim_token": None, "claimed_by": None, "lease_expires_at": None,
           "updated_at": max(now, job.updated_at), "version": job.version + 1}
    )
    uow.campaign_jobs.update(ended, job.version)
    append_event(uow, key=(job.job_id, str(ended.version)), event_type=f"CAMPAIGN_JOB_{status.value}",
                 subjects=(ref(RefKind.CAMPAIGN_JOB, job.job_id), ref(RefKind.CAMPAIGN_MEMBER, member.member_id)),
                 after={"reason": reason, "previous_status": job.status.value}, correlation_id=correlation_id, now=now)
    return ended


def stop_member(
    uow: UnitOfWork, member: CampaignMember, status: CampaignMemberStatus, reason: str, *,
    plan_reason: FollowUpCancelReason, job_status: CampaignJobStatus = CampaignJobStatus.CANCELLED,
    correlation_id: str, now: datetime,
) -> CampaignMember:
    """End campaign automation for this membership (terminal ``status``)."""
    end_open_job(uow, member, job_status, reason, correlation_id=correlation_id, now=now)
    if member.lead_id is not None:
        drafts = [m for m in uow.outbound.list_by_lead(member.lead_id) if is_campaign_message(m)]
        cancelled, released = cancel_undispatched(uow, drafts, now)
        if cancelled:
            append_event(uow, key=(member.member_id, *(m.outbound_id for m in cancelled)), event_type="CAMPAIGN_DRAFTS_CANCELLED",
                         subjects=(ref(RefKind.CAMPAIGN_MEMBER, member.member_id),
                                   *(ref(RefKind.OUTBOUND_MESSAGE, m.outbound_id) for m in cancelled)),
                         after={"reason": reason, "outbound_ids": [m.outbound_id for m in cancelled], "released_reservation_ids": released},
                         correlation_id=correlation_id, now=now)
        _cancel_plan(uow, member, plan_reason, now)
    current = uow.campaign_members.get(member.member_id) or member
    if current.status is status or (current.status is M.SUPPRESSED):  # suppression is never downgraded
        return current
    return save_member(uow, current, status=status, terminal_reason=reason, correlation_id=correlation_id, now=now)


# ---- Hooks ----------------------------------------------------------------------------------


def record_contact_reply(uow: UnitOfWork, contact_id: str, *, correlation_id: str, now: datetime) -> list[CampaignMember]:
    """Stage 6 observation: the contact wrote. Campaign automation stops for every one of
    the contact's memberships in the same transaction; the conversation workflow owns the
    contact from now on."""
    stopped = []
    for member in uow.campaign_members.list_by_contact(contact_id):
        if member.status in IN_SEQUENCE:
            stopped.append(stop_member(uow, member, M.REPLIED, "CUSTOMER_REPLIED", plan_reason=FollowUpCancelReason.REPLY_RECEIVED,
                                       job_status=CampaignJobStatus.SUPERSEDED, correlation_id=correlation_id, now=now))
    return stopped


def record_inbound_outcome(uow: UnitOfWork, *, contact_id: str | None, lead: Lead | None, dnc_added: bool, correlation_id: str, now: datetime) -> None:
    """Stage 6 finalization: suppression wins over everything; a won lead converts."""
    if dnc_added and contact_id is not None:
        record_suppressed(uow, contact_id, correlation_id=correlation_id, now=now)
    if lead is not None and lead.stage is LeadStage.CLOSED and lead.close_reason is CloseReason.WON:
        member = uow.campaign_members.get_by_lead(lead.lead_id)
        if member is not None and member.status not in (M.SUPPRESSED, M.CONVERTED):
            stop_member(uow, member, M.CONVERTED, "LEAD_WON", plan_reason=FollowUpCancelReason.LEAD_CLOSED,
                        correlation_id=correlation_id, now=now)


def record_lead_closed(uow: UnitOfWork, lead: Lead, *, correlation_id: str, now: datetime) -> None:
    """An operator closed the lead (Stage 12 pipeline decision). WON converts the
    membership exactly as a Stage 6 outcome does; any other close ends a membership that
    is still in sequence (its job, undispatched campaign drafts and plan are stopped). A
    membership that already ended keeps its terminal status."""
    if lead.close_reason is CloseReason.WON:
        record_inbound_outcome(uow, contact_id=None, lead=lead, dnc_added=False, correlation_id=correlation_id, now=now)
        return
    member = uow.campaign_members.get_by_lead(lead.lead_id)
    if member is not None and member.status in IN_SEQUENCE:
        stop_member(uow, member, M.CANCELLED, "LEAD_CLOSED", plan_reason=FollowUpCancelReason.LEAD_CLOSED,
                    correlation_id=correlation_id, now=now)


def record_suppressed(uow: UnitOfWork, contact_id: str, *, correlation_id: str, now: datetime) -> None:
    for member in uow.campaign_members.list_by_contact(contact_id):
        if member.status is not M.SUPPRESSED:
            stop_member(uow, member, M.SUPPRESSED, "DO_NOT_CONTACT", plan_reason=FollowUpCancelReason.SUPPRESSED,
                        correlation_id=correlation_id, now=now)


def record_lead_owned(uow: UnitOfWork, lead_id: str, *, correlation_id: str, now: datetime) -> None:
    member = uow.campaign_members.get_by_lead(lead_id)
    if member is not None and member.status in IN_SEQUENCE:
        stop_member(uow, member, M.CANCELLED, "OPERATOR_TOOK_OVER", plan_reason=FollowUpCancelReason.OPERATOR_TOOK_OVER,
                    correlation_id=correlation_id, now=now)


def record_draft_approved(uow: UnitOfWork, message: OutboundMessage, *, correlation_id: str, now: datetime) -> None:
    member = member_for(uow, message)
    if member is not None and member.status is M.DRAFTED:
        save_member(uow, member, status=M.APPROVED, correlation_id=correlation_id, now=now)


def record_draft_rejected(uow: UnitOfWork, message: OutboundMessage, *, correlation_id: str, now: datetime) -> None:
    """Deterministic outcome of an operator rejection: the membership ends (CANCELLED).
    The rejected touch is never regenerated (its job is COMPLETED)."""
    member = member_for(uow, message)
    if member is not None and member.status in IN_SEQUENCE:
        stop_member(uow, member, M.CANCELLED, "OPERATOR_REJECTED", plan_reason=FollowUpCancelReason.OPERATOR,
                    correlation_id=correlation_id, now=now)


def record_dispatch_claimed(uow: UnitOfWork, message: OutboundMessage, *, correlation_id: str, now: datetime) -> None:
    member = member_for(uow, message)
    if member is not None and member.status is M.APPROVED:
        save_member(uow, member, status=M.DISPATCHING, correlation_id=correlation_id, now=now)


def record_dispatch_not_accepted(uow: UnitOfWork, message: OutboundMessage, *, retry_possible: bool, reason: str,
                                 correlation_id: str, now: datetime) -> None:
    member = member_for(uow, message)
    if member is None or member.status is not M.DISPATCHING:
        return
    if retry_possible:
        save_member(uow, member, status=M.APPROVED, correlation_id=correlation_id, now=now)
    else:
        stop_member(uow, member, M.FAILED, f"NOT_ACCEPTED:{reason}", plan_reason=FollowUpCancelReason.OPERATOR,
                    correlation_id=correlation_id, now=now)


def record_touch_accepted(uow: UnitOfWork, message: OutboundMessage, *, correlation_id: str, now: datetime) -> None:
    """Stage 8 established that the provider ACCEPTED this campaign touch (at dispatch, by
    reconciliation, or as late-acceptance evidence). Provider truth is authoritative:

    - The accepted send is recorded exactly once per message (guarded by its durable
      CAMPAIGN_TOUCH_ACCEPTED record), so replayed or repeated evidence, even arriving after
      a newer touch, never counts a touch twice or reopens anything.
    - A membership still in sequence becomes WAITING.
    - A membership that ended FAILED because this very touch was reported not accepted is
      reconciled: WAITING if it may still continue, otherwise the stopped state that now
      applies (SUPPRESSED, CONVERTED, REPLIED or CANCELLED). History is not rewritten: the
      failure, the correction and their audit records all remain.
    - Any other terminal status (REPLIED, SUPPRESSED, CONVERTED, COMPLETED, SKIPPED,
      CANCELLED) outranks the send: it is recorded but automation is not resurrected.
    - WAITING (and only WAITING) starts or advances the campaign FollowUpPlan and marks the
      lead CONTACTED after a first touch.
    """
    member = member_for(uow, message)
    job = job_for(uow, message)
    if member is None or job is None:
        return
    if uow.audit.get(event_id_for("CAMPAIGN_TOUCH_ACCEPTED", member.member_id, message.outbound_id)) is not None:
        return  # this message's acceptance is already recorded (durable, per message)
    previous = member.status
    changes: dict[str, object] = {
        "touch_count": member.touch_count + 1, "latest_outbound_id": message.outbound_id, "last_activity_at": now,
    }
    reconciled = False
    if member.status in IN_SEQUENCE:
        changes["status"] = M.WAITING
    elif member.status is M.FAILED and (member.terminal_reason or "").startswith("NOT_ACCEPTED"):
        status, reason = _after_late_acceptance(uow, member, now)
        changes |= {"status": status, "terminal_reason": reason}
        reconciled = True
    member = save_member(uow, member, correlation_id=correlation_id, now=now, **changes)
    append_event(uow, key=(member.member_id, message.outbound_id), event_type="CAMPAIGN_TOUCH_ACCEPTED",
                 subjects=(*_subjects(member), ref(RefKind.OUTBOUND_MESSAGE, message.outbound_id)),
                 after={"touch_no": job.touch_no, "previous_status": previous.value, "status": member.status.value,
                        "reconciled_from_failed": reconciled, "touch_count": member.touch_count},
                 correlation_id=correlation_id, now=now)
    if member.status is not M.WAITING or member.lead_id is None:
        return
    campaign = uow.campaigns.get(member.campaign_id)
    lead = uow.leads.get(member.lead_id)
    if message.kind is OutboundKind.FIRST_TOUCH and lead is not None:
        _mark_contacted(uow, lead, correlation_id=correlation_id, now=now)
    if campaign is None or campaign.max_follow_ups == 0:
        return
    plan = uow.follow_ups.get_open_for_lead(member.lead_id)
    if plan is not None and plan.campaign_id == campaign.campaign_id and plan.status is FollowUpStatus.ACTIVE:
        if message.kind is OutboundKind.FOLLOW_UP:
            uow.follow_ups.update(FollowUpPlan.model_validate(
                plan.model_dump()
                | {"steps_sent": min(plan.steps_sent + 1, plan.max_steps), "next_due_at": now + campaign.min_interval_between_follow_ups,
                   "updated_at": max(now, plan.updated_at), "version": plan.version + 1}
            ), plan.version)
        return
    if plan is None:
        # First touch, or a touch reconciled after its plan was cancelled with the failure:
        # (re)start the sequence after the touches actually sent.
        sent_follow_ups = min(job.touch_no - 1, campaign.max_follow_ups)
        plan_id = plan_id_for(member.member_id)
        if uow.follow_ups.get(plan_id) is not None:
            plan_id = stable_id("fp", "campaign", member.member_id, message.outbound_id)
        uow.follow_ups.add(FollowUpPlan(
            plan_id=plan_id, lead_id=member.lead_id, campaign_id=campaign.campaign_id, anchor_outbound_id=message.outbound_id,
            max_steps=campaign.max_follow_ups, steps_sent=sent_follow_ups,
            next_due_at=now + campaign.min_interval_between_follow_ups, created_at=now, updated_at=now,
        ))


def _after_late_acceptance(uow: UnitOfWork, member: CampaignMember, now: datetime) -> tuple[M, str | None]:
    """Where a membership that FAILED on this touch stands once the touch proved accepted.
    Stopping conditions that arose meanwhile win; only a still-eligible one resumes."""
    contact = uow.contacts.get(member.contact_id)
    company = uow.companies.get(contact.company_id) if contact is not None and contact.company_id else None
    if contact is not None and evaluate_suppression(contact.email, company.domain if company else None,
                                                    suppression_entries(uow, contact.email, company.domain if company else None),
                                                    now) is not None:
        return M.SUPPRESSED, "DO_NOT_CONTACT"
    lead = uow.leads.get(member.lead_id or "")
    if lead is None or lead.stage is LeadStage.CLOSED:
        if lead is not None and lead.close_reason is CloseReason.WON:
            return M.CONVERTED, "LEAD_WON"
        return M.CANCELLED, "LEAD_CLOSED"
    if lead.status is LeadStatus.OPERATOR_OWNED:
        return M.CANCELLED, "OPERATOR_TOOK_OVER"
    if any(c.status not in TERMINAL_CONVERSATION_STATUSES for c in uow.conversations.list_by_contact(member.contact_id)):
        return M.REPLIED, "CUSTOMER_REPLIED"
    campaign = uow.campaigns.get(member.campaign_id)
    if campaign is None or campaign.status is CampaignStatus.ENDED:
        return M.CANCELLED, "CAMPAIGN_ENDED"
    return M.WAITING, None


def _mark_contacted(uow: UnitOfWork, lead: Lead, *, correlation_id: str, now: datetime) -> None:
    if lead.status is not LeadStatus.AUTOMATED or not is_allowed_lead_transition(lead.stage, LeadStage.CONTACTED):
        return
    uow.leads.update(Lead.model_validate(
        lead.model_dump() | {"stage": LeadStage.CONTACTED, "updated_at": max(now, lead.updated_at), "version": lead.version + 1}
    ), lead.version)
    append_event(uow, key=(lead.lead_id, "CONTACTED"), event_type="LEAD_STAGE_CHANGED", subjects=(ref(RefKind.LEAD, lead.lead_id),),
                 after={"from": lead.stage.value, "to": LeadStage.CONTACTED.value}, correlation_id=correlation_id, now=now)


def _cancel_plan(uow: UnitOfWork, member: CampaignMember, reason: FollowUpCancelReason, now: datetime) -> None:
    if member.lead_id is None:
        return
    plan = uow.follow_ups.get_open_for_lead(member.lead_id)
    if plan is None or plan.campaign_id != member.campaign_id:
        return
    uow.follow_ups.update(FollowUpPlan.model_validate(
        plan.model_dump()
        | {"status": FollowUpStatus.CANCELLED, "cancel_reason": reason, "next_due_at": None,
           "updated_at": max(now, plan.updated_at), "version": plan.version + 1}
    ), plan.version)


def _subjects(member: CampaignMember) -> tuple[EntityRef, ...]:
    refs = [ref(RefKind.CAMPAIGN_MEMBER, member.member_id), ref(RefKind.CAMPAIGN, member.campaign_id),
            ref(RefKind.PROSPECT_CONTACT, member.contact_id)]
    if member.lead_id is not None:
        refs.append(ref(RefKind.LEAD, member.lead_id))
    return tuple(refs)
