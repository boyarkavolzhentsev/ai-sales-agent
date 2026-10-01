"""Gmail implementation of the provider-neutral ``MailboxReader`` (read-only).

Positions are Gmail history ids. ``changes`` reads ``users.history.list`` (messageAdded
only) after the cursor, at most ``limit`` messages, whole history records only, so the
returned position is exactly "everything up to here was returned". Labels already in the
history event filter drafts, spam, trash, chat and our own sent mail without fetching
them. An expired start id becomes ``MailboxCursorExpired`` (no silent reset). Nothing
here labels, archives, marks or deletes mail.
"""

from app.integrations.gmail.client import GmailApi
from app.integrations.gmail.errors import GmailCode, GmailError
from app.integrations.gmail.mime import PROVIDER, normalize_inbound, skip_label
from app.integrations.mailbox import (
    FetchedMessage,
    MailboxChange,
    MailboxChanges,
    MailboxCursorExpired,
    MailboxReadError,
)

PAGE_SIZE = 100
MAX_PAGES = 20  # bounds one read; the next pass continues from the returned position


class GmailMailboxReader:
    provider = PROVIDER

    def __init__(self, api: GmailApi, *, address: str) -> None:
        self._api = api
        self.address = address

    def start_position(self) -> str:
        try:
            return self._api.profile().history_id
        except GmailError as exc:
            raise MailboxReadError(f"GMAIL_{exc.code.value}") from None

    def changes(self, position: str, limit: int) -> MailboxChanges:
        found: list[MailboxChange] = []
        next_position = position
        token: str | None = None
        for _ in range(MAX_PAGES):
            try:
                page = self._api.history(position, page_token=token, max_results=min(PAGE_SIZE, max(limit, 1)))
            except GmailError as exc:
                if exc.code is GmailCode.HISTORY_EXPIRED:
                    raise MailboxCursorExpired() from None
                raise MailboxReadError(f"GMAIL_{exc.code.value}") from None
            for record in page.records:
                if found and len(found) + len(record.added) > limit:
                    return MailboxChanges(changes=tuple(found), next_position=next_position)
                found += [MailboxChange(message_ref=message_id, skip_reason=skip_label(labels))
                          for message_id, labels in record.added]
                next_position = record.history_id
            if page.next_page_token is None:
                # Every record up to the mailbox's current history id was returned.
                return MailboxChanges(changes=tuple(found), next_position=page.history_id)
            token = page.next_page_token
        return MailboxChanges(changes=tuple(found), next_position=next_position)

    def fetch(self, message_ref: str) -> FetchedMessage:
        try:
            raw = self._api.get_raw(message_ref)
        except GmailError as exc:
            if exc.code is GmailCode.NOT_FOUND:
                return FetchedMessage(skip_reason="MESSAGE_GONE")  # deleted before we read it
            raise MailboxReadError(f"GMAIL_{exc.code.value}") from None
        return normalize_inbound(raw, mailbox=self.address)
