"""Non-secret provider configuration (immutable). Secrets live in ``secrets.py`` only.

Field types are checked here; which fields a selected provider needs, and that an
unselected provider carries no settings, is decided by ``evaluate`` (one place for the
cross-field rules, with stable codes)."""

import hashlib
import json
from pathlib import Path
from typing import Annotated

from pydantic import Field

from app.core.models.base import CoreModel
from app.core.models.types import EmailAddress, NonEmptyStr
from app.integrations.providers import (
    EmailProviderId,
    EmbeddingsProviderId,
    KnowledgeProviderId,
    LLMProviderId,
    OperatorProviderId,
)

FINGERPRINT_PREFIX = "cfg-"


class EmailProviderConfig(CoreModel):
    provider: EmailProviderId = EmailProviderId.NONE
    # The mailbox the provider account serves; must be one of the runtime's mailboxes.
    address: EmailAddress | None = None
    # Local OAuth client file (contents are secret, never read in Stage 15) and the local
    # file a future OAuth flow stores its token in.
    credentials_file: Path | None = None
    token_file: Path | None = None
    poll_interval_seconds: Annotated[int, Field(ge=15, le=3600)] = 60
    # Bound of every Gmail API call (connect and read).
    timeout_seconds: Annotated[int, Field(ge=1, le=120)] = 30


class LLMProviderConfig(CoreModel):
    provider: LLMProviderId = LLMProviderId.NONE
    model: NonEmptyStr | None = None
    timeout_seconds: Annotated[int, Field(ge=1, le=300)] = 30


class OperatorChannelConfig(CoreModel):
    provider: OperatorProviderId = OperatorProviderId.NONE
    # Telegram chats allowed to issue operator commands (negative IDs are group chats).
    operator_chat_ids: tuple[int, ...] = ()


class KnowledgeProviderConfig(CoreModel):
    provider: KnowledgeProviderId = KnowledgeProviderId.LOCAL
    # The deployment's approved knowledge directory (ingested into the local index).
    directory: Path | None = None


class EmbeddingsProviderConfig(CoreModel):
    provider: EmbeddingsProviderId = EmbeddingsProviderId.NONE


class IntegrationConfig(CoreModel):
    """Every provider selection and its non-secret settings for one deployment."""

    email: EmailProviderConfig = EmailProviderConfig()
    llm: LLMProviderConfig = LLMProviderConfig()
    operator: OperatorChannelConfig = OperatorChannelConfig()
    knowledge: KnowledgeProviderConfig = KnowledgeProviderConfig()
    embeddings: EmbeddingsProviderConfig = EmbeddingsProviderConfig()

    def fingerprint(self) -> str:
        """Identifies the deployment's provider configuration. Built from non-secret
        settings only: rotating a key or token does not change it, and nothing secret can
        be recovered from it (secrets are never part of this model)."""
        text = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return FINGERPRINT_PREFIX + hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
