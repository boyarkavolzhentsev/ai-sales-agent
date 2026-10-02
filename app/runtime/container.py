"""Composition root: the adapters the runtime plugs in, and the services built from them.

Adapters are the existing boundary Protocols; a future real provider implements them:
- ``EmailTransport`` and ``DispatchReconciler`` (Stage 8),
- ``LLMTransport`` (Stage 5, behind ``StructuredLLM``),
- ``OperatorAuthenticator`` (Stage 7),
- ``QualificationExtractor`` and ``SalesAdvisor`` (Stage 12 AI contracts) and
  ``CommercialExtractor`` (Stage 13): LLM-backed (app.ai) when an LLM provider is selected
  (Stage 18), injected fakes in tests; a ``PriceCatalog`` (default: approved internal
  knowledge facts),
- ``MailboxReader`` (Stage 16 inbound mailbox sync; Gmail),
- ``EmbeddingTransport`` (Stage 19): when configured, Stage 6 retrieves knowledge
  semantically (``SemanticRetriever``) and ``knowledge-index`` embeds approved chunks;
  without it, retrieval stays lexical and nothing is embedded.
The Stage 14 execution coordinator is built over the same subsystem instances and is told
which capabilities exist, so a missing adapter only makes an action non-executable.
The offline default configures none of the provider adapters, so the capabilities that
need them are simply unavailable: no fake ever fabricates a provider outcome in a real
database. Tests pass deterministic fakes explicitly.

Construction performs no I/O and starts nothing: ``build_services`` only wires objects
around one Database and one Clock.
"""

from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

from app.campaign import CampaignEnroller, CampaignExecutor, CampaignScheduler
from app.conversation import FollowUpExecutor, FollowUpScheduler
from app.dispatch import DispatchReconciler, DispatchService, EmailTransport
from app.embeddings import EmbeddingTransport
from app.enrichment import EnrichmentService
from app.inbound import InboundService
from app.integrations import ProviderConnectors, build_provider_adapters
from app.integrations.mailbox import MailboxReader, MailboxSync
from app.knowledge import KnowledgeIndexer, LexicalRetriever, SemanticRetriever
from app.llm import LLMTransport, StructuredLLM
from app.operator import OperatorAuthenticator, OperatorCredential, OperatorService
from app.commercial import CommercialExtractor, CommercialService, PriceCatalog
from app.orchestration import ExecutionCapabilities, OrchestratorConfig, SalesOrchestrator
from app.persistence import Clock, Database, EnrichmentKind, SystemClock
from app.pipeline import PipelineService, QualificationExtractor, SalesAdvisor
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
    qualification_extractor: QualificationExtractor | None = None
    sales_advisor: SalesAdvisor | None = None
    commercial_extractor: CommercialExtractor | None = None
    price_catalog: PriceCatalog | None = None
    mailbox: MailboxReader | None = None
    operator_channel: Any = None  # the Telegram adapters (Stage 17)
    embeddings_transport: EmbeddingTransport | None = None  # semantic retrieval (Stage 19)


def offline_adapters() -> Adapters:
    """Nothing external configured: dispatch, reconciliation and inbound analysis are
    unavailable; campaign and follow-up ticks (which only produce drafts) work."""
    return Adapters()


def configured_adapters(config: RuntimeConfig, base: Adapters | None = None,
                        connectors: ProviderConnectors | None = None, clock: Clock | None = None) -> Adapters:
    """The adapters of the configured providers, through the provider factory, on top of
    ``base`` (injected adapters, e.g. tests; else the offline set). A selected provider's
    adapters always replace the injected ones of its category, and a provider without an
    implementation contributes nothing: no fake is ever substituted. Building Gmail
    adapters reads/refreshes the local token and checks the account (ProviderUnavailableError)."""
    adapters = base or offline_adapters()
    built = build_provider_adapters(config.integrations, config.secrets, connectors)
    if built.email_transport is not None:
        adapters = replace(adapters, email_transport=built.email_transport, reconciler=built.reconciler, mailbox=built.mailbox)
    if built.llm_transport is not None:
        # One selected LLM provider serves every AI contract; injected ones are replaced
        # (never mixed with fakes). Building them makes no request.
        from app.ai import LLMCommercialExtractor, LLMQualificationExtractor, LLMSalesAdvisor

        llm = StructuredLLM(built.llm_transport, clock or SystemClock())
        locale = config.inbound_config().default_locale
        adapters = replace(adapters, llm_transport=built.llm_transport,
                           qualification_extractor=LLMQualificationExtractor(llm, locale=locale),
                           sales_advisor=LLMSalesAdvisor(llm, locale=locale),
                           commercial_extractor=LLMCommercialExtractor(llm, locale=locale))
    if built.embeddings_transport is not None:
        # Independent of the LLM provider; building it makes no request.
        adapters = replace(adapters, embeddings_transport=built.embeddings_transport)
    if built.operator_channel is not None:
        # Telegram credentials are verified by Telegram's authenticator; any other scheme
        # still goes to the configured one (DenyAll in production, injected in tests).
        from app.integrations.telegram.auth import SchemeAuthenticator

        adapters = replace(adapters, operator_channel=built.operator_channel,
                           authenticator=SchemeAuthenticator(built.operator_channel.authenticator, adapters.authenticator))
    return adapters


