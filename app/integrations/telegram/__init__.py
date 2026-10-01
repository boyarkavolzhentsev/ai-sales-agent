"""Telegram operator channel (Stage 17): the human operator interface only.

Telegram never holds business truth: every action goes through the existing Stage 7
operator commands (authorization, idempotency, versions, audit), and review cards come
from Stage 14's operator queue. Business packages never import this package; the
provider registry loads it only when ``OPERATOR_PROVIDER=TELEGRAM``. This package module
imports nothing so that selecting another provider never loads it.
"""
