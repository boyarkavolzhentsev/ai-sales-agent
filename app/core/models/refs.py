from app.core.enums import ActorType, RefKind
from app.core.models.base import CoreModel
from app.core.models.types import EntityId, NonEmptyStr


class EntityRef(CoreModel):
    """Typed reference to another core entity."""

    kind: RefKind
    id: EntityId


class Actor(CoreModel):
    """Who performed an action: a system component, an LLM model, an operator or an external party."""

    type: ActorType
    id: NonEmptyStr
