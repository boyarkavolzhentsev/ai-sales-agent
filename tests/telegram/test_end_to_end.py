"""Whole business cycles where every operator decision is a Telegram button press: Stage 14
plans -> review card -> Stage 7 command -> Stage 14 executes the automatic step -> Stage 8
sends (fake transport, or the fake Gmail API). WON, LOST and DNC need a confirmation;
nothing is ever WON automatically; acceptance confirmation is not WON."""

from datetime import timedelta
from pathlib import Path

from app.core.enums import CloseReason, DNCScope, LeadStage, OutboundStatus
from app.orchestration import ExecutionAction as A
from app.orchestration import ExecutionOutcome as X
from app.orchestration import ExecutionOwner as O
from app.persistence import ConfirmationStatus
from tests.campaign.builders import PROSPECT, campaign_messages
from tests.commercial.builders import asks
from tests.orchestration.builders import customer_replies, enrolled, prepare_proposal
from tests.telegram.builders import Console, console
from tests.telegram.fakes import ALICE_CHAT, BOB_CHAT


def plan(c: Console, owner: O, action: A) -> None:
    current = c.world.plan()
    assert (current.owner, current.action) == (owner, action), current


def auto(c: Console, owner: O, action: A, expected: str, *, dispatch: bool = False) -> None:
    plan(c, owner, action)
    result = c.world.execute(dispatch=dispatch)
    assert (result.outcome, result.subsystem_outcome) == (X.EXECUTED, expected), result


def press(c: Console, label: str, chat: int = ALICE_CHAT) -> dict[str, int]:
    """Cards are synced first; the newest button with this label is pressed."""
    c.sync()
    buttons = c.telegram.buttons_for(chat, label)
    assert buttons, (label, [s.text.splitlines()[0] for s in c.telegram.sent if s.chat_id == chat])
    c.telegram.press(chat, buttons[-1])
    return c.sync().outcomes


def confirm(c: Console, label: str, chat: int = ALICE_CHAT) -> dict[str, int]:
    assert press(c, label, chat) == {"ACTION": 1}
    assert c.telegram.texts(chat)[-1].startswith("Please confirm")
    [*_, token] = c.telegram.buttons_for(chat, "Confirm")
    c.telegram.press(chat, token)
    return c.sync().outcomes


def engaged(c: Console) -> None:
    enrolled(c.world)
    auto(c, O.CAMPAIGN, A.PREPARE_CAMPAIGN_TOUCH, "DRAFT_CREATED")
    plan(c, O.OPERATOR, A.REVIEW_CAMPAIGN_DRAFT)
    assert press(c, "Approve") == {"ACTION": 1}
    auto(c, O.CAMPAIGN, A.SEND_APPROVED_MESSAGE, "ACCEPTED", dispatch=True)
    customer_replies(c.world, "p-reply")
    plan(c, O.OPERATOR, A.REVIEW_REPLY_DRAFT)
    assert press(c, "Approve") == {"ACTION": 1}
    auto(c, O.CONVERSATION, A.SEND_APPROVED_MESSAGE, "ACCEPTED", dispatch=True)


def presented(c: Console) -> None:
    engaged(c)
    plan(c, O.OPERATOR, A.REVIEW_QUALIFICATION)
    assert press(c, "Approve qualification") == {"ACTION": 1}
    plan(c, O.OPERATOR, A.CREATE_OPPORTUNITY)
    assert press(c, "Create opportunity") == {"ACTION": 1}
    plan(c, O.OPERATOR, A.PREPARE_PROPOSAL)
    prepare_proposal(c.world)  # proposal content is authored in the operator console, not in Telegram
    plan(c, O.OPERATOR, A.REVIEW_PROPOSAL)
    assert press(c, "Approve proposal") == {"ACTION": 1}
    plan(c, O.OPERATOR, A.PRESENT_PROPOSAL)
    assert press(c, "Mark presented") == {"ACTION": 1}
    auto(c, O.CONVERSATION, A.PROCESS_FOLLOW_UP, "SCHEDULED")


