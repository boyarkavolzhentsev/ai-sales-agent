from pydantic import BaseModel, ConfigDict


class CoreModel(BaseModel):
    """Base for every core contract.

    - frozen: instances are immutable; state changes produce new validated instances.
    - extra="forbid": unknown fields are rejected rather than silently dropped.
    - allow_inf_nan=False: floats must be finite.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        allow_inf_nan=False,
        validate_default=True,
    )
