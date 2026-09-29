"""Execution of one claimed campaign touch: revalidate, then produce its draft.

One IMMEDIATE transaction: claim-token check, revalidation of the campaign, membership,
contact, lead and Stage 3 policy at the injected ``now``, composition from persisted
facts only, the membership thread (first touch), the draft and the job completion all
commit together or not at all. The draft is a FIRST_TOUCH or FOLLOW_UP outbound message
whose idempotency key is ``campaign:<job_id>`` (unique in SQL): one logical touch, at most
one message. It is DRAFTED: V1 sends nothing without Stage 7 approval and Stage 8.
"""

from datetime import datetime

from app.core.enums import (
    CampaignJobStatus,
    CampaignMemberStatus,
    DraftPurpose,
    FollowUpCancelReason,
    FollowUpStatus,
    OutboundKind,
    OutboundStatus,
    RefKind,
)
from app.core.models import Campaign, CampaignJob, CampaignMember, EmailThread, OutboundMessage, ProspectContact
from app.core.models.types import JsonObject
from app.campaign.audit import append_event, ref
from app.campaign.composer import Composition, compose_first_touch, compose_follow_up
from app.campaign.ids import campaign_key, job_id_for, stable_id, thread_id_for
from app.campaign.models import CampaignClaim, CampaignExecutionConfig, ExecutionOutcome, ExecutionResult
from app.campaign.policy import (
    BLOCKING,
    CANCELLING,
    SUPERSEDING,
    CampaignBlock,
    ContactFacts,
    campaign_blockers,
    contact_blockers,
    lead_blockers,
    load_contact_facts,
    terminal_status,
)
from app.campaign.state import save_member, stop_member
from app.llm.claim_check import draft_hash
from app.persistence import Clock, Database, NotFoundError, UnitOfWork
from app.policy import PolicyDecisionResult, evaluate_follow_up_policy
from app.policy.reply import evaluate_send_policy, quota_snapshot, suppression_entries

M = CampaignMemberStatus
DRAFT_CREATED = "DRAFT_CREATED"  # the event Stage 7/8 read a draft's context from


