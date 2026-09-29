from enum import StrEnum


class ContactDepartment(StrEnum):
    PARTNERSHIPS = "PARTNERSHIPS"
    PROCUREMENT = "PROCUREMENT"
    BUSINESS_DEVELOPMENT = "BUSINESS_DEVELOPMENT"
    OPERATIONS = "OPERATIONS"
    SALES = "SALES"
    GENERAL = "GENERAL"
    OTHER = "OTHER"


class ContactType(StrEnum):
    ROLE_ADDRESS = "ROLE_ADDRESS"
    NAMED_BUSINESS = "NAMED_BUSINESS"


class ContactSource(StrEnum):
    """Where a prospect company or contact record came from (provenance)."""

    IMPORT = "IMPORT"
    INBOUND = "INBOUND"
    OPERATOR = "OPERATOR"


class EmailValidity(StrEnum):
    UNKNOWN = "UNKNOWN"
    VALID = "VALID"
    BOUNCED = "BOUNCED"


class IcpFit(StrEnum):
    FIT = "FIT"
    NOT_FIT = "NOT_FIT"
    UNKNOWN = "UNKNOWN"
