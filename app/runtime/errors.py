"""Typed runtime failures. Messages carry names and codes, never secret values."""


class RuntimeFailure(Exception):
    """Base class for runtime composition and lifecycle failures."""


class ConfigError(RuntimeFailure):
    """The configuration is invalid; ``problems`` name each variable/field and the issue."""

    def __init__(self, problems: tuple[str, ...]) -> None:
        self.problems = problems
        super().__init__("invalid configuration: " + "; ".join(problems))


class StartupError(RuntimeFailure):
    """Startup did not complete; the runtime is FAILED and never READY."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(code + (f": {detail}" if detail else ""))


class RuntimeNotReadyError(RuntimeFailure):
    """An operation was requested while the runtime is not READY (not started, failed,
    or shutting down). No work was started."""


class RuntimeBusyError(RuntimeFailure):
    """A tick was requested while another tick of this runtime is still running."""


class CapabilityUnavailableError(RuntimeFailure):
    """The operation needs an adapter that is not configured (e.g. no email transport)."""
