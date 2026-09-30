from enum import StrEnum


class LeadOrigin(StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class LeadStage(StrEnum):
    """Commercial pipeline position. Contactability (DNC) is deliberately not a stage; the
    outcome of a CLOSED lead is its CloseReason (WON, LOST, ...).

    Automatic band (set from provider or customer facts): NEW, CONTACTED, ENGAGED,
    INTERESTED, MEETING_REQUESTED, QUALIFYING. Operator band (human commercial judgement,
    Stage 12): QUALIFIED, OPPORTUNITY, NEGOTIATION. See ``app.pipeline.policy``.
    """

    NEW = "NEW"
    CONTACTED = "CONTACTED"
    ENGAGED = "ENGAGED"
    INTERESTED = "INTERESTED"
    MEETING_REQUESTED = "MEETING_REQUESTED"
    QUALIFYING = "QUALIFYING"  # qualification facts are being collected
    QUALIFIED = "QUALIFIED"  # an operator approved the qualification
    OPPORTUNITY = "OPPORTUNITY"  # an operator opened an opportunity
    NEGOTIATION = "NEGOTIATION"  # an operator started commercial negotiation
    CLOSED = "CLOSED"


class LeadStatus(StrEnum):
    """Who drives the lead; orthogonal to stage."""

    AUTOMATED = "AUTOMATED"
    ON_HOLD = "ON_HOLD"
    OPERATOR_OWNED = "OPERATOR_OWNED"


class CloseReason(StrEnum):
    NOT_INTERESTED = "NOT_INTERESTED"
    NO_RESPONSE = "NO_RESPONSE"
    UNSUBSCRIBED = "UNSUBSCRIBED"
    INVALID_CONTACT = "INVALID_CONTACT"
    DUPLICATE = "DUPLICATE"
    WON = "WON"
    LOST = "LOST"
    DISQUALIFIED = "DISQUALIFIED"  # an operator decided the lead does not qualify


class LeadIntent(StrEnum):
    INFO_REQUEST = "INFO_REQUEST"
    PRICING_REQUEST = "PRICING_REQUEST"
    MEETING_REQUEST = "MEETING_REQUEST"
    POSITIVE_INTEREST = "POSITIVE_INTEREST"
    OBJECTION = "OBJECTION"
    NEGOTIATION = "NEGOTIATION"
    NOT_INTERESTED = "NOT_INTERESTED"
    UNSUBSCRIBE = "UNSUBSCRIBE"
    REFERRAL = "REFERRAL"
    OUT_OF_OFFICE = "OUT_OF_OFFICE"
    LEGAL_OR_COMPLAINT = "LEGAL_OR_COMPLAINT"
    NON_SALES = "NON_SALES"
    SPAM_OR_IRRELEVANT = "SPAM_OR_IRRELEVANT"
    UNCLEAR = "UNCLEAR"
