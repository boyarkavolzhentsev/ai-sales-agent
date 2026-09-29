from enum import StrEnum


class CampaignStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ENDED = "ENDED"


class CampaignReviewMode(StrEnum):
    """Operator review mode for outbound messages.

    Only EACH is supported behaviourally in V1. FIRST_N and NONE are reserved
    contract values: they validate, but no V1 behaviour exists for them.
    """

    EACH = "EACH"
    FIRST_N = "FIRST_N"
    NONE = "NONE"


SUPPORTED_REVIEW_MODES_V1: frozenset[CampaignReviewMode] = frozenset({CampaignReviewMode.EACH})


def is_review_mode_supported_v1(mode: CampaignReviewMode) -> bool:
    return mode in SUPPORTED_REVIEW_MODES_V1
