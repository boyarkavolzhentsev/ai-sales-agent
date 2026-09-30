"""Composition root: the adapters the runtime plugs in, and the services built from them.

Adapters are the existing boundary Protocols; a future real provider implements them:
- ``EmailTransport`` and ``DispatchReconciler`` (Stage 8),
- ``LLMTransport`` (Stage 5, behind ``StructuredLLM``),
- ``OperatorAuthenticator`` (Stage 7).
The offline default configures none of the provider adapters, so the capabilities that
need them are simply unavailable: no fake ever fabricates a provider outcome in a real
database. Tests pass deterministic fakes explicitly.

Construction performs no I/O and starts nothing: ``build_services`` only wires objects
around one Database and one Clock.
"""

from dataclasses import dataclass, field

from app.campaign import CampaignEnroller, CampaignExecutor, CampaignScheduler
from app.conversation import FollowUpExecutor, FollowUpScheduler
from app.dispatch import DispatchReconciler, DispatchService, EmailTransport
from app.inbound import InboundService
from app.llm import LLMTransport, StructuredLLM
from app.operator import OperatorAuthenticator, OperatorCredential, OperatorService
from app.persistence import Clock, Database
from app.runtime.config import RuntimeConfig


class DenyAllAuthenticator:
    """The default operator boundary until a trusted one (e.g. Telegram) is configured."""

    def authenticate(self, credential: OperatorCredential) -> str | None:
        return None


@dataclass(frozen=True)
class Adapters:
    email_transport: EmailTransport | None = None
    reconciler: DispatchReconciler | None = None
    llm_transport: LLMTransport | None = None
    authenticator: OperatorAuthenticator = field(default_factory=DenyAllAuthenticator)


def offline_adapters() -> Adapters:
    """Nothing external configured: dispatch, reconciliation and inbound analysis are
    unavailable; campaign and follow-up ticks (which only produce drafts) work."""
    return Adapters()


@dataclass(frozen=True)
class Capabilities:
    dispatch: bool
    reconciliation: bool
    inbound: bool

    @classmethod
    def of(cls, adapters: Adapters) -> "Capabilities":
        # Reconciliation resolves attempts made by a transport; both belong to one provider.
        return cls(
            dispatch=adapters.email_transport is not None,
            reconciliation=adapters.email_transport is not None and adapters.reconciler is not None,
            inbound=adapters.llm_transport is not None,
        )


@dataclass(frozen=True)
class Services:
    operator: OperatorService
    campaign_enroller: CampaignEnroller
    campaign_scheduler: CampaignScheduler
    campaign_executor: CampaignExecutor
    follow_up_scheduler: FollowUpScheduler
    follow_up_executor: FollowUpExecutor
    dispatch: DispatchService | None
    inbound: InboundService | None


def build_services(db: Database, clock: Clock, config: RuntimeConfig, adapters: Adapters) -> Services:
    campaign_config = config.campaign_config()
    follow_up_config = config.follow_up_config()
    dispatch = None
    if adapters.email_transport is not None:
        dispatch = DispatchService(db, clock, config.dispatch_config(), adapters.email_transport, adapters.reconciler)
    inbound = None
    if adapters.llm_transport is not None:
        inbound = InboundService(db, StructuredLLM(adapters.llm_transport, clock), clock, config.inbound_config())
    return Services(
        operator=OperatorService(db, clock, config.operator_config(), adapters.authenticator),
        campaign_enroller=CampaignEnroller(db, clock),
        campaign_scheduler=CampaignScheduler(db, clock, campaign_config),
        campaign_executor=CampaignExecutor(db, clock, campaign_config),
        follow_up_scheduler=FollowUpScheduler(db, clock, follow_up_config),
        follow_up_executor=FollowUpExecutor(db, clock, follow_up_config),
        dispatch=dispatch,
        inbound=inbound,
    )