def test_a_won_cycle_decided_entirely_in_telegram(tmp_path: Path) -> None:
    c = console(tmp_path)
    presented(c)
    c.world.commercial.default = asks(accept=True)
    customer_replies(c.world, "p-accept", body="We accept your proposal. What does the Basic plan cost per month?")
    assert press(c, "Approve") == {"ACTION": 1}
    auto(c, O.CONVERSATION, A.SEND_APPROVED_MESSAGE, "ACCEPTED", dispatch=True)
    plan(c, O.OPERATOR, A.CONFIRM_ACCEPTANCE)
    c.sync()
    assert c.telegram.buttons_for(ALICE_CHAT, "Won…") == []  # no WON before the acceptance is confirmed
    assert press(c, "Confirm acceptance") == {"ACTION": 1}
    assert any("NOT won yet" in t for t in c.telegram.texts(ALICE_CHAT))
    assert c.world.lead_row().stage is not LeadStage.CLOSED  # acceptance confirmation is never WON
    plan(c, O.OPERATOR, A.MARK_WON)
    assert c.world.execute(dispatch=True).outcome is X.REQUIRES_OPERATOR  # never auto-WON
    assert confirm(c, "Won…") == {"ACTION": 1}
    lead = c.world.lead_row()
    assert (lead.stage, lead.close_reason) == (LeadStage.CLOSED, CloseReason.WON)
    plan(c, O.NONE, A.NO_ACTION)
    assert len(c.world.transport.calls) == 3  # first touch and two replies: Telegram sent no email
    sent_before = len(c.telegram.sent)
    c.sync()
    assert len(c.telegram.sent) == sent_before  # a closed lead produces no more cards


def test_a_lost_decision_needs_a_reason_and_a_confirmation(tmp_path: Path) -> None:
    c = console(tmp_path)
    presented(c)
    c.world.commercial.default = asks(decline=True)
    customer_replies(c.world, "p-decline", body="We went with another vendor. What did the Basic plan cost per month?")
    assert press(c, "Approve") == {"ACTION": 1}
    auto(c, O.CONVERSATION, A.SEND_APPROVED_MESSAGE, "ACCEPTED", dispatch=True)
    plan(c, O.OPERATOR, A.DECIDE_LOSS)
    assert press(c, "Lost…") == {"ACTION": 1}
    assert c.world.lead_row().stage is not LeadStage.CLOSED  # choosing "Lost" only asks why
    reasons = c.telegram.sent[-1].buttons
    c.telegram.press(ALICE_CHAT, reasons[0][0][1])
    assert c.sync().outcomes == {"ACTION": 1}
    assert c.world.lead_row().stage is not LeadStage.CLOSED  # a reason is not a confirmation
    [*_, token] = c.telegram.buttons_for(ALICE_CHAT, "Confirm")
    c.telegram.press(ALICE_CHAT, token)
    assert c.sync().outcomes == {"ACTION": 1}
    lead = c.world.lead_row()
    assert (lead.stage, lead.close_reason) == (LeadStage.CLOSED, CloseReason.LOST)


def test_dnc_before_any_reply_suppresses_the_campaign_member(tmp_path: Path) -> None:
    c = console(tmp_path)
    enrolled(c.world)
    auto(c, O.CAMPAIGN, A.PREPARE_CAMPAIGN_TOUCH, "DRAFT_CREATED")
    assert confirm(c, "Do not contact…") == {"ACTION": 1}
    current = c.world.plan()
    assert (current.owner, current.action) == (O.NONE, A.NO_AUTOMATION) and current.blockers[0].value == "DNC"
    assert c.world.execute(dispatch=True).outcome is not X.EXECUTED
    assert c.world.transport.calls == []
    # The stale review card can no longer approve anything.
    [approve] = c.telegram.buttons_for(ALICE_CHAT, "Approve")
    c.telegram.press(ALICE_CHAT, approve)
    assert set(c.sync().outcomes) <= {"STALE", "REJECTED", "NOT_FOUND"}
    assert all(m.status is not OutboundStatus.SENT for m in c.world.messages())


