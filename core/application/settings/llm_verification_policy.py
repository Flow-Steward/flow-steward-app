"""Freshness policy for account-scoped chat-model verification snapshots."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

# What a chat model must do before Flow Steward routes work to it: plain chat,
# tool calls, and answers in our structured format. This is the one list; the
# llm_provider contract, discovery labelling and the capability suite all read
# it, so relaxing a requirement starts here (the suite's probes must follow).
REQUIRED_ROUTING_CAPABILITIES: frozenset[str] = frozenset({"chat", "tools", "structured_output"})

MODEL_VERIFICATION_MAX_AGE = timedelta(days=30)
MODEL_VERIFICATION_EXPIRED_ERROR = "verification_expired"


def _as_utc_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return None


def missing_routing_capabilities(declared: Any) -> list[str]:
    """Required capabilities a provider says a model lacks, before verification.

    An empty or absent declaration means the provider reported nothing, which is
    not evidence either way: the model stays a candidate for verification.
    """
    if isinstance(declared, dict):
        declared = list(declared)
    names = {str(item).strip() for item in declared or [] if str(item).strip()}
    if not names:
        return []
    return sorted(REQUIRED_ROUTING_CAPABILITIES - names)


def model_verification_is_fresh(model: dict[str, Any], *, now: datetime | None = None) -> bool:
    """Return whether a passed account-scoped probe remains routable."""
    if str(model.get("verification_status") or "").strip().lower() != "passed":
        return False
    verified_at = _as_utc_datetime(model.get("verified_at"))
    if verified_at is None:
        return False
    current = now or datetime.now(UTC)
    current = current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)
    age = current - verified_at
    return timedelta(0) <= age <= MODEL_VERIFICATION_MAX_AGE
