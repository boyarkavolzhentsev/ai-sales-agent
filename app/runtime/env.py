"""Building RuntimeConfig from an environment mapping (``SALES_AGENT_*`` variables).

Rules:
- Every value that permits or limits sending is required and explicit: no default for
  the sending window, the limits or the kill switch. A missing or malformed value fails.
- Booleans accept exactly "true" or "false"; integers must be plain non-negative digits.
- The time zone must be a valid IANA name (the existing policy validators check it).
- Error messages name the variable and the problem, never the value (values may be
  secrets). Unknown ``SALES_AGENT_*`` variables are rejected, so a typo cannot silently
  leave a limit at an unintended value.
- Provider variables (``app.integrations``) are optional: a provider's settings and
  secrets are required only when it is selected, and rejected when it is not. A provider
  configuration that is INVALID fails loading; NOT_IMPLEMENTED does not (it only leaves the
  capability unavailable; PRODUCTION mode refuses it at startup).
- The environment is the only source (no ``.env`` file is read, no config file, no CLI
  override): explicit environment values over the documented defaults.
"""

from collections.abc import Callable, Mapping
from datetime import datetime, time, timedelta
from pathlib import Path

from pydantic import ValidationError

from app.integrations import INTEGRATION_VARIABLES, IntegrationStatus, evaluate, parse_integrations
from app.llm import SenderIdentity
from app.persistence import MEMORY
from app.policy import GlobalDailyLimits, KillSwitchState, LimitPolicy, SendingWindow, Weekday
from app.runtime.config import RuntimeConfig, RuntimeMode, WorkerSettings
from app.runtime.errors import ConfigError

PREFIX = "SALES_AGENT_"
REQUIRED = (
    "MODE", "DATABASE_PATH", "SENDER_NAME", "COMPANY_NAME", "MAILBOXES", "TIMEZONE", "WINDOW_DAYS",
    "WINDOW_START", "WINDOW_END", "MAX_SENDS_PER_DAY", "MAX_NEW_CONTACTS_PER_DAY", "MAX_FOLLOW_UPS_PER_DAY",
    "MAX_FOLLOW_UPS_PER_CONTACT", "MIN_FOLLOW_UP_INTERVAL_HOURS", "KILL_SWITCH", "OPERATOR_IDS",
)
OPTIONAL = ("APP_ID", "CODE_VERSION", "KILL_SWITCH_REASON", "WORKER_ID", "BATCH_LIMIT", "POLICY_VERSION",
            *sorted(INTEGRATION_VARIABLES))
_DAYS = {"MON": Weekday.MONDAY, "TUE": Weekday.TUESDAY, "WED": Weekday.WEDNESDAY, "THU": Weekday.THURSDAY,
         "FRI": Weekday.FRIDAY, "SAT": Weekday.SATURDAY, "SUN": Weekday.SUNDAY}


def load_config(environ: Mapping[str, str], *, now: datetime) -> RuntimeConfig:
    """``now`` stamps the kill-switch state read from the environment."""
    values = {key[len(PREFIX):]: value for key, value in environ.items() if key.startswith(PREFIX)}
    errors: list[str] = []
    unknown = sorted(set(values) - set(REQUIRED) - set(OPTIONAL))
    errors += [f"{PREFIX}{name}: unknown variable" for name in unknown]
    errors += [f"{PREFIX}{name}: required" for name in REQUIRED if not values.get(name, "").strip()]
    if errors:
        raise ConfigError(tuple(errors))
    parser = _Parser(values)
    mode = parser.mode()
    database_path = parser.database_path(mode)
    kill_switch_on = parser.boolean("KILL_SWITCH")
    reason = values.get("KILL_SWITCH_REASON", "").strip() or None
    if kill_switch_on and reason is None:
        parser.errors.append(f"{PREFIX}KILL_SWITCH_REASON: required when the kill switch is on")
    timezone = values["TIMEZONE"].strip()
    limits = parser.build("limits", lambda: LimitPolicy(
        policy_version=values.get("POLICY_VERSION", "env-1").strip() or "env-1", timezone=timezone,
        global_limits=GlobalDailyLimits(
            max_sends_per_day=parser.integer("MAX_SENDS_PER_DAY"),
            max_new_contacts_per_day=parser.integer("MAX_NEW_CONTACTS_PER_DAY"),
            max_follow_ups_per_day=parser.integer("MAX_FOLLOW_UPS_PER_DAY"),
        ),
        max_follow_ups_per_contact=parser.integer("MAX_FOLLOW_UPS_PER_CONTACT"),
        min_interval_between_follow_ups=timedelta(hours=parser.integer("MIN_FOLLOW_UP_INTERVAL_HOURS", minimum=1)),
    ))
    window = parser.build("sending window", lambda: SendingWindow(
        timezone=timezone, working_days=parser.days("WINDOW_DAYS"),
        start_local_time=parser.clock_time("WINDOW_START"), end_local_time=parser.clock_time("WINDOW_END"),
    ))
    worker = parser.build("worker", lambda: WorkerSettings(
        worker_id=values.get("WORKER_ID", "").strip() or "local-worker",
        batch_limit=parser.integer("BATCH_LIMIT", minimum=1) if values.get("BATCH_LIMIT", "").strip() else 25,
    ))
    parsed = parse_integrations(values)
    integrations = evaluate(parsed.config, parsed.secrets, mailboxes=_items(values["MAILBOXES"]),
                            operator_ids=_items(values["OPERATOR_IDS"]), extra=parsed.problems)
    parser.errors += [problem for status in integrations.providers for problem in status.problems]
    if parser.errors or limits is None or window is None or worker is None:
        raise ConfigError(tuple(parser.errors))
    fields: dict[str, object] = {
        "mode": mode, "database_path": database_path, "limits": limits, "window": window, "worker": worker,
        "sender": {"sender_name": values["SENDER_NAME"].strip(), "company_name": values["COMPANY_NAME"].strip()},
        "mailboxes": _items(values["MAILBOXES"]), "operator_ids": _items(values["OPERATOR_IDS"]),
        "kill_switch": {"enabled": kill_switch_on, "reason": reason, "changed_at": now, "changed_by": "environment"},
        "integrations": parsed.config, "secrets": parsed.secrets,
    }
    for name in ("APP_ID", "CODE_VERSION"):
        if values.get(name, "").strip():
            fields[name.lower()] = values[name].strip()
    try:
        return RuntimeConfig.model_validate(fields)
    except ValidationError as exc:
        raise ConfigError(_describe(exc)) from None