def test_dnc_during_a_conversation_goes_through_stage9(tmp_path: Path) -> None:
    c = console(tmp_path)
    engaged(c)
    assert confirm(c, "Do not contact…") == {"ACTION": 1}
    with c.db.transaction() as uow:
        conversations = uow.conversations.list_by_lead(c.world.lead)
        entries = uow.dnc.list_for_value(DNCScope.EMAIL, PROSPECT)
    assert any(conv.status.value == "DO_NOT_CONTACT" for conv in conversations) and entries
    assert c.world.plan().action is A.NO_AUTOMATION
    with c.db.transaction() as uow:
        jobs = [j for conv in conversations for j in uow.follow_up_jobs.list_for_conversation(conv.conversation_id)]
        member = uow.campaign_members.get(c.world.member_id or "")
    assert all(j.status.value not in ("SCHEDULED", "CLAIMED") for j in jobs)  # no follow-up survives
    assert member is not None and member.status.value != "ACTIVE"
    assert c.world.app.execution_pass(dispatch_approved=True).attempted == 0


# ---- Confirmation security ----------------------------------------------------------------------------------


def pending_won(c: Console) -> str:
    """Alice asked to mark the lead WON; returns the Confirm button's data."""
    presented(c)
    c.world.commercial.default = asks(accept=True)
    customer_replies(c.world, "p-accept", body="We accept your proposal. What does the Basic plan cost per month?")
    assert press(c, "Approve") == {"ACTION": 1}
    auto(c, O.CONVERSATION, A.SEND_APPROVED_MESSAGE, "ACCEPTED", dispatch=True)
    assert press(c, "Confirm acceptance") == {"ACTION": 1}
    assert press(c, "Won…") == {"ACTION": 1}
    [*_, token] = c.telegram.buttons_for(ALICE_CHAT, "Confirm")
    return token


def confirmation_status(c: Console, data: str) -> ConfirmationStatus:
    with c.db.transaction() as uow:
        return uow.operator_channel.get_confirmation(data.split("|")[1]).status  # type: ignore[union-attr]


def test_only_the_same_operator_in_the_same_chat_can_confirm(tmp_path: Path) -> None:
    c = console(tmp_path)
    token = pending_won(c)
    c.telegram.press(BOB_CHAT, token)  # another operator replays Alice's confirmation
    assert c.sync().outcomes == {"ACTION": 1}
    assert c.telegram.texts(BOB_CHAT)[-1] == "This confirmation is not yours or does not exist."
    assert c.world.lead_row().stage is not LeadStage.CLOSED
    assert confirmation_status(c, token) is ConfirmationStatus.PENDING
    c.telegram.press(ALICE_CHAT, token)
    c.telegram.press(ALICE_CHAT, token)  # a double tap
    assert c.sync().outcomes == {"ACTION": 2}
    assert c.telegram.texts(ALICE_CHAT)[-2:] == ["Lead marked WON.", "Already handled."]
    assert confirmation_status(c, token) is ConfirmationStatus.USED


def test_an_expired_confirmation_does_nothing(tmp_path: Path) -> None:
    c = console(tmp_path)
    token = pending_won(c)
    c.world.advance(timedelta(minutes=5, seconds=1))
    c.telegram.press(ALICE_CHAT, token)
    c.sync()
    assert c.telegram.texts(ALICE_CHAT)[-1].startswith("This confirmation expired")
    assert c.world.lead_row().stage is not LeadStage.CLOSED
    assert confirmation_status(c, token) is ConfirmationStatus.CANCELLED


def test_cancel_changes_nothing_and_cannot_be_confirmed_afterwards(tmp_path: Path) -> None:
    c = console(tmp_path)
    token = pending_won(c)
    [*_, cancel] = c.telegram.buttons_for(ALICE_CHAT, "Cancel")
    c.telegram.press(ALICE_CHAT, cancel)
    c.telegram.press(ALICE_CHAT, token)
    c.sync()
    assert c.telegram.texts(ALICE_CHAT)[-2:] == ["Cancelled. Nothing changed.", "Already handled."]
    assert c.world.lead_row().stage is not LeadStage.CLOSED


def test_a_lead_that_changed_after_the_request_is_not_confirmed(tmp_path: Path) -> None:
    from tests.operator.builders import AS_BOB
    from app.operator import TakeOwnership
    c = console(tmp_path)
    token = pending_won(c)
    lead = c.world.lead_row()
    c.world.ops.take_ownership(AS_BOB, TakeOwnership(command_id="cmd-bob-owns", correlation_id="c", lead_id=lead.lead_id,
                                                     expected_lead_version=lead.version))
    c.telegram.press(ALICE_CHAT, token)
    assert c.sync().outcomes == {"STALE": 1}
    assert c.world.lead_row().stage is not LeadStage.CLOSED
    assert confirmation_status(c, token) is ConfirmationStatus.CANCELLED


