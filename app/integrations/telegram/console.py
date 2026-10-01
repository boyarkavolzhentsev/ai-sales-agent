"""The Telegram operator console: turns verified updates into existing Stage 7 commands,
and Stage 14 operator-queue items into review cards. No business rule lives here.

Every update:
1. only private chats; a group or channel is never answered with business data;
2. the Telegram identity (user id == chat id, configured) becomes a Stage 7 credential and
   Stage 7 ``authorize`` must accept it; anyone else gets a generic "Not authorized.";
3. a button press (callback data is attacker-controlled) is parsed strictly, the target is
   re-read from authoritative state, and the version on the button must still be current
   (else "changed"): an old card never executes against new state;
4. the Stage 7 command is built with a deterministic ``command_id`` (from the update id,
   the action and the target), so a replayed update can never execute twice: Stage 7
   returns the recorded outcome, or refuses the reused id for a different payload, which
   here means "already handled";
5. only after the command committed is Telegram answered (best effort). A failed
   acknowledgement never retries or undoes the business command.
WON, LOST and DNC are never one click: they create a durable confirmation bound to the
operator, the chat, the target and the version seen, valid for a few minutes; only the
same operator confirming it in the same chat executes the command.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.commercial.terms import term_row_id
from app.core.enums import (
    ConversationStatus,
    EscalationResolution,
    LeadStatus,
    LostReason,
    ObjectionStatus,
    OutboundKind,
)
from app.inbound.models import stable_id
from app.integrations.telegram.auth import TelegramOperatorAuthenticator, credential_for
from app.integrations.telegram.callbacks import Action, Callback, decode, encode
from app.integrations.telegram.client import TelegramApi, Update
from app.integrations.telegram.errors import TelegramCode, TelegramError
from app.integrations.telegram.rendering import excerpt, fit, short
from app.operator import (
    ApproveDraft,
    ApproveProposal,
    ApproveQualification,
    ApproveTermRequest,
    CommandCollisionError,
    CommandRejectedError,
    CreateOpportunity,
    DismissCommercialSignal,
    MarkDoNotContact,
    MarkLeadLost,
    MarkLeadWon,
    MarkProposalAccepted,
    MarkProposalDeclined,
    MarkProposalPresented,
    OperatorNotFoundError,
    OperatorService,
    OperatorUnauthorizedError,
    RejectDraft,
    RejectReason,
    RejectTermRequest,
    ResolveEscalation,
    StaleCommandError,
    SuppressCampaignMember,
    TakeOwnership,
    UpdateObjection,
)
from app.operator.models import OperatorCredential
from app.orchestration import ExecutionAction, ExecutionOwner, ExecutionQueue, SalesExecutionPlan, SalesOrchestrator
from app.persistence import (
    Clock,
    ConfirmationStatus,
    Database,
    NotificationStatus,
    OperatorConfirmation,
    OperatorNotification,
)

PROVIDER = "telegram"
CONFIRMATION_TTL = timedelta(minutes=5)
NOTIFICATION_LEASE = timedelta(minutes=2)  # covers rendering a card; a submitted card is never re-leased
REJECT_REASONS = tuple(RejectReason)
LOST_REASONS = tuple(LostReason)
Buttons = tuple[tuple[tuple[str, str], ...], ...]
HELP = ("Commands:\n/queue - items that need you (first page)\n/status - provider status\n/help - this help\n\n"
        "Review cards arrive here; buttons act through the normal operator commands and are re-checked first.")
A = ExecutionAction
LABELS = {
    A.REVIEW_REPLY_DRAFT: "Reply draft to review", A.REVIEW_CAMPAIGN_DRAFT: "Campaign draft to review",
    A.ESCALATION_REVIEW: "Escalation to review", A.REVIEW_QUALIFICATION: "Qualification to review",
    A.CREATE_OPPORTUNITY: "Qualified: create the opportunity?", A.PREPARE_PROPOSAL: "Proposal to prepare",
    A.REVIEW_PROPOSAL: "Proposal to approve", A.REVISE_PROPOSAL: "Proposal needs a revision",
    A.PRESENT_PROPOSAL: "Approved proposal: presented?", A.REVIEW_TERM_REQUEST: "Customer term request",
    A.HANDLE_OBJECTION: "Customer objection", A.CONFIRM_ACCEPTANCE: "Customer acceptance signal",
    A.MARK_WON: "Accepted proposal: mark the lead WON?", A.DECIDE_LOSS: "Loss decision needed",
    A.WAIT_FOR_OPERATOR: "Waiting for an operator", A.RESPOND_TO_CUSTOMER: "Customer wrote; nothing drafted",
    A.QUALIFY_LEAD: "Qualification facts missing", A.RECONCILE_DISPATCH: "Dispatch recovery needs attention",
}


@dataclass(frozen=True)
class Reply:
    text: str
    ack: str
    buttons: Buttons = ()
    close_card: bool = False  # the card's buttons no longer apply


class TelegramConsole:
    def __init__(self, *, db: Database, clock: Clock, api: TelegramApi, operators: TelegramOperatorAuthenticator,
                 authorize: Callable[[OperatorCredential], str], operator_service: OperatorService,
                 orchestrator: SalesOrchestrator, status: Callable[[], str], page_size: int = 10,
                 worker_id: str = "local-worker", lease: timedelta = NOTIFICATION_LEASE) -> None:
        self._db = db
        self._clock = clock
        self._api = api
        self._operators = operators
        self._authorize = authorize
        self._ops = _Once(operator_service)
        self._orchestrator = orchestrator
        self._status = status
        self._page = page_size
        self._worker_id = worker_id
        self._lease = lease

    # ---- Updates --------------------------------------------------------------------------

    def handle(self, update: Update) -> str:
        """The outcome code (for reporting); never raises for a domain or Telegram outcome."""
        if update.kind == "other":
            return "IGNORED_UNSUPPORTED"
        if update.chat_type != "private":
            if update.kind == "callback_query" and update.callback_id:
                self._ack(update.callback_id, "Not permitted here.")
            return "IGNORED_NOT_PRIVATE"  # nothing is ever written into a group
        operator = self._operator(update)
        if operator is None:
            if update.kind == "callback_query" and update.callback_id:
                self._ack(update.callback_id, "Not authorized.")
            elif update.chat_id is not None:
                self._send(update.chat_id, "Not authorized.")
            return "UNAUTHORIZED"
        assert update.chat_id is not None and update.user_id is not None
        credential = credential_for(update.user_id, update.chat_id)
        if update.kind == "message":
            return self._command(update, credential)
        return self._callback(update, credential, operator)

    def failed(self, update: Update) -> None:
        """Best effort, after an update could not be handled: tell an authorized operator
        (private chat only) without any detail. Never raises."""
        try:
            if update.chat_type != "private" or self._operator(update) is None:
                return
            assert update.chat_id is not None
            if update.kind == "callback_query" and update.callback_id:
                self._ack(update.callback_id, "Could not complete")
            self._send(update.chat_id, "This could not be completed. Check /queue for the current state before trying again.")
        except Exception:  # noqa: BLE001 - UI only; the failure itself is already recorded
            pass

    def _operator(self, update: Update) -> str | None:
        operator = self._operators.operator_for(update.user_id, update.chat_id, update.chat_type)
        if operator is None or update.user_id is None or update.chat_id is None:
            return None
        try:
            return self._authorize(credential_for(update.user_id, update.chat_id))  # Stage 7 remains the authority
        except OperatorUnauthorizedError:
            return None

    def _command(self, update: Update, credential: OperatorCredential) -> str:
        assert update.chat_id is not None
        word = (update.text or "").strip().split(" ", 1)[0].split("@", 1)[0].lower()
        if word in ("/start", "/help"):
            self._send(update.chat_id, "You are authorized as an operator.\n\n" + HELP if word == "/start" else HELP)
        elif word == "/status":
            self._send(update.chat_id, fit(self._status()))
        elif word == "/queue":
            self._send(update.chat_id, self._queue_text())
        else:
            self._send(update.chat_id, "Unknown command. " + HELP)
        return "COMMAND"

    def _callback(self, update: Update, credential: OperatorCredential, operator: str) -> str:
        assert update.chat_id is not None and update.callback_id is not None
        callback = decode(update.callback_data)
        if callback is None:
            self._ack(update.callback_id, "Unknown action.")
            return "INVALID_CALLBACK"
        try:
            reply = self._act(update, callback, credential, operator)
            outcome = "ACTION"
        except StaleCommandError:
            reply, outcome = Reply("This item changed since the card was sent. A fresh card will follow if it still needs you.",
                                   "Changed meanwhile", close_card=True), "STALE"
        except CommandCollisionError:
            reply, outcome = Reply("Already handled.", "Already handled", close_card=True), "ALREADY_HANDLED"
        except CommandRejectedError as exc:
            codes = ", ".join(code.value for code in exc.codes)
            reply, outcome = Reply(f"Not allowed now ({codes}).", "Not allowed now"), "REJECTED"
        except OperatorNotFoundError:
            reply, outcome = Reply("Not found.", "Not found", close_card=True), "NOT_FOUND"
        except OperatorUnauthorizedError:
            reply, outcome = Reply("Not authorized.", "Not authorized"), "UNAUTHORIZED"
        # Business work (if any) is committed: only now is Telegram told. Failures here are UI only.
        self._ack(update.callback_id, reply.ack)
        if reply.close_card and update.message_id is not None:
            self._close(update.chat_id, update.message_id, reply.ack)
        self._send(update.chat_id, reply.text, reply.buttons)
        return outcome

    # ---- Actions ------------------------------------------------------------------------------------

    def _act(self, update: Update, cb: Callback, credential: OperatorCredential, operator: str) -> Reply:
        command_id = stable_id("tg", str(update.update_id), cb.action.value, cb.target)
        correlation = f"telegram-{update.update_id}"
        ids = {"command_id": command_id, "correlation_id": correlation}
        ops, act = self._ops, cb.action
        if act not in (Action.CONFIRM, Action.CANCEL) and self._recorded(command_id):
            raise CommandCollisionError(command_id)  # this very update already ran (a replay after a crash)
        if act in (Action.APPROVE_DRAFT, Action.REJECT_DRAFT, Action.REJECT_DRAFT_REASON):
            draft = ops.get_draft(credential, cb.target)
            self._same(draft.version, cb.version)
            if act is Action.REJECT_DRAFT:
                rows = tuple((((reason.value.replace("_", " ").title(), encode(Action.REJECT_DRAFT_REASON, cb.target, cb.version, i)),)
                              for i, reason in enumerate(REJECT_REASONS)))
                return Reply("Why is the draft rejected?", "Choose a reason", rows)
            if act is Action.APPROVE_DRAFT:
                if draft.lead is None:
                    raise OperatorNotFoundError("lead")
                ops.approve_draft(credential, ApproveDraft(**ids, outbound_id=cb.target, draft_id=draft.draft_id,
                                                           content_hash=draft.content_hash, expected_outbound_version=draft.version,
                                                           expected_lead_version=draft.lead.version))
                return Reply("Draft approved. It is sent only by the normal dispatch step.", "Approved", close_card=True)
            reason = _pick(REJECT_REASONS, cb.argument)
            ops.reject_draft(credential, RejectDraft(**ids, outbound_id=cb.target, draft_id=draft.draft_id,
                                                     expected_outbound_version=draft.version, reason=reason))
            return Reply(f"Draft rejected ({reason.value}).", "Rejected", close_card=True)
        if act in (Action.RESOLVE_NO_ACTION, Action.RESOLVE_REPLIED):
            escalation = ops.get_escalation(credential, cb.target)
            self._same(escalation.version, cb.version)
            disposition = EscalationResolution.NO_ACTION if act is Action.RESOLVE_NO_ACTION else EscalationResolution.OPERATOR_REPLIED
            ops.resolve_escalation(credential, ResolveEscalation(**ids, escalation_id=cb.target, expected_escalation_version=escalation.version,
                                                                 disposition=disposition, note="Resolved by the operator in Telegram."))
            return Reply(f"Escalation resolved ({disposition.value}).", "Resolved", close_card=True)
        if act is Action.TAKE_OWNERSHIP:
            lead = ops.get_lead(credential, cb.target)
            self._same(lead.version, cb.version)
            ops.take_ownership(credential, TakeOwnership(**ids, lead_id=cb.target, expected_lead_version=lead.version))
            return Reply("You own this lead now: automation for it stopped.", "Owned", close_card=True)
        if act in (Action.APPROVE_QUALIFICATION, Action.CREATE_OPPORTUNITY):
            view = ops.get_lead_pipeline(credential, cb.target)
            if act is Action.APPROVE_QUALIFICATION:
                if view.qualification_version is None:
                    raise OperatorNotFoundError("qualification")
                self._same(view.qualification_version, cb.version)
                ops.approve_qualification(credential, ApproveQualification(
                    **ids, lead_id=cb.target, expected_lead_version=view.lead_version,
                    expected_qualification_version=view.qualification_version))
                return Reply("Qualification approved.", "Approved", close_card=True)
            self._same(view.lead_version, cb.version)
            ops.create_opportunity(credential, CreateOpportunity(**ids, lead_id=cb.target, expected_lead_version=view.lead_version))
            return Reply("Opportunity created. Prepare the proposal in the operator console.", "Created", close_card=True)
        if act in (Action.APPROVE_PROPOSAL, Action.MARK_PRESENTED, Action.CONFIRM_ACCEPTANCE, Action.MARK_DECLINED):
            version = self._version("proposal_revisions", cb.target)
            self._same(version, cb.version)
            common = {**ids, "revision_id": cb.target, "expected_revision_version": version}
            if act is Action.APPROVE_PROPOSAL:
                ops.approve_proposal(credential, ApproveProposal(**common))
                return Reply("Proposal approved (terms and totals are now frozen).", "Approved", close_card=True)
            if act is Action.MARK_PRESENTED:
                ops.mark_proposal_presented(credential, MarkProposalPresented(**common))
                return Reply("Proposal marked as presented.", "Presented", close_card=True)
            if act is Action.CONFIRM_ACCEPTANCE:
                ops.mark_proposal_accepted(credential, MarkProposalAccepted(**common))
                return Reply("Acceptance confirmed. The lead is NOT won yet: that is a separate decision.", "Confirmed",
                             close_card=True)
            ops.mark_proposal_declined(credential, MarkProposalDeclined(**common))
            return Reply("Proposal marked as declined.", "Declined", close_card=True)
        if act in (Action.APPROVE_TERM, Action.REJECT_TERM):
            with self._db.transaction() as uow:
                request = uow.term_requests.get(cb.target)
                term = (uow.commercial_terms.get(term_row_id(request.opportunity_id, request.term_type, request.term_key))
                        if request is not None else None)
            if request is None:
                raise OperatorNotFoundError("term request")
            self._same(request.version, cb.version)
            if act is Action.APPROVE_TERM:
                ops.approve_term_request(credential, ApproveTermRequest(**ids, request_id=cb.target, expected_request_version=request.version,
                                                                        expected_term_version=term.version if term else None))
                return Reply("Term request approved.", "Approved", close_card=True)
            ops.reject_term_request(credential, RejectTermRequest(**ids, request_id=cb.target, expected_request_version=request.version,
                                                                  reason="Rejected by the operator in Telegram."))
            return Reply("Term request rejected.", "Rejected", close_card=True)
        if act is Action.ACKNOWLEDGE_OBJECTION:
            version = self._version("objections", cb.target)
            self._same(version, cb.version)
            ops.update_objection(credential, UpdateObjection(**ids, objection_id=cb.target, expected_objection_version=version,
                                                             status=ObjectionStatus.ACKNOWLEDGED))
            return Reply("Objection acknowledged.", "Acknowledged", close_card=True)
        if act is Action.DISMISS_SIGNAL:
            version = self._version("commercial_signals", cb.target)
            self._same(version, cb.version)
            ops.dismiss_commercial_signal(credential, DismissCommercialSignal(**ids, signal_id=cb.target,
                                                                              expected_signal_version=version))
            return Reply("Signal dismissed (not a customer decision).", "Dismissed", close_card=True)
        if act in (Action.WON, Action.LOST, Action.LOST_REASON, Action.DNC):
            lead = ops.get_lead(credential, cb.target)
            self._same(lead.version, cb.version)
            if act is Action.LOST:
                rows = tuple((((reason.value.replace("_", " ").title(), encode(Action.LOST_REASON, cb.target, cb.version, i)),)
                              for i, reason in enumerate(LOST_REASONS)))
                return Reply("Why is the lead lost?", "Choose a reason", rows)
            argument = _pick(LOST_REASONS, cb.argument).value if act is Action.LOST_REASON else None
            terminal = {Action.WON: "WON", Action.LOST_REASON: "LOST", Action.DNC: "DNC"}[act]
            token = self._confirmation(update, operator, terminal, cb.target, lead.version, argument)
            what = {"WON": "mark this lead WON", "LOST": f"mark this lead LOST ({argument})",
                    "DNC": "put this contact on DO NOT CONTACT"}[terminal]
            return Reply(f"Please confirm: {what}? This cannot be undone from here. Valid for 5 minutes.", "Confirm?",
                         ((("Confirm", encode(Action.CONFIRM, token)), ("Cancel", encode(Action.CANCEL, token))),))
        if act in (Action.CONFIRM, Action.CANCEL):
            return self._confirm(update, cb, credential, operator)
        raise OperatorNotFoundError("action")

    def _recorded(self, command_id: str) -> bool:
        """Stage 7 recorded this deterministic command id: its outcome is committed."""
        with self._db.transaction() as uow:
            return uow.idempotency.exists(f"operator:command:{command_id}")

    @staticmethod
    def _same(current: int, seen: int | None) -> None:
        if seen is None or current != seen:
            raise StaleCommandError(())

    def _version(self, table: str, entity_id: str) -> int:
        with self._db.transaction() as uow:
            found = getattr(uow, table).get(entity_id)
        if found is None:
            raise OperatorNotFoundError(table)
        return int(found.version)

    # ---- Confirmations ------------------------------------------------------------------------------------

    def _confirmation(self, update: Update, operator: str, action: str, target: str, version: int, argument: str | None) -> str:
        token = hashlib.sha256(f"{update.update_id}|{action}|{target}|{argument}".encode()).hexdigest()[:16]
        now = self._clock.now()
        assert update.chat_id is not None
        with self._db.transaction() as uow:
            if uow.operator_channel.get_confirmation(token) is None:  # a replayed update reuses its token
                uow.operator_channel.add_confirmation(OperatorConfirmation(
                    confirmation_id=token, provider=PROVIDER, operator_id=operator, chat_id=update.chat_id, action=action,
                    target_id=target, target_version=version, argument=argument, created_at=now,
                    expires_at=now + CONFIRMATION_TTL))
        return token

    def _confirm(self, update: Update, cb: Callback, credential: OperatorCredential, operator: str) -> Reply:
        now = self._clock.now()
        with self._db.transaction() as uow:
            pending = uow.operator_channel.get_confirmation(cb.target)
        if pending is None or pending.operator_id != operator or pending.chat_id != update.chat_id:
            return Reply("This confirmation is not yours or does not exist.", "Not permitted")
        command_id = stable_id("tgc", pending.confirmation_id)
        if pending.status is not ConfirmationStatus.PENDING or self._recorded(command_id):
            self._finish(pending, ConfirmationStatus.USED, now)  # no-op unless the crash fell between the two
            return Reply("Already handled.", "Already handled", close_card=True)
        if cb.action is Action.CANCEL:
            self._finish(pending, ConfirmationStatus.CANCELLED, now)
            return Reply("Cancelled. Nothing changed.", "Cancelled", close_card=True)
        if pending.expires_at <= now:
            self._finish(pending, ConfirmationStatus.CANCELLED, now)
            return Reply("This confirmation expired. Start again from a fresh card.", "Expired", close_card=True)
        lead = self._ops.get_lead(credential, pending.target_id)
        if lead.version != pending.target_version:  # the lead changed after the operator decided
            self._finish(pending, ConfirmationStatus.CANCELLED, now)
            raise StaleCommandError(())
        ids = {"command_id": command_id, "correlation_id": f"telegram-{update.update_id}"}
        try:
            self._terminal(pending, ids, credential)
        except (CommandRejectedError, StaleCommandError):
            self._finish(pending, ConfirmationStatus.CANCELLED, now)  # refused: never reusable later
            raise
        self._finish(pending, ConfirmationStatus.USED, now)
        done = {"WON": "Lead marked WON.", "LOST": f"Lead marked LOST ({pending.argument}).",
                "DNC": "Contact is on do-not-contact; automation for it stopped."}[pending.action]
        return Reply(done, "Done", close_card=True)

    def _terminal(self, pending: OperatorConfirmation, ids: dict[str, str], credential: OperatorCredential) -> None:
        lead_id, version = pending.target_id, pending.target_version
        with self._db.transaction() as uow:
            opportunity = uow.opportunities.get_active_for_lead(lead_id)
            conversations = [c for c in uow.conversations.list_by_lead(lead_id) if c.status is not ConversationStatus.DO_NOT_CONTACT]
            member = uow.campaign_members.get_by_lead(lead_id)
        if pending.action == "WON":
            if opportunity is None:
                raise CommandRejectedError(())
            self._ops.mark_lead_won(credential, MarkLeadWon(**ids, lead_id=lead_id, expected_lead_version=version,
                                                            opportunity_id=opportunity.opportunity_id,
                                                            expected_opportunity_version=opportunity.version))
        elif pending.action == "LOST":
            self._ops.mark_lead_lost(credential, MarkLeadLost(**ids, lead_id=lead_id, expected_lead_version=version,
                                                              expected_opportunity_version=opportunity.version if opportunity else None,
                                                              reason=LostReason(pending.argument)))
        else:  # DNC: through the conversation (Stage 9) or, before any reply, the campaign membership
            live = max(conversations, key=lambda c: (c.last_activity_at, c.conversation_id), default=None)
            if live is not None:
                self._ops.mark_do_not_contact(credential, MarkDoNotContact(
                    **ids, conversation_id=live.conversation_id, expected_conversation_version=live.version,
                    note="Do not contact (operator, Telegram)."))
            elif member is not None:
                self._ops.suppress_campaign_member(credential, SuppressCampaignMember(
                    **ids, member_id=member.member_id, expected_member_version=member.version,
                    note="Do not contact (operator, Telegram)."))
            else:
                raise CommandRejectedError(())

    def _finish(self, pending: OperatorConfirmation, status: ConfirmationStatus, now: datetime) -> None:
        with self._db.transaction() as uow:
            current = uow.operator_channel.get_confirmation(pending.confirmation_id)
            if current is None or current.status is not ConfirmationStatus.PENDING:
                return
            uow.operator_channel.update_confirmation(OperatorConfirmation.model_validate(current.model_dump() | {
                "status": status, "used_at": now if status is ConfirmationStatus.USED else None,
                "version": current.version + 1}), current.version)

    # ---- Notifications -------------------------------------------------------------------------------------

    def operator_plans(self, limit: int) -> list[SalesExecutionPlan]:
        """Stage 14's operator queue (its own order), plus recovery that cannot run by itself."""
        plans = list(self._orchestrator.queue(ExecutionQueue.OPERATOR, limit=limit))
        stuck = [p for p in self._orchestrator.queue(ExecutionQueue.RECOVERY, limit=limit) if not p.executable]
        return (plans + stuck)[:limit]

    def notify(self, limit: int) -> tuple[int, int, str | None]:
        """Send at most ``limit`` new cards (each card once per operator chat and plan
        version). Returns (sent, not sent, stop reason).

        Delivery is claimed durably, so concurrent passes never both send one card:
        1. claim (CAS, IMMEDIATE transaction): create the row as CLAIMED, or re-claim a
           FAILED one or a CLAIMED one whose lease expired; anything else is skipped;
        2. CLAIMED -> SUBMITTING with the claim token, committed before ``sendMessage``
           (a lost race or a re-claimed lease ends here, nothing sent);
        3. SUBMITTING -> SENT / FAILED (Telegram refused, or never reached) / UNKNOWN (the
           request may have been delivered). UNKNOWN and an abandoned SUBMITTING are never
           resent automatically: no duplicate card; the operator still has /queue."""
        sent = failed = 0
        plans = self.operator_plans(limit)
        for chat_id in sorted(self._operators_by_chat()):
            credential = credential_for(chat_id, chat_id)
            for plan in plans:
                if sent + failed >= limit:
                    return sent, failed, None
                key = stable_id("tn", PROVIDER, str(chat_id), plan.lead_id, plan.action.value, plan.fingerprint)
                claim = self._claim(key, chat_id, plan)
                if claim is None:
                    continue  # sent, in flight elsewhere, or uncertain
                try:
                    text, buttons = self.card(plan, credential)
                except (OperatorNotFoundError, OperatorUnauthorizedError):
                    self._settle(key, claim, NotificationStatus.FAILED, error="ITEM_CHANGED", phase=NotificationStatus.CLAIMED)
                    continue  # changed meanwhile; the next plan will say so
                if not self._settle(key, claim, NotificationStatus.SUBMITTING, phase=NotificationStatus.CLAIMED):
                    continue  # the lease was lost to another worker: it owns delivery now
                try:
                    message = self._api.send_message(chat_id, text, buttons=buttons)
                except TelegramError as exc:
                    failed += 1
                    outcome = NotificationStatus.UNKNOWN if exc.uncertain else NotificationStatus.FAILED
                    self._settle(key, claim, outcome, error=exc.code.value)
                    if exc.code is TelegramCode.RATE_LIMITED:
                        return sent, failed, "RATE_LIMITED"  # no busy loop: the next pass continues
                    continue
                sent += 1
                self._settle(key, claim, NotificationStatus.SENT, message_id=message.message_id)
        return sent, failed, None

    def _operators_by_chat(self) -> dict[int, str]:
        return self._operators.configured()

    def _claim(self, key: str, chat_id: int, plan: SalesExecutionPlan) -> str | None:
        """The claim token if this worker now owns delivery of the card, else None."""
        now = self._clock.now()
        lease = now + self._lease
        with self._db.transaction() as uow:
            current = uow.operator_channel.get_notification(key)
            if current is None:
                token = stable_id("nc", key, "1")
                uow.operator_channel.add_notification(OperatorNotification(
                    notification_id=key, provider=PROVIDER, chat_id=chat_id, subject_id=plan.lead_id,
                    action=plan.action.value, plan_fingerprint=plan.fingerprint, status=NotificationStatus.CLAIMED,
                    claim_token=token, claimed_by=self._worker_id, lease_expires_at=lease, created_at=now, updated_at=now))
                return token
            expired = current.status is NotificationStatus.CLAIMED and current.lease_expires_at is not None                 and current.lease_expires_at <= now
            if current.status is not NotificationStatus.FAILED and not expired:
                return None
            token = stable_id("nc", key, str(current.claim_count + 1))
            uow.operator_channel.update_notification(OperatorNotification.model_validate(current.model_dump() | {
                "status": NotificationStatus.CLAIMED, "claim_token": token, "claimed_by": self._worker_id,
                "claim_count": current.claim_count + 1, "lease_expires_at": lease,
                "updated_at": max(now, current.updated_at), "version": current.version + 1}), current.version)
            return token

    def _settle(self, key: str, token: str, status: NotificationStatus, *, phase: NotificationStatus = NotificationStatus.SUBMITTING,
                error: str | None = None, message_id: int | None = None) -> bool:
        """Move the card from ``phase`` to ``status`` only for the current claim holder."""
        now = self._clock.now()
        with self._db.transaction() as uow:
            current = uow.operator_channel.get_notification(key)
            if current is None or current.claim_token != token or current.status is not phase:
                return False  # a stale claim never moves the card
            uow.operator_channel.update_notification(OperatorNotification.model_validate(current.model_dump() | {
                "status": status, "lease_expires_at": None, "provider_message_id": message_id,
                "last_error_code": error, "updated_at": max(now, current.updated_at), "version": current.version + 1}),
                current.version)
        return True

    def card(self, plan: SalesExecutionPlan, credential: OperatorCredential) -> tuple[str, Buttons]:
        """A plain-text card and only the buttons the current plan allows."""
        lead = self._ops.get_lead(credential, plan.lead_id)
        lines = [LABELS.get(plan.action, plan.action.value.replace("_", " ").title()),
                 f"Lead {short(plan.lead_id)} - {lead.stage.value}, {lead.status.value}"]
        if lead.contact_email:
            lines.append(f"Contact: {lead.contact_email}")
        if plan.reasons:
            lines.append("Why: " + ", ".join(plan.reasons))
        if plan.blockers:
            lines.append("Blocking: " + ", ".join(b.value for b in plan.blockers))
        rows: list[tuple[tuple[str, str], ...]] = []
        action, refs = plan.action, plan.refs
        if action in (A.REVIEW_REPLY_DRAFT, A.REVIEW_CAMPAIGN_DRAFT) and refs.outbound_ids:
            draft = self._ops.get_draft(credential, refs.outbound_ids[0])
            if draft.customer is not None:
                lines += ["", "Customer wrote (untrusted):", excerpt(draft.customer.body_text, 800)]
            kind = "first touch" if draft.kind is OutboundKind.FIRST_TOUCH else draft.kind.value.lower().replace("_", " ")
            lines += ["", f"Draft {kind} - subject: {excerpt(draft.generated_draft.subject, 150)}",
                      excerpt(draft.generated_draft.body, 1500)]
            if action is A.REVIEW_REPLY_DRAFT and not draft.evidence:
                # Generated text that cites no approved knowledge: only the reviewer can catch an
                # unsupported capability claim the deterministic checks cannot recognize.
                lines.append("Note: this draft cites no approved knowledge. Check every factual statement.")
            if draft.blockers:
                lines.append("Approval blocked now: " + ", ".join(b.value for b in draft.blockers))
            rows.append((("Approve", encode(Action.APPROVE_DRAFT, draft.outbound_id, draft.version)),
                         ("Reject", encode(Action.REJECT_DRAFT, draft.outbound_id, draft.version))))
        elif action is A.ESCALATION_REVIEW and refs.escalation_ids:
            escalation = self._ops.get_escalation(credential, refs.escalation_ids[0])
            lines.append("Reasons: " + ", ".join(r.value for r in escalation.reasons))
            if escalation.customer is not None:
                lines += ["", "Customer wrote (untrusted):", excerpt(escalation.customer.body_text, 800)]
            row = [("Resolve: no action", encode(Action.RESOLVE_NO_ACTION, escalation.escalation_id, escalation.version)),
                   ("Resolve: I replied", encode(Action.RESOLVE_REPLIED, escalation.escalation_id, escalation.version))]
            rows.append(tuple(row))
            if lead.status is not LeadStatus.OPERATOR_OWNED:
                rows.append((("Take ownership", encode(Action.TAKE_OWNERSHIP, lead.lead_id, lead.version)),))
        elif action is A.ESCALATION_REVIEW and lead.status is not LeadStatus.OPERATOR_OWNED:
            rows.append((("Take ownership", encode(Action.TAKE_OWNERSHIP, lead.lead_id, lead.version)),))
        elif action is A.REVIEW_QUALIFICATION and not refs.conflict_ids:
            view = self._ops.get_lead_pipeline(credential, plan.lead_id)
            if view.qualification_version is not None:
                missing = [g.field for g in view.gaps if g.reason == "MISSING_REQUIRED"]
                lines.append("Still missing: " + ", ".join(missing) if missing else "All required facts are known.")
                rows.append((("Approve qualification", encode(Action.APPROVE_QUALIFICATION, plan.lead_id, view.qualification_version)),))
        elif action is A.CREATE_OPPORTUNITY:
            rows.append((("Create opportunity", encode(Action.CREATE_OPPORTUNITY, plan.lead_id, lead.version)),))
        elif refs.revision_id and action in (A.REVIEW_PROPOSAL, A.PRESENT_PROPOSAL, A.CONFIRM_ACCEPTANCE, A.DECIDE_LOSS):
            version = self._version("proposal_revisions", refs.revision_id)
            button = {A.REVIEW_PROPOSAL: ("Approve proposal", Action.APPROVE_PROPOSAL),
                      A.PRESENT_PROPOSAL: ("Mark presented", Action.MARK_PRESENTED),
                      A.CONFIRM_ACCEPTANCE: ("Confirm acceptance", Action.CONFIRM_ACCEPTANCE),
                      A.DECIDE_LOSS: ("Proposal declined", Action.MARK_DECLINED)}[action]
            rows.append(((button[0], encode(button[1], refs.revision_id, version)),))
        elif action is A.REVIEW_TERM_REQUEST and refs.request_ids:
            version = self._version("term_requests", refs.request_ids[0])
            rows.append((("Approve term", encode(Action.APPROVE_TERM, refs.request_ids[0], version)),
                         ("Reject term", encode(Action.REJECT_TERM, refs.request_ids[0], version))))
        elif action is A.HANDLE_OBJECTION and refs.objection_ids:
            version = self._version("objections", refs.objection_ids[0])
            rows.append((("Acknowledge objection", encode(Action.ACKNOWLEDGE_OBJECTION, refs.objection_ids[0], version)),))
        if action in (A.CONFIRM_ACCEPTANCE, A.DECIDE_LOSS) and refs.signal_ids:
            version = self._version("commercial_signals", refs.signal_ids[0])
            rows.append((("Not a decision: dismiss", encode(Action.DISMISS_SIGNAL, refs.signal_ids[0], version)),))
        terminal: list[tuple[str, str]] = []
        if action is A.MARK_WON:
            terminal.append(("Won…", encode(Action.WON, lead.lead_id, lead.version)))
        if plan.owner is ExecutionOwner.OPERATOR:
            terminal.append(("Lost…", encode(Action.LOST, lead.lead_id, lead.version)))
        terminal.append(("Do not contact…", encode(Action.DNC, lead.lead_id, lead.version)))
        rows.append(tuple(terminal))
        return fit("\n".join(lines)), tuple(rows)

    def _queue_text(self) -> str:
        everything = self._orchestrator.queue(ExecutionQueue.OPERATOR)
        page = everything[: self._page]
        if not page:
            return "Nothing needs you right now."
        lines = [f"{len(everything)} item(s) need you. First {len(page)}:"]
        lines += [f"- {LABELS.get(p.action, p.action.value)} (lead {short(p.lead_id)})" for p in page]
        if len(everything) > len(page):
            lines.append(f"... and {len(everything) - len(page)} more.")
        return fit("\n".join(lines))

    # ---- Telegram UI (best effort) ---------------------------------------------------------------------------

    def _send(self, chat_id: int, text: str, buttons: Buttons = ()) -> None:
        try:
            self._api.send_message(chat_id, fit(text), buttons=buttons)
        except TelegramError:
            pass  # UI only: the business outcome (if any) is already committed

    def _ack(self, callback_id: str, text: str) -> None:
        try:
            self._api.answer_callback_query(callback_id, text)
        except TelegramError:
            pass

    def _close(self, chat_id: int, message_id: int, note: str) -> None:
        try:
            self._api.edit_message_text(chat_id, message_id, f"[{note}] This card is closed.")
        except TelegramError:
            pass


class _Once:
    """The operator service, where a Stage 7 replay (``replayed=True``: this command id
    already ran, e.g. a concurrent pass handled the same update) counts as "already
    handled" rather than as a new action."""

    def __init__(self, service: OperatorService) -> None:
        self._service = service

    def __getattr__(self, name: str) -> Callable[..., object]:
        method = getattr(self._service, name)

        def call(*args: object, **kwargs: object) -> object:
            result = method(*args, **kwargs)
            if getattr(result, "replayed", False) is True:
                raise CommandCollisionError("replayed")
            return result

        return call


def _pick[T](options: tuple[T, ...], index: int | None) -> T:
    if index is None or not 0 <= index < len(options):
        raise OperatorNotFoundError("option")
    return options[index]