class _Parser:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.errors: list[str] = []

    def fail(self, name: str, problem: str) -> None:
        self.errors.append(f"{PREFIX}{name}: {problem}")

    def mode(self) -> RuntimeMode:
        raw = self.values["MODE"].strip().upper()
        if raw not in RuntimeMode.__members__:
            self.fail("MODE", "must be 'local', 'test' or 'production'")
            return RuntimeMode.LOCAL
        return RuntimeMode(raw)

    def database_path(self, mode: RuntimeMode) -> str:
        raw = self.values["DATABASE_PATH"].strip()
        if raw == MEMORY:
            if mode is not RuntimeMode.TEST:
                self.fail("DATABASE_PATH", "an in-memory database is only allowed in test mode")
            return raw
        path = Path(raw)
        if path.is_dir():
            self.fail("DATABASE_PATH", "is a directory, not a database file")
        elif not path.parent.is_dir():
            self.fail("DATABASE_PATH", "its parent directory does not exist")
        return raw

    def boolean(self, name: str) -> bool:
        raw = self.values.get(name, "").strip().lower()
        if raw not in ("true", "false"):
            self.fail(name, "must be exactly 'true' or 'false'")
            return True  # never silently permissive: an invalid kill switch reads as on
        return raw == "true"

    def integer(self, name: str, *, minimum: int = 0) -> int:
        raw = self.values.get(name, "").strip()
        if not raw.isdigit() or int(raw) < minimum:
            self.fail(name, f"must be a whole number >= {minimum}")
            return minimum
        return int(raw)

    def days(self, name: str) -> tuple[Weekday, ...]:
        names = [item.upper() for item in _items(self.values[name])]
        bad = [n for n in names if n not in _DAYS]
        if bad or not names:
            self.fail(name, "must be a comma-separated list of MON,TUE,WED,THU,FRI,SAT,SUN")
            return (Weekday.MONDAY,)
        return tuple(_DAYS[n] for n in dict.fromkeys(names))

    def clock_time(self, name: str) -> time:
        raw = self.values[name].strip()
        try:
            hours, minutes = raw.split(":")
            return time(int(hours), int(minutes))
        except ValueError:
            self.fail(name, "must be HH:MM")
            return time(0, 0)

    def build[T](self, what: str, factory: Callable[[], T]) -> T | None:
        try:
            return factory()
        except ValidationError as exc:
            self.errors += [f"{what}: {message}" for message in _describe(exc)]
            return None


def _items(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _describe(exc: ValidationError) -> tuple[str, ...]:
    """Location and message only: pydantic's own text includes the input value."""
    return tuple(f"{'.'.join(str(p) for p in error['loc']) or 'config'}: {error['msg']}" for error in exc.errors())


def inspect_integrations(environ: Mapping[str, str]) -> IntegrationStatus:
    """The provider configuration health of an environment, even when the rest of the
    configuration is incomplete (for ``provider-status``). Sanitized; no I/O beyond local
    file metadata."""
    values = {key[len(PREFIX):]: value for key, value in environ.items() if key.startswith(PREFIX)}
    parsed = parse_integrations(values)
    return evaluate(parsed.config, parsed.secrets, mailboxes=_items(values.get("MAILBOXES", "")),
                    operator_ids=_items(values.get("OPERATOR_IDS", "")), extra=parsed.problems)