@dataclass(frozen=True)
class Capabilities:
    dispatch: bool
    reconciliation: bool
    inbound: bool
    email_sync: bool = False
    operator_channel: bool = False
    qualification_extraction: bool = False
    commercial_extraction: bool = False
    sales_advice: bool = False
    semantic_retrieval: bool = False

    @classmethod
    def of(cls, adapters: Adapters) -> "Capabilities":
        # Reconciliation resolves attempts made by a transport; both belong to one provider.
        return cls(
            dispatch=adapters.email_transport is not None,
            reconciliation=adapters.email_transport is not None and adapters.reconciler is not None,
            inbound=adapters.llm_transport is not None,
            email_sync=adapters.mailbox is not None,
            operator_channel=adapters.operator_channel is not None,
            qualification_extraction=adapters.qualification_extractor is not None,
            commercial_extraction=adapters.commercial_extractor is not None,
            sales_advice=adapters.sales_advisor is not None,
            semantic_retrieval=adapters.embeddings_transport is not None,
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
    pipeline: PipelineService
    commercial: CommercialService
    orchestrator: SalesOrchestrator
    mailbox_sync: MailboxSync | None = None
    operator_channel: Any = None  # OperatorChannelSync, composed by the runtime (Stage 17)
    enrichment: EnrichmentService | None = None  # durable Stage 12/13 extraction jobs (Stage 18)
    knowledge_indexer: KnowledgeIndexer | None = None  # embeds approved chunks (Stage 19)


def build_services(db: Database, clock: Clock, config: RuntimeConfig, adapters: Adapters) -> Services:
    campaign_config = config.campaign_config()
    follow_up_config = config.follow_up_config()
    dispatch = None
    if adapters.email_transport is not None:
        dispatch = DispatchService(db, clock, config.dispatch_config(), adapters.email_transport, adapters.reconciler)
    embeddings = adapters.embeddings_transport
    settings = config.integrations.embeddings
    knowledge = (SemanticRetriever(db, embeddings, min_similarity=settings.min_similarity) if embeddings is not None
                 else LexicalRetriever(db))
    indexer = None
    if embeddings is not None:
        # A claim outlives one request (timeout, plus at most one retry) with margin.
        indexer = KnowledgeIndexer(db, clock, embeddings, lease=timedelta(seconds=max(60, 3 * settings.timeout_seconds)))
    inbound = None
    if adapters.llm_transport is not None:
        inbound = InboundService(db, StructuredLLM(adapters.llm_transport, clock), clock, config.inbound_config(),
                                 knowledge=knowledge)
    campaign_scheduler = CampaignScheduler(db, clock, campaign_config)
    campaign_executor = CampaignExecutor(db, clock, campaign_config)
    follow_up_scheduler = FollowUpScheduler(db, clock, follow_up_config)
    follow_up_executor = FollowUpExecutor(db, clock, follow_up_config)
    capabilities = Capabilities.of(adapters)
    orchestrator = SalesOrchestrator(
        db, clock,
        OrchestratorConfig(qualification=config.pipeline.profile, commercial=config.commercial.profile,
                           follow_up=follow_up_config, dispatch=config.dispatch_config(), kill_switch=config.kill_switch,
                           worker_id=config.worker.worker_id),
        ExecutionCapabilities(dispatch=capabilities.dispatch, reconciliation=capabilities.reconciliation, llm=capabilities.inbound),
        campaign_scheduler=campaign_scheduler, campaign_executor=campaign_executor, follow_up_scheduler=follow_up_scheduler,
        follow_up_executor=follow_up_executor, dispatch=dispatch,
    )
    pipeline = PipelineService(db, clock, config.pipeline, extractor=adapters.qualification_extractor,
                               advisor=adapters.sales_advisor)
    commercial = CommercialService(db, clock, config.commercial, extractor=adapters.commercial_extractor,
                                   catalog=adapters.price_catalog)
    enrichment = None
    if inbound is not None:
        kinds = frozenset({kind for kind, adapter in ((EnrichmentKind.QUALIFICATION_EXTRACTION, adapters.qualification_extractor),
                                                       (EnrichmentKind.COMMERCIAL_EXTRACTION, adapters.commercial_extractor))
                           if adapter is not None})
        enrichment = EnrichmentService(db, clock, pipeline=pipeline, commercial=commercial, load_result=inbound.final_result,
                                       kinds=kinds, worker_id=config.worker.worker_id)
    return Services(
        operator=OperatorService(db, clock, config.operator_config(), adapters.authenticator, config.pipeline,
                                 config.commercial, adapters.price_catalog),
        campaign_enroller=CampaignEnroller(db, clock),
        campaign_scheduler=campaign_scheduler,
        campaign_executor=campaign_executor,
        follow_up_scheduler=follow_up_scheduler,
        follow_up_executor=follow_up_executor,
        dispatch=dispatch,
        inbound=inbound,
        pipeline=pipeline,
        commercial=commercial,
        orchestrator=orchestrator,
        mailbox_sync=MailboxSync(db, clock, adapters.mailbox) if adapters.mailbox is not None else None,
        enrichment=enrichment,
        knowledge_indexer=indexer,
    )
