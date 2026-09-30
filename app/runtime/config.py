"""Validated top-level runtime configuration.

Shared values (sender identity, mailboxes, limits, sending window, kill switch) are held
once and the existing per-subsystem configuration contracts are built from them, so the
subsystems can never disagree. Only the few per-subsystem tunables live in their own small
sections. Provider credentials are optional placeholders for the future integration
stage; nothing in Stage 11 needs or uses them, and they are never shown in reprs.
"""

from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Self

from pydantic import AfterValidator, Field, SecretStr, model_validator

from app.campaign import CampaignExecutionConfig
from app.conversation import FollowUpConfig
from app.core.models.base import CoreModel
from app.core.models.types import EmailAddress, NonEmptyStr
from app.core.validation import unique_items
from app.dispatch import DispatchConfig
from app.inbound import InboundConfig
from app.llm import SenderIdentity
from app.operator import OperatorConfig
from app.commercial import CommercialConfig
from app.pipeline import PipelineConfig
from app.persistence import MEMORY
from app.policy import KillSwitchState, LimitPolicy, SendingWindow


class RuntimeMode(StrEnum):
    """LOCAL: a file database for local runs. TEST: may use an in-memory database.
    A production mode does not exist before live integrations are built."""

    LOCAL = "LOCAL"
    TEST = "TEST"


class DispatchSettings(CoreModel):
    permit_ttl: Annotated[timedelta, Field(gt=timedelta(0), le=timedelta(minutes=30))] = timedelta(minutes=5)
    max_attempts: Annotated[int, Field(ge=1, le=5)] = 3


class FollowUpSettings(CoreModel):
    max_follow_ups: Annotated[int, Field(ge=1, le=5)] = 2
    first_delay: Annotated[timedelta, Field(gt=timedelta(0))] = timedelta(days=3)
    interval: Annotated[timedelta, Field(gt=timedelta(0))] = timedelta(days=4)
    lease: Annotated[timedelta, Field(gt=timedelta(0), le=timedelta(hours=1))] = timedelta(minutes=5)
    defer_delay: Annotated[timedelta, Field(gt=timedelta(0))] = timedelta(hours=1)


class CampaignSettings(CoreModel):
    value_question: NonEmptyStr = "What does the product integrate with?"
    value_questions: dict[str, NonEmptyStr] = {}
    lease: Annotated[timedelta, Field(gt=timedelta(0), le=timedelta(hours=1))] = timedelta(minutes=5)
    defer_delay: Annotated[timedelta, Field(gt=timedelta(0))] = timedelta(hours=1)


class WorkerSettings(CoreModel):
    worker_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")] = "local-worker"
    # Upper bound of jobs, conversations or messages one tick phase handles.
    batch_limit: Annotated[int, Field(ge=1, le=500)] = 25


class ProviderSecrets(CoreModel):
    """Placeholders for future live providers. Optional, unused in Stage 11, never printed."""

    email_api_token: SecretStr | None = None
    llm_api_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None


class RuntimeConfig(CoreModel):
    mode: RuntimeMode
    # Logging-safe identity of this application instance.
    app_id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")] = "ai-sales-agent"
    code_version: NonEmptyStr = "local"
    database_path: NonEmptyStr
    # Our own mailboxes: inbound self-loop detection and allowed sender mailboxes.
    mailboxes: Annotated[tuple[EmailAddress, ...], Field(min_length=1), AfterValidator(unique_items)]
    sender: SenderIdentity
    limits: LimitPolicy
    window: SendingWindow
    kill_switch: KillSwitchState
    operator_ids: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1), AfterValidator(unique_items)]
    dispatch: DispatchSettings = DispatchSettings()
    follow_up: FollowUpSettings = FollowUpSettings()
    campaign: CampaignSettings = CampaignSettings()
    worker: WorkerSettings = WorkerSettings()
    secrets: ProviderSecrets = ProviderSecrets()
    pipeline: PipelineConfig = PipelineConfig()
    commercial: CommercialConfig = CommercialConfig()

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.database_path == MEMORY and self.mode is not RuntimeMode.TEST:
            raise ValueError("an in-memory database is only allowed in TEST mode")
        if self.window.timezone != self.limits.timezone:
            raise ValueError("the sending window and the limits must use the same time zone")
        return self

    # ---- The existing subsystem contracts, built from the shared values ------------------

    def inbound_config(self) -> InboundConfig:
        return InboundConfig(own_addresses=self.mailboxes, sender=self.sender, code_version=self.code_version)

    def operator_config(self) -> OperatorConfig:
        return OperatorConfig(authorized_operator_ids=self.operator_ids, sender=self.sender)

    def dispatch_config(self) -> DispatchConfig:
        return DispatchConfig(
            sender_mailboxes=self.mailboxes, sender=self.sender, limits=self.limits, window=self.window,
            kill_switch=self.kill_switch, permit_ttl=self.dispatch.permit_ttl, max_attempts=self.dispatch.max_attempts,
        )

    def follow_up_config(self) -> FollowUpConfig:
        return FollowUpConfig(
            sender=self.sender, limits=self.limits, window=self.window, kill_switch=self.kill_switch,
            **self.follow_up.model_dump(),
        )

    def campaign_config(self) -> CampaignExecutionConfig:
        return CampaignExecutionConfig(
            sender=self.sender, limits=self.limits, window=self.window, kill_switch=self.kill_switch,
            **self.campaign.model_dump(),
        )
