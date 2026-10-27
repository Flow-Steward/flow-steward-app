"""Shared opaque idempotency header validation for workflow runs and approvals."""

from core.application.management.idempotency_keys import validate_idempotency_key

__all__ = ["validate_idempotency_key"]
