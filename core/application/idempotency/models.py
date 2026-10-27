"""Value objects for shared request idempotency."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from core.application.canonical_json import canonical_json_hash

IdempotencyOutcome = Literal[
    "claimed",
    "replay",
    "in_progress",
    "mismatch",
    "expired",
    "retryable",
]


@dataclass(frozen=True)
class IdempotencyScope:
    """Account-bound operation identity, independent of transport resources."""

    operation: str
    account_id: str
    dimensions: tuple[tuple[str, str], ...] = ()

    def key(self) -> str:
        operation = str(self.operation or "").strip()
        account_id = str(self.account_id or "").strip()
        if not operation or not account_id:
            raise ValueError("Idempotency scope requires operation and account_id")
        normalized_dimensions = sorted(
            (str(name or "").strip(), str(value or "").strip()) for name, value in self.dimensions
        )
        if any(not name or not value for name, value in normalized_dimensions):
            raise ValueError("Idempotency scope dimensions must be non-empty")
        digest = canonical_json_hash(
            {
                "version": 1,
                "operation": operation,
                "account_id": account_id,
                "dimensions": normalized_dimensions,
            }
        )
        return f"request:v1:{digest}"


@dataclass(frozen=True)
class IdempotencyClaim:
    outcome: IdempotencyOutcome
    scope_key: str
    key_digest: str
    request_fingerprint: str
    request_id: str
    job_id: str | None = None
    response: dict[str, Any] | None = None
    result_expires_at: datetime | None = None
    recovered: bool = False

    @property
    def claimed(self) -> bool:
        return self.outcome == "claimed"
