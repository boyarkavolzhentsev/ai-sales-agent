class InboundProcessingError(Exception):
    """The inbound message could not be processed and not even an escalation could be
    persisted. Nothing was sent and no fabricated outcome was stored."""


class IdempotencyCollisionError(InboundProcessingError):
    """The same provider message identity arrived with different content. Never merged
    silently: the stored observation stays authoritative and the collision is surfaced."""