class CampaignExecutor:
    def __init__(self, db: Database, clock: Clock, config: CampaignExecutionConfig) -> None:
        self._db = db
        self._clock = clock
        self._config = config

    def execute(self, claim: CampaignClaim, *, correlation_id: str) -> ExecutionResult:
        now = self._clock.now()
        with self._db.transaction() as uow:
            job = uow.campaign_jobs.get(claim.job_id)
            if job is None:
                raise NotFoundError(f"campaign job {claim.job_id} does not exist")
            if job.status is CampaignJobStatus.COMPLETED:
                return _result(job, ExecutionOutcome.REPLAYED)
            if job.status is not CampaignJobStatus.CLAIMED or job.claim_token != claim.claim_token:
                return _result(job, ExecutionOutcome.STALE_CLAIM)
            member = uow.campaign_members.get(job.member_id)
            campaign = uow.campaigns.get(job.campaign_id)
            if member is None or campaign is None:
                raise NotFoundError(f"campaign job {job.job_id} lost its membership or campaign")

            facts = load_contact_facts(uow, member.contact_id)
            codes = self._blockers(uow, job, member, campaign, facts, now)
            if codes or facts.contact is None:
                return self._dispose(uow, job, member, codes or [CampaignBlock.CONTACT_MISSING], correlation_id, now)
            composition = self._compose(uow, job, member, campaign, facts.contact, facts, now)
            if not composition.claim_check.passed:
                return self._dispose(uow, job, member, [CampaignBlock.CLAIM_CHECK_FAILED], correlation_id, now)
            outbound = self._draft(uow, job, member, campaign, facts.contact, composition, now)
            append_event(uow, key=(job.job_id,), event_type=DRAFT_CREATED,
                         subjects=(ref(RefKind.OUTBOUND_MESSAGE, outbound.outbound_id), ref(RefKind.MESSAGE_DRAFT, outbound.draft_id),
                                   ref(RefKind.EMAIL_THREAD, outbound.thread_id or ""), ref(RefKind.LEAD, outbound.lead_id),
                                   ref(RefKind.CAMPAIGN, campaign.campaign_id), ref(RefKind.CAMPAIGN_MEMBER, member.member_id),
                                   ref(RefKind.CAMPAIGN_JOB, job.job_id)),
                         after=_provenance(job, member, outbound, composition), correlation_id=correlation_id, now=now)
            completed = _update(uow, job, now, status=CampaignJobStatus.COMPLETED, outbound_id=outbound.outbound_id)
            current = uow.campaign_members.get(member.member_id) or member
            updated = save_member(uow, current, status=M.DRAFTED, next_action_at=None, thread_id=outbound.thread_id,
                                  correlation_id=correlation_id, now=now)
            append_event(uow, key=(job.job_id, str(completed.version)), event_type="CAMPAIGN_JOB_COMPLETED",
                         subjects=(ref(RefKind.CAMPAIGN_JOB, job.job_id), ref(RefKind.OUTBOUND_MESSAGE, outbound.outbound_id)),
                         after={"outbound_id": outbound.outbound_id, "claim_no": job.claim_count}, correlation_id=correlation_id, now=now)
            return _result(completed, ExecutionOutcome.DRAFT_CREATED, member_status=updated.status)

    # ---- Revalidation ------------------------------------------------------------------------

    def _blockers(self, uow: UnitOfWork, job: CampaignJob, member: CampaignMember, campaign: Campaign,
                  facts: ContactFacts, now: datetime) -> list[str]:
        codes: list[str] = list(campaign_blockers(campaign.status))
        if member.version != job.basis_member_version:
            codes.append(CampaignBlock.MEMBER_CHANGED)
        if member.status is not (M.ENROLLED if job.touch_no == 1 else M.WAITING):
            codes.append(CampaignBlock.MEMBER_NOT_READY)
        if job.touch_no != member.touch_count + 1:
            codes.append(CampaignBlock.STALE_TOUCH)
        if job.due_at > now:
            codes.append(CampaignBlock.NOT_DUE)
        codes += contact_blockers(facts, now)
        codes += lead_blockers(uow, member)
        if codes or facts.contact is None:
            return list(dict.fromkeys(codes))
        return list(dict.fromkeys(codes + [r.value for r in self._policy(uow, job, member, campaign, facts, now).reasons]))

    def _policy(self, uow: UnitOfWork, job: CampaignJob, member: CampaignMember, campaign: Campaign,
                facts: ContactFacts, now: datetime) -> PolicyDecisionResult:
        contact = facts.contact
        if contact is None:
            return PolicyDecisionResult.from_checks(())
        domain = facts.company.domain if facts.company else None
        common = {"limits": self._config.limits, "window": self._config.window, "kill_switch": self._config.kill_switch}
        if job.touch_no == 1:
            return evaluate_send_policy(uow, kind=OutboundKind.FIRST_TOUCH, campaign=campaign, contact=contact, company_domain=domain,
                                        mailbox=campaign.sending_mailbox, now=now, **common)
        lead = uow.leads.get(member.lead_id or "")
        plan = uow.follow_ups.get_open_for_lead(member.lead_id or "")
        last = uow.outbound.get(member.latest_outbound_id or "")
        if lead is None or plan is None or plan.campaign_id != campaign.campaign_id or last is None or last.sent_at is None:
            return PolicyDecisionResult.from_checks(())  # handled by the explicit checks below
        return evaluate_follow_up_policy(
            plan=plan, campaign=campaign, lead=lead, contact=contact, last_outbound_at=last.sent_at, now=now,
            suppression_entries=tuple(suppression_entries(uow, contact.email, domain)), company_domain=domain,
            quota_snapshot=quota_snapshot(uow, contact=contact, mailbox=campaign.sending_mailbox, campaign=campaign,
                                          limits=self._config.limits, now=now),
            **common,
        )

    def _dispose(self, uow: UnitOfWork, job: CampaignJob, member: CampaignMember, codes: list[str],
                 correlation_id: str, now: datetime) -> ExecutionResult:
        codes = list(dict.fromkeys(codes))
        if job.touch_no > 1 and not self._plan_active(uow, member):
            codes.append(CampaignBlock.NO_FOLLOW_UP_PLAN)
        terminal = terminal_status(codes)
        if terminal is not None and not SUPERSEDING.intersection(codes):
            status, reason = terminal
            ended = _update(uow, job, now, status=CampaignJobStatus.BLOCKED, block_codes=tuple(codes), reason=reason)
            stopped = stop_member(uow, member, status, reason, plan_reason=_plan_reason(status), correlation_id=correlation_id, now=now)
            self._event(uow, ended, "BLOCKED", codes, correlation_id, now)
            return _result(ended, ExecutionOutcome.BLOCKED, codes, member_status=stopped.status)
        if SUPERSEDING.intersection(codes):
            ended = _update(uow, job, now, status=CampaignJobStatus.SUPERSEDED, reason=next(c for c in codes if c in SUPERSEDING))
            self._event(uow, ended, "SUPERSEDED", codes, correlation_id, now)
            return _result(ended, ExecutionOutcome.SUPERSEDED, codes)
        if CANCELLING.intersection(codes):
            ended = _update(uow, job, now, status=CampaignJobStatus.CANCELLED, reason=next(c for c in codes if c in CANCELLING))
            self._event(uow, ended, "CANCELLED", codes, correlation_id, now)
            return _result(ended, ExecutionOutcome.CANCELLED, codes)
        if BLOCKING.intersection(codes):
            ended = _update(uow, job, now, status=CampaignJobStatus.BLOCKED, block_codes=tuple(codes), reason=codes[0])
            save_member(uow, member, next_action_at=None, correlation_id=correlation_id, now=now)
            self._event(uow, ended, "BLOCKED", codes, correlation_id, now)
            return _result(ended, ExecutionOutcome.BLOCKED, codes)
        due_at = max(now + self._config.defer_delay, job.due_at)
        member = save_member(uow, member, next_action_at=due_at, correlation_id=correlation_id, now=now)
        deferred = _update(uow, job, now, status=CampaignJobStatus.SCHEDULED, due_at=due_at, basis_member_version=member.version)
        self._event(uow, deferred, "DEFERRED", codes, correlation_id, now)
        return _result(deferred, ExecutionOutcome.DEFERRED, codes)

    @staticmethod
    def _plan_active(uow: UnitOfWork, member: CampaignMember) -> bool:
        plan = uow.follow_ups.get_open_for_lead(member.lead_id or "")
        return plan is not None and plan.campaign_id == member.campaign_id and plan.status is FollowUpStatus.ACTIVE

    @staticmethod
    def _event(uow: UnitOfWork, job: CampaignJob, what: str, codes: list[str], correlation_id: str, now: datetime) -> None:
        append_event(uow, key=(job.job_id, str(job.version)), event_type=f"CAMPAIGN_JOB_{what}",
                     subjects=(ref(RefKind.CAMPAIGN_JOB, job.job_id), ref(RefKind.CAMPAIGN_MEMBER, job.member_id)),
                     after={"reason_codes": codes, "due_at": job.due_at.isoformat()}, correlation_id=correlation_id, now=now)

    # ---- Draft -------------------------------------------------------------------------------

    def _compose(self, uow: UnitOfWork, job: CampaignJob, member: CampaignMember, campaign: Campaign,
                 contact: ProspectContact, facts: ContactFacts, now: datetime) -> Composition:
        if job.touch_no == 1:
            question = self._config.value_questions.get(campaign.campaign_id, self._config.value_question)
            return compose_first_touch(uow, campaign, contact, facts.company, self._config.sender, question, now)
        first = uow.campaign_jobs.get(job_id_for(member.member_id, 1))
        first_message = uow.outbound.get(first.outbound_id) if first is not None and first.outbound_id else None
        subject = first_message.subject if first_message is not None else f"A short introduction from {self._config.sender.company_name}"
        return compose_follow_up(contact, facts.company, self._config.sender, subject)

    def _draft(self, uow: UnitOfWork, job: CampaignJob, member: CampaignMember, campaign: Campaign,
               contact: ProspectContact, composition: Composition, now: datetime) -> OutboundMessage:
        key = campaign_key(job.job_id)
        existing = uow.outbound.get_by_idempotency_key(key)
        if existing is not None:
            return existing
        thread_id = member.thread_id or thread_id_for(member.member_id)
        if uow.threads.get(thread_id) is None:
            # The membership's single thread, registered before the first send so a reply to any
            # touch joins it and belongs to this lead.
            uow.threads.add(EmailThread(
                thread_id=thread_id, mailbox=campaign.sending_mailbox,
                participant_addresses=tuple(dict.fromkeys((campaign.sending_mailbox, contact.email))),
                subject_normalized=composition.subject.lower().removeprefix("re: ").strip(), lead_id=member.lead_id,
            ))
        first = job.touch_no == 1
        outbound = OutboundMessage(
            outbound_id=stable_id("ob", "campaign", job.job_id), idempotency_key=key,
            kind=OutboundKind.FIRST_TOUCH if first else OutboundKind.FOLLOW_UP, lead_id=member.lead_id or "",
            contact_id=member.contact_id, campaign_id=campaign.campaign_id, thread_id=thread_id,
            sequence_no=job.touch_no - 1, draft_id=stable_id("dr", "campaign", job.job_id), subject=composition.subject,
            body_final=composition.body, content_hash=draft_hash(composition.subject, composition.body),
            status=OutboundStatus.DRAFTED, created_at=now,
        )
        uow.outbound.add(outbound)
        return outbound


