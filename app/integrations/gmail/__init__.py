"""Gmail provider (Stage 16): Stage 8 ``EmailTransport`` and ``DispatchReconciler``, a
``MailboxReader`` for inbound sync, and installed-app OAuth for one user mailbox.

Infrastructure only: it never decides whether something may be sent, what a message
means, or anything about leads, campaigns, proposals or do-not-contact. Business packages
never import it; the provider registry loads it only when ``EMAIL_PROVIDER=GMAIL``.

This package module imports nothing on purpose: ``gmail.tokens`` (standard library only)
serves provider-status without loading a Google library; ``gmail.provider`` is imported
only when Gmail adapters are actually built.
"""
