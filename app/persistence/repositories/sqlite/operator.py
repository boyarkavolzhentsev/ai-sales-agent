from app.core.models import OperatorCommand, OperatorResponse
from app.persistence.repositories.sqlite._rows import load, load_all
from app.persistence.serialization import model_to_json, to_utc_text
from app.persistence.transaction import Transaction


class SqliteOperatorCommandRepository:
    """Operator commands are immutable records; ``telegram_update_id`` is unique (dedupe)."""

    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def add(self, command: OperatorCommand) -> None:
        self._tx.execute(
            "INSERT INTO operator_commands (command_id, telegram_update_id, operator_user_id, "
            "received_at, data) VALUES (?, ?, ?, ?, ?)",
            (
                command.command_id,
                command.telegram_update_id,
                command.operator_user_id,
                to_utc_text(command.received_at),
                model_to_json(command),
            ),
        )

    def get(self, command_id: str) -> OperatorCommand | None:
        row = self._tx.fetch_one(
            "SELECT data FROM operator_commands WHERE command_id = ?", (command_id,)
        )
        return load(OperatorCommand, row)

    def get_by_telegram_update_id(self, telegram_update_id: int) -> OperatorCommand | None:
        row = self._tx.fetch_one(
            "SELECT data FROM operator_commands WHERE telegram_update_id = ?", (telegram_update_id,)
        )
        return load(OperatorCommand, row)


class SqliteOperatorResponseRepository:
    """A command may have several responses (e.g. NEEDS_CONFIRMATION, then OK)."""

    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    def add(self, response: OperatorResponse) -> None:
        self._tx.execute(
            "INSERT INTO operator_responses (command_id, status, created_at, data) "
            "VALUES (?, ?, ?, ?)",
            (
                response.command_id,
                response.status.value,
                to_utc_text(response.created_at),
                model_to_json(response),
            ),
        )

    def list_for_command(self, command_id: str) -> list[OperatorResponse]:
        rows = self._tx.fetch_all(
            "SELECT data FROM operator_responses WHERE command_id = ? "
            "ORDER BY created_at, response_id",
            (command_id,),
        )
        return load_all(OperatorResponse, rows)