def _provenance(job: CampaignJob, member: CampaignMember, outbound: OutboundMessage, composition: Composition) -> JsonObject:
    """What the draft was built from (IDs, evidence locations, fields used); no body text."""
    purpose = DraftPurpose.OUTBOUND_FIRST_TOUCH if job.touch_no == 1 else DraftPurpose.OUTBOUND_FOLLOW_UP
    return {
        "purpose": purpose.value, "draft_id": outbound.draft_id, "outbound_id": outbound.outbound_id,
        "campaign_id": job.campaign_id, "member_id": member.member_id, "job_id": job.job_id, "touch_no": job.touch_no,
        "personalization_fields": list(composition.personalization),
        "evidence_ids_used": [e.evidence_id for e in composition.evidence],
        "evidence": [
            {"evidence_id": e.evidence_id, "chunk_id": e.chunk_id, "source_id": e.source_id, "source_version": e.source_version,
             "domain": e.domain.value, "score": e.score, "rank": e.rank}
            for e in composition.evidence
        ],
        "query": composition.query.model_dump(mode="json") if composition.query is not None else None,
        "claim_check": composition.claim_check.model_dump(mode="json"),
    }


def _plan_reason(status: CampaignMemberStatus) -> FollowUpCancelReason:
    return {
        M.SUPPRESSED: FollowUpCancelReason.SUPPRESSED,
        M.CANCELLED: FollowUpCancelReason.OPERATOR,
        M.COMPLETED: FollowUpCancelReason.CAMPAIGN_ENDED,
    }.get(status, FollowUpCancelReason.OPERATOR)


def _update(uow: UnitOfWork, job: CampaignJob, now: datetime, **changes: object) -> CampaignJob:
    updated = CampaignJob.model_validate(
        job.model_dump() | {"claim_token": None, "claimed_by": None, "lease_expires_at": None} | changes
        | {"updated_at": max(now, job.updated_at), "version": job.version + 1}
    )
    uow.campaign_jobs.update(updated, job.version)
    return updated


def _result(job: CampaignJob, outcome: ExecutionOutcome, codes: list[str] | tuple[str, ...] = (),
            member_status: CampaignMemberStatus | None = None) -> ExecutionResult:
    return ExecutionResult(job_id=job.job_id, outcome=outcome, job_status=job.status, member_status=member_status,
                           outbound_id=job.outbound_id, due_at=job.due_at, reason_codes=tuple(dict.fromkeys(codes)))
