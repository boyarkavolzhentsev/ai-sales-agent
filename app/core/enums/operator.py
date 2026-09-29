from enum import StrEnum


class OperatorCommandKind(StrEnum):
    READ = "READ"
    MUTATE = "MUTATE"


class OperatorResponseStatus(StrEnum):
    OK = "OK"
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
    REJECTED = "REJECTED"
    UNAUTHORIZED = "UNAUTHORIZED"
    INVALID = "INVALID"
    ERROR = "ERROR"
