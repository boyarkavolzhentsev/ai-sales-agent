from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.core.enums import (
    CloseReason,
    ContactDepartment,
    ContactSource,
    ContactType,
    EmailDirection,
    IcpFit,
    LeadOrigin,
    LeadStage,
    LeadStatus,
)
from app.core.models import EmailMessage, EmailThread, Lead, ProspectCompany, ProspectContact

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
NAIVE = datetime(2026, 1, 1, 12, 0)
HASH = "a" * 64


def email_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "message_id": "msg-1",
        "rfc_message_id": "<abc@mail.example.com>",
        "thread_id": "thr-1",
        "direction": EmailDirection.INBOUND,
        "mailbox": "sales@ourco.com",
        "from_address": "Buyer@Prospect.com",
        "to_addresses": ("sales@ourco.com",),
        "subject": "Question",
        "body_text": "Hello",
        "raw_ref": "raw/msg-1.eml",
        "raw_hash": HASH,
        "received_at": T0,
    }
    return base | overrides


def lead_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "lead_id": "lead-1",
        "contact_id": "contact-1",
        "company_id": "company-1",
        "origin": LeadOrigin.OUTBOUND,
        "campaign_id": "camp-1",
        "stage": LeadStage.NEW,
        "created_at": T0,
        "updated_at": T0,
    }
    return base | overrides


def company_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "company_id": "company-1",
        "name": "Prospect Ltd",
        "domain": "Prospect.com",
        "source": ContactSource.IMPORT,
        "created_at": T0,
        "updated_at": T0,
    }
    return base | overrides


def contact_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "contact_id": "contact-1",
        "company_id": "company-1",
        "email": "Partnerships@Prospect.com",
        "department": ContactDepartment.PARTNERSHIPS,
        "contact_type": ContactType.ROLE_ADDRESS,
        "source": ContactSource.IMPORT,
        "source_ref": "import-2026-01-01.csv",
        "collected_at": T0,
        "created_at": T0,
        "updated_at": T0,
    }
    return base | overrides


# ---- EmailMessage / EmailThread --------------------------------------------


def test_email_message_normalizes_addresses() -> None:
    message = EmailMessage(**email_kwargs())
    assert message.from_address == "buyer@prospect.com"
    assert message.is_bounce is False


def test_email_message_is_immutable_and_forbids_extra_fields() -> None:
    message = EmailMessage(**email_kwargs())
    with pytest.raises(ValidationError):
        message.subject = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        EmailMessage(**email_kwargs(unexpected="x"))


def test_email_direction_requires_matching_timestamp() -> None:
    with pytest.raises(ValidationError, match="received_at"):
        EmailMessage(**email_kwargs(received_at=None))
    with pytest.raises(ValidationError, match="sent_at"):
        EmailMessage(**email_kwargs(direction=EmailDirection.OUTBOUND))
    EmailMessage(**email_kwargs(direction=EmailDirection.OUTBOUND, sent_at=T0, received_at=None))


def test_email_message_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError):
        EmailMessage(**email_kwargs(received_at=NAIVE))


def test_email_message_rejects_duplicate_recipients_after_normalization() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        EmailMessage(**email_kwargs(to_addresses=("a@x.com", "A@X.com")))


def test_email_message_rejects_bad_hash_and_blank_ids() -> None:
    with pytest.raises(ValidationError):
        EmailMessage(**email_kwargs(raw_hash="A" * 64))
    with pytest.raises(ValidationError):
        EmailMessage(**email_kwargs(rfc_message_id="   "))
    with pytest.raises(ValidationError):
        EmailMessage(**email_kwargs(message_id="has space"))


def test_email_thread() -> None:
    thread = EmailThread(
        thread_id="thr-1",
        mailbox="sales@ourco.com",
        participant_addresses=("buyer@prospect.com", "sales@ourco.com"),
        subject_normalized="question",
        message_ids=("msg-1", "msg-2"),
    )
    assert thread.lead_id is None
    with pytest.raises(ValidationError):
        EmailThread(
            thread_id="thr-1",
            mailbox="sales@ourco.com",
            participant_addresses=(),
            subject_normalized="",
        )
    with pytest.raises(ValidationError, match="duplicate"):
        EmailThread(
            thread_id="thr-1",
            mailbox="sales@ourco.com",
            participant_addresses=("a@x.com",),
            subject_normalized="",
            message_ids=("m1", "m1"),
        )


