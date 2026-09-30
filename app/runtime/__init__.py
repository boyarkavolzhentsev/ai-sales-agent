"""Runtime composition: configuration, dependency wiring, lifecycle, recovery inspection,
worker passes and health. Composition only: business rules stay in the subsystems.

Importing this package does nothing: no database is opened, no environment is read, no
worker starts. Everything happens through explicit ``SalesAgentRuntime.start()`` and tick
calls (or the one-shot CLI, ``python -m app.runtime``). Nothing under ``app`` other than
this package imports it.
"""

from app.runtime.application import SalesAgentRuntime
from app.runtime.config import (
    CampaignSettings,
    DispatchSettings,
    FollowUpSettings,
    ProviderSecrets,
    RuntimeConfig,
    RuntimeMode,
    WorkerSettings,
)
from app.runtime.container import Adapters, Capabilities, DenyAllAuthenticator, Services, build_services, offline_adapters
from app.runtime.env import load_config
from app.runtime.errors import (
    CapabilityUnavailableError,
    ConfigError,
    RuntimeBusyError,
    RuntimeFailure,
    RuntimeNotReadyError,
    StartupError,
)
from app.runtime.results import (
    DispatchPhaseResult,
    HealthReport,
    ItemError,
    PhaseStatus,
    ReconciliationResult,
    RecoveryReport,
    RuntimeState,
    RuntimeTickResult,
    StartupReport,
    WorkResult,
)

__all__ = [
    "Adapters",
    "CampaignSettings",
    "Capabilities",
    "CapabilityUnavailableError",
    "ConfigError",
    "DenyAllAuthenticator",
    "DispatchPhaseResult",
    "DispatchSettings",
    "FollowUpSettings",
    "HealthReport",
    "ItemError",
    "PhaseStatus",
    "ProviderSecrets",
    "ReconciliationResult",
    "RecoveryReport",
    "RuntimeBusyError",
    "RuntimeConfig",
    "RuntimeFailure",
    "RuntimeMode",
    "RuntimeNotReadyError",
    "RuntimeState",
    "RuntimeTickResult",
    "SalesAgentRuntime",
    "Services",
    "StartupError",
    "StartupReport",
    "WorkResult",
    "WorkerSettings",
    "build_services",
    "load_config",
    "offline_adapters",
]
