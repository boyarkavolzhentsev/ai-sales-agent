from enum import StrEnum


class DNCScope(StrEnum):
    EMAIL = "EMAIL"
    DOMAIN = "DOMAIN"


class DNCReason(StrEnum):
    UNSUBSCRIBE_REQUEST = "UNSUBSCRIBE_REQUEST"
    COMPLAINT = "COMPLAINT"
    HARD_BOUNCE = "HARD_BOUNCE"
    OPERATOR = "OPERATOR"
    LEGAL = "LEGAL"
