"""Gmail implementation of Stage 8's ``DispatchReconciler`` (read-only, never sends).

Evidence: the attempt's own Message-ID (Stage 8 generates one per attempt and the
transport sends it). Gmail is searched with ``rfc822msgid:``; a found message counts only
if its Message-ID header is exactly ours and Gmail files it as SENT by this account. That
is positive acceptance (ACCEPTED, with Gmail's message id).

Gmail cannot prove a rejection: there is no record of failed sends. So the absence of a
match is NOT_FOUND (the attempt stays unresolved, never "rejected"), and any read error is
UNKNOWN. Subjects and bodies are never compared.
"""

from app.dispatch.transport import ReconciliationFinding, ReconciliationResult
from app.integrations.gmail.client import GmailApi
from app.integrations.gmail.errors import GmailError

MAX_CANDIDATES = 5


def _normalized(message_id: str) -> str:
    return message_id.strip().strip("<>").strip().lower()


class GmailReconciler:
    def __init__(self, api: GmailApi) -> None:
        self._api = api

    def lookup(self, request_id: str, rfc_message_id: str) -> ReconciliationResult:
        wanted = _normalized(rfc_message_id)
        try:
            ids = self._api.find_message_ids(f"rfc822msgid:{wanted}", max_results=MAX_CANDIDATES, include_spam_trash=True)
            for message_id in ids:
                found = self._api.get_metadata(message_id, ("Message-ID",))
                if _normalized(found.headers.get("message-id", "")) == wanted and "SENT" in found.label_ids:
                    return ReconciliationResult(finding=ReconciliationFinding.ACCEPTED, reason_code="GMAIL_SENT_MESSAGE_FOUND",
                                                provider_message_id=found.message_id)
        except GmailError as exc:
            return ReconciliationResult(finding=ReconciliationFinding.UNKNOWN, reason_code=f"GMAIL_{exc.code.value}")
        return ReconciliationResult(finding=ReconciliationFinding.NOT_FOUND, reason_code="GMAIL_NOT_FOUND")