# ---- Gmail inbound -> Telegram review -> Stage 7 -> Stage 14 -> Stage 8 -> Gmail ----------------------------


def test_gmail_and_telegram_together(tmp_path: Path) -> None:
    from tests.gmail.fakes import customer_email
    c = console(tmp_path, gmail=True)
    assert c.app.email_sync().status.value == "INITIALIZED"
    enrolled(c.world)
    auto(c, O.CAMPAIGN, A.PREPARE_CAMPAIGN_TOUCH, "DRAFT_CREATED")
    assert press(c, "Approve") == {"ACTION": 1}  # Telegram approval through Stage 7
    auto(c, O.CAMPAIGN, A.SEND_APPROVED_MESSAGE, "ACCEPTED", dispatch=True)  # Stage 8 -> Gmail
    [first] = campaign_messages(c.db, c.world.lead)
    assert first.status is OutboundStatus.SENT and first.provider_message_id in c.gmail.messages
    c.gmail.deliver(customer_email(sender=PROSPECT, message_id="<reply-1@acme-prospect.example>", in_reply_to=first.rfc_message_id))
    assert c.app.email_sync().processed == 1  # Gmail inbound -> Stage 6 drafts a reply
    plan(c, O.OPERATOR, A.REVIEW_REPLY_DRAFT)
    c.sync()
    card = [s for s in c.telegram.sent if s.chat_id == ALICE_CHAT][-1]
    assert card.text.startswith("Reply draft to review") and "Customer wrote (untrusted):" in card.text
    assert press(c, "Approve") == {"ACTION": 1}
    auto(c, O.CONVERSATION, A.SEND_APPROVED_MESSAGE, "ACCEPTED", dispatch=True)
    assert sum(1 for m in c.gmail.messages.values() if "SENT" in m.labels) == 2


# ---- Commercial review items -----------------------------------------------------------------------------


def customer_says(c: Console, extraction: object, provider_message_id: str) -> None:
    c.world.commercial.default = extraction  # type: ignore[assignment]
    customer_replies(c.world, provider_message_id, body="How much is the Basic plan per month? Also see our note.")
    assert press(c, "Approve") == {"ACTION": 1}
    auto(c, O.CONVERSATION, A.SEND_APPROVED_MESSAGE, "ACCEPTED", dispatch=True)


def test_a_term_request_is_approved_or_rejected_in_telegram(tmp_path: Path) -> None:
    from app.core.enums import TermRequestStatus, TermType
    from tests.commercial.builders import text
    c = console(tmp_path)
    presented(c)
    customer_says(c, asks((TermType.PAYMENT_TERM, text("NET_60"))), "p-term")
    plan(c, O.OPERATOR, A.REVIEW_TERM_REQUEST)
    assert press(c, "Reject term") == {"ACTION": 1}
    with c.db.transaction() as uow:
        [request] = uow._tx.fetch_all("SELECT data FROM commercial_term_requests")  # noqa: SLF001
    assert f'"{TermRequestStatus.REJECTED.value}"' in request[0]


def test_an_objection_is_acknowledged_and_a_signal_dismissed(tmp_path: Path) -> None:
    from app.core.enums import ObjectionCategory
    c = console(tmp_path)
    presented(c)
    customer_says(c, asks(objections=((ObjectionCategory.PRICE, "Too expensive"),)), "p-objection")
    plan(c, O.OPERATOR, A.HANDLE_OBJECTION)
    assert press(c, "Acknowledge objection") == {"ACTION": 1}
    customer_says(c, asks(accept=True), "p-yes")
    plan(c, O.OPERATOR, A.CONFIRM_ACCEPTANCE)
    assert press(c, "Not a decision: dismiss") == {"ACTION": 1}
    assert c.world.plan().action is not A.CONFIRM_ACCEPTANCE
    assert c.world.lead_row().stage is not LeadStage.CLOSED
