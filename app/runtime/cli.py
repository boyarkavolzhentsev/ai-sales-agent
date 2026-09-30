"""One-shot local commands: ``python -m app.runtime <command>``.

  init              start once: create/migrate the database (the only command that may
                    create one), inspect recovery
  health            read-only: open the existing database read-only and report its schema;
                    never creates, migrates or writes anything
  tick              one runtime tick (reconciliation, campaign, follow-up)
                    [--dispatch-approved: also dispatch operator-approved messages]
  reconcile         one Stage 8 reconciliation pass
  campaign-tick     one campaign pass (drafts only)
  follow-up-tick    one conversation follow-up pass (drafts only)
  dispatch-tick     one pass over operator-approved messages (Stage 8 revalidates each)

Every command runs once and exits; there is no loop or daemon. Configuration comes from
``SALES_AGENT_*`` environment variables. Output is JSON with IDs, counts and codes only;
the configuration and any secret are never printed. The CLI uses the offline adapters:
without a configured provider, dispatch and reconciliation report SKIPPED rather than
fabricating provider outcomes. Nothing is ever approved here.

Exit codes: 0 ok, 1 unexpected error, 2 invalid configuration or usage, 3 startup or
health failure, 4 a phase reported errors.
"""

import argparse
import json
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from pydantic import BaseModel

from app.persistence import MEMORY, SystemClock
from app.persistence.migrations import current_version, latest_version
from app.runtime.application import SalesAgentRuntime
from app.runtime.env import load_config
from app.runtime.errors import ConfigError, StartupError
from app.runtime.results import PhaseStatus, RuntimeTickResult

OK, UNEXPECTED, INVALID_CONFIG, UNHEALTHY, PHASE_ERRORS = 0, 1, 2, 3, 4
TICKS = ("tick", "reconcile", "campaign-tick", "follow-up-tick", "dispatch-tick")


def main(argv: Sequence[str], environ: Mapping[str, str], out: TextIO) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.runtime", description="AI sales agent one-shot runtime commands")
    parser.add_argument("command", choices=("init", "health", *TICKS))
    parser.add_argument("--dispatch-approved", action="store_true", help="tick only: also dispatch approved messages")
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as exc:
        return INVALID_CONFIG if exc.code else OK
    clock = SystemClock()
    try:
        config = load_config(environ, now=clock.now())
    except ConfigError as exc:
        _emit(out, {"error": "INVALID_CONFIGURATION", "problems": list(exc.problems)})
        return INVALID_CONFIG
    if args.command == "health":
        return _health(config.database_path, out)
    if args.command != "init" and (config.database_path == MEMORY or not Path(config.database_path).is_file()):
        # Only ``init`` may create a database: a mistyped path must not silently start empty.
        _emit(out, {"error": "DATABASE_MISSING", "hint": "run 'init' first"})
        return UNHEALTHY

    runtime = SalesAgentRuntime(config, clock=clock)
    try:
        startup = runtime.start()
    except StartupError as exc:
        _emit(out, {"error": "STARTUP_FAILED", "code": exc.code, "health": runtime.health().model_dump(mode="json")})
        return UNHEALTHY
    try:
        if args.command == "init":
            _emit(out, {"startup": startup.model_dump(mode="json"), "health": runtime.health().model_dump(mode="json")})
            return OK
        result: BaseModel = {
            "tick": lambda: runtime.tick(dispatch_approved=args.dispatch_approved),
            "reconcile": runtime.reconcile,
            "campaign-tick": runtime.campaign_tick,
            "follow-up-tick": runtime.follow_up_tick,
            "dispatch-tick": runtime.dispatch_tick,
        }[args.command]()
        _emit(out, result.model_dump(mode="json"))
        return PHASE_ERRORS if _has_errors(result) else OK
    finally:
        runtime.stop()


def _health(database_path: str, out: TextIO) -> int:
    """Read-only: the database must already exist; it is opened with mode=ro."""
    report: dict[str, object] = {"latest_schema_version": latest_version()}
    if database_path == MEMORY or not Path(database_path).is_file():
        _emit(out, report | {"database_ok": False, "problem": "DATABASE_MISSING"})
        return UNHEALTHY
    try:
        connection = sqlite3.connect(f"{Path(database_path).resolve().as_uri()}?mode=ro", uri=True)
        try:
            version = current_version(connection)
        finally:
            connection.close()
    except sqlite3.Error as exc:
        _emit(out, report | {"database_ok": False, "problem": f"DATABASE_UNAVAILABLE:{type(exc).__name__}"})
        return UNHEALTHY
    current = version == latest_version()
    _emit(out, report | {"database_ok": True, "schema_version": version, "schema_current": current})
    return OK if current else UNHEALTHY


def _has_errors(result: BaseModel) -> bool:
    if isinstance(result, RuntimeTickResult):
        return not result.ok
    return getattr(result, "status", None) is PhaseStatus.ERROR


def _emit(out: TextIO, payload: dict[str, object]) -> None:
    out.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
