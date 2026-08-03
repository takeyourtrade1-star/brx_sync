"""Compatibility shim for the durable, snapshot-authoritative webhook path.

The historical implementation in this module applied arithmetic stock deltas
from order payloads.  Webhooks are not an authoritative stock source, so every
caller is deliberately routed to the inbox-ledger processor instead.
"""

from app.services.webhook_ledger_processor import WebhookLedgerProcessor

# Preserve the old import name without preserving the unsafe implementation.
WebhookProcessor = WebhookLedgerProcessor

__all__ = ["WebhookLedgerProcessor", "WebhookProcessor"]