# ---- Lead -------------------------------------------------------------------


def test_lead_defaults() -> None:
    lead = Lead(**lead_kwargs())
    assert lead.status is LeadStatus.AUTOMATED
    assert lead.version == 1


def test_closed_lead_requires_close_reason() -> None:
    with pytest.raises(ValidationError, match="close_reason"):
        Lead(**lead_kwargs(stage=LeadStage.CLOSED))
    lead = Lead(**lead_kwargs(stage=LeadStage.CLOSED, close_reason=CloseReason.NOT_INTERESTED))
    assert lead.close_reason is CloseReason.NOT_INTERESTED


@pytest.mark.parametrize("stage", [s for s in LeadStage if s is not LeadStage.CLOSED])
def test_open_lead_must_not_have_close_reason(stage: LeadStage) -> None:
    with pytest.raises(ValidationError, match="close_reason"):
        Lead(**lead_kwargs(stage=stage, close_reason=CloseReason.WON))


def test_lead_updated_at_cannot_precede_created_at() -> None:
    with pytest.raises(ValidationError, match="updated_at"):
        Lead(**lead_kwargs(updated_at=T0 - timedelta(seconds=1)))


def test_lead_rejects_naive_datetime_and_bad_version() -> None:
    with pytest.raises(ValidationError):
        Lead(**lead_kwargs(created_at=NAIVE))
    with pytest.raises(ValidationError):
        Lead(**lead_kwargs(version=0))


# ---- Prospects --------------------------------------------------------------


def test_company_domain_is_normalized() -> None:
    assert ProspectCompany(**company_kwargs()).domain == "prospect.com"
    with pytest.raises(ValidationError):
        ProspectCompany(**company_kwargs(domain="  "))
    with pytest.raises(ValidationError):
        ProspectCompany(**company_kwargs(domain="not a domain"))


def test_company_icp_assessment_requires_timestamp() -> None:
    with pytest.raises(ValidationError, match="icp_assessed_at"):
        ProspectCompany(**company_kwargs(icp_fit=IcpFit.FIT))
    ProspectCompany(**company_kwargs(icp_fit=IcpFit.FIT, icp_assessed_at=T0))


def test_company_country_must_be_iso_alpha2_uppercase() -> None:
    ProspectCompany(**company_kwargs(country="UA"))
    with pytest.raises(ValidationError):
        ProspectCompany(**company_kwargs(country="ua"))


def test_contact_email_is_normalized_and_required() -> None:
    assert ProspectContact(**contact_kwargs()).email == "partnerships@prospect.com"
    with pytest.raises(ValidationError):
        ProspectContact(**contact_kwargs(email=""))
    with pytest.raises(ValidationError):
        ProspectContact(**contact_kwargs(email="Partners <p@prospect.com>"))


def test_contact_rejects_naive_collected_at_and_bad_locale() -> None:
    with pytest.raises(ValidationError):
        ProspectContact(**contact_kwargs(collected_at=NAIVE))
    with pytest.raises(ValidationError):
        ProspectContact(**contact_kwargs(locale="English"))
    assert ProspectContact(**contact_kwargs(locale="en-GB")).locale == "en-GB"


# ---- Optimistic-concurrency version -------------------------------------------


def test_company_contact_thread_have_concurrency_version() -> None:
    thread_kwargs: dict[str, object] = {
        "thread_id": "thr-1",
        "mailbox": "sales@ourco.com",
        "participant_addresses": ("buyer@prospect.com",),
        "subject_normalized": "question",
    }
    cases = [
        (ProspectCompany, company_kwargs()),
        (ProspectContact, contact_kwargs()),
        (EmailThread, thread_kwargs),
    ]
    for model, kwargs in cases:
        assert model(**kwargs).version == 1
        assert model(**(kwargs | {"version": 7})).version == 7
        for bad in (0, -1):
            with pytest.raises(ValidationError):
                model(**(kwargs | {"version": bad}))
