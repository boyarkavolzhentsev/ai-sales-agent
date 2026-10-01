"""Stable integration configuration codes. A problem names the variable, never its value."""

from enum import StrEnum

from app.core.models.base import CoreModel
from app.integrations.providers import ProviderCategory


class IntegrationCode(StrEnum):
    UNKNOWN_PROVIDER = "UNKNOWN_PROVIDER"  # not one of the category's provider IDs
    PROVIDER_NOT_SELECTED = "PROVIDER_NOT_SELECTED"  # a setting/secret for a provider that is not selected
    PROVIDER_NOT_IMPLEMENTED = "PROVIDER_NOT_IMPLEMENTED"  # valid configuration, no adapter exists yet
    MISSING_SETTING = "MISSING_SETTING"
    MISSING_SECRET = "MISSING_SECRET"
    INVALID_PROVIDER_CONFIG = "INVALID_PROVIDER_CONFIG"  # malformed or inconsistent value
    SECRET_SOURCE_INVALID = "SECRET_SOURCE_INVALID"  # contradictory sources, or a file where secrets must not be
    CREDENTIAL_FILE_MISSING = "CREDENTIAL_FILE_MISSING"
    CREDENTIAL_FILE_UNREADABLE = "CREDENTIAL_FILE_UNREADABLE"
    CREDENTIAL_FILE_PERMISSIONS_BROAD = "CREDENTIAL_FILE_PERMISSIONS_BROAD"  # a warning, POSIX only


class IntegrationProblem(CoreModel):
    category: ProviderCategory
    code: IntegrationCode
    variable: str  # the SALES_AGENT_* name (without the prefix)

    def render(self) -> str:
        return f"SALES_AGENT_{self.variable}: {self.code.value}"
