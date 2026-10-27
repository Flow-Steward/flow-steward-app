"""Provider-neutral retry facts and operation-level retry policy.

Provider adapters own interpretation of their response formats.  This module
accepts only a closed, safe projection and never inspects provider identity,
operation identity, URLs, headers, or response bodies.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

DEFAULT_MAX_RETRIES = 2
DEFAULT_INITIAL_BACKOFF_SECONDS = 2.0
DEFAULT_MAX_BACKOFF_SECONDS = 120.0
DEFAULT_OPERATION_DEADLINE_SECONDS = 300.0
MAX_JITTER_RATIO = 0.20

REQUEST_SEMANTICS = frozenset({"safe_read", "mutation", "reconciled"})
EXTERNAL_EFFECT_STATUSES = frozenset(
    {"failed", "timeout_unknown", "succeeded", "running", "suppressed"}
)
FAILURE_CLASSES = frozenset(
    {
        "transient",
        "contract",
        "tool",
        "validation",
        "provider",
        "human_blocked",
        "approval_rejected",
        "platform_blocker",
        "unknown",
    }
)
OPERATION_KINDS = frozenset(
    {
        "action",
        "bulk_action",
        "control",
        "discovery",
        "event",
        "human_channel",
        "query",
        "read",
        "report",
        "source",
        "tool_call",
    }
)
_SAFE_READ_OPERATION_KINDS = frozenset({"discovery", "query", "read", "report", "source"})

_SAFE_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _signal_mapping(value: Any) -> list[Mapping[str, Any]]:
    """Return exactly one caller-selected, protocol-owned signal envelope.

    Business results and arbitrary error objects are provider-controlled data.
    Callers that own a transport envelope must select that envelope explicitly;
    this normalizer never searches nested containers for retry authority.
    """
    root = _mapping(value)
    return [root] if root else []


def _first(mappings: list[Mapping[str, Any]], key: str) -> Any:
    for value in mappings:
        if key in value:
            return value[key]
    return None


def _closed_text(value: Any, allowed: frozenset[str]) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else ""


def _safe_code(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip().lower()
    return normalized if _SAFE_CODE_RE.fullmatch(normalized) else ""


def _positive_seconds(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return seconds


def _http_status(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    return status if 100 <= status <= 599 else None


def normalize_retry_signal(
    value: Any,
    *,
    operation_kind: str = "",
    request_semantics: str = "",
) -> dict[str, Any]:
    """Project one explicitly selected runtime envelope onto the closed contract."""
    mappings = _signal_mapping(value)
    projected: dict[str, Any] = {}

    normalized_kind = _closed_text(operation_kind, OPERATION_KINDS) or _closed_text(
        _first(mappings, "operation_kind"), OPERATION_KINDS
    )
    normalized_semantics = _closed_text(request_semantics, REQUEST_SEMANTICS) or _closed_text(
        _first(mappings, "request_semantics"), REQUEST_SEMANTICS
    )
    if not normalized_semantics and normalized_kind:
        normalized_semantics = request_semantics_for_operation_kind(normalized_kind)
    failure_class = _closed_text(_first(mappings, "failure_class"), FAILURE_CLASSES)
    external_status = _closed_text(
        _first(mappings, "external_effect_status"), EXTERNAL_EFFECT_STATUSES
    )
    retry_after = _positive_seconds(_first(mappings, "retry_after_seconds"))
    provider_error_code = _safe_code(_first(mappings, "provider_error_code"))
    http_status = _http_status(_first(mappings, "http_status"))
    if http_status is None:
        http_status = _http_status(_first(mappings, "status_code"))

    if normalized_kind:
        projected["operation_kind"] = normalized_kind
    if normalized_semantics:
        projected["request_semantics"] = normalized_semantics
    if failure_class:
        projected["failure_class"] = failure_class
    retryable = _first(mappings, "retryable")
    if isinstance(retryable, bool):
        projected["retryable"] = retryable
    if retry_after is not None:
        projected["retry_after_seconds"] = retry_after
    definitely_no_effect = _first(mappings, "definitely_no_external_effect")
    if isinstance(definitely_no_effect, bool):
        projected["definitely_no_external_effect"] = definitely_no_effect
    if external_status:
        projected["external_effect_status"] = external_status
    if provider_error_code:
        projected["provider_error_code"] = provider_error_code
    if http_status is not None:
        projected["http_status"] = http_status
    return projected


class OperationRetrySignalError(RuntimeError):
    """Abort an agent tool loop while preserving a closed retry signal."""

    def __init__(
        self,
        signal: Mapping[str, Any],
        *,
        operation_deadline_epoch_seconds: float | None = None,
    ) -> None:
        super().__init__("operation failed with a structured retry signal")
        self.retry_signal = normalize_retry_signal(signal)
        try:
            deadline = float(operation_deadline_epoch_seconds or 0.0)
        except (TypeError, ValueError):
            deadline = 0.0
        self.operation_deadline_epoch_seconds = deadline if math.isfinite(deadline) else 0.0


def request_semantics_for_operation_kind(operation_kind: Any) -> str:
    """Map a provider-neutral operation kind to conservative request semantics."""
    normalized = _closed_text(operation_kind, OPERATION_KINDS)
    return "safe_read" if normalized in _SAFE_READ_OPERATION_KINDS else "mutation"


def parse_retry_after(value: Any, *, now: datetime | None = None) -> float | None:
    """Parse an RFC Retry-After delta or HTTP-date into a positive delay."""
    direct = _positive_seconds(value)
    if direct is not None:
        return direct
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return _positive_seconds((parsed - reference).total_seconds())


def policy_from_state(
    state: Mapping[str, Any],
    *,
    configured_retries: int | None = None,
) -> RetryPolicy:
    """Build retry policy from project state with fail-closed numeric coercion."""
    project_policy = _mapping(state.get("project_policy"))
    if not project_policy:
        project_policy = _mapping(_mapping(state.get("project_config")).get("project_policy"))

    def nonnegative_int(key: str, fallback: int) -> int:
        raw = project_policy.get(key, fallback)
        if isinstance(raw, bool):
            return fallback
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return fallback

    default_retries = (
        DEFAULT_MAX_RETRIES if configured_retries is None else max(0, int(configured_retries))
    )
    maximum = nonnegative_int("retry_backoff_max_seconds", int(DEFAULT_MAX_BACKOFF_SECONDS))
    initial = nonnegative_int("retry_backoff_initial_seconds", int(DEFAULT_INITIAL_BACKOFF_SECONDS))
    if maximum > 0:
        initial = min(initial, maximum)
    return RetryPolicy(
        max_retries=nonnegative_int("retry_max_attempts", default_retries),
        initial_backoff_seconds=initial,
        max_backoff_seconds=maximum,
        operation_deadline_seconds=nonnegative_int(
            "retry_operation_deadline_seconds",
            int(DEFAULT_OPERATION_DEADLINE_SECONDS),
        ),
        strategy=str(project_policy.get("retry_strategy") or "bounded"),
    )


@dataclass(frozen=True)
class RetryPolicy:
    """Project retry settings; ``max_retries`` excludes the first invocation."""

    max_retries: int = DEFAULT_MAX_RETRIES
    initial_backoff_seconds: float = DEFAULT_INITIAL_BACKOFF_SECONDS
    max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS
    operation_deadline_seconds: float = DEFAULT_OPERATION_DEADLINE_SECONDS
    strategy: str = "bounded"

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_retries", max(0, int(self.max_retries)))
        initial = max(0.0, float(self.initial_backoff_seconds))
        maximum = max(0.0, float(self.max_backoff_seconds))
        object.__setattr__(self, "initial_backoff_seconds", initial)
        object.__setattr__(self, "max_backoff_seconds", maximum)
        object.__setattr__(
            self,
            "operation_deadline_seconds",
            max(0.0, float(self.operation_deadline_seconds)),
        )
        strategy = str(self.strategy or "bounded").strip().lower()
        object.__setattr__(
            self,
            "strategy",
            strategy if strategy in {"bounded", "flat", "failure_class"} else "bounded",
        )


@dataclass(frozen=True)
class RetryDecision:
    should_retry: bool
    reason_code: str
    retry_number: int
    max_retries: int
    failure_class: str
    base_delay_seconds: float = 0.0
    applied_delay_seconds: float = 0.0
    retry_after_seconds: float | None = None
    provider_error_code: str = ""
    http_status: int | None = None

    def telemetry(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "attempt_number": self.retry_number + 1,
            "max_retries": self.max_retries,
            "base_delay_seconds": self.base_delay_seconds,
            "applied_delay_seconds": self.applied_delay_seconds,
            "failure_class": self.failure_class,
            "reason_code": self.reason_code,
        }
        if self.retry_after_seconds is not None:
            payload["retry_after_seconds"] = self.retry_after_seconds
        if self.provider_error_code:
            payload["provider_error_code"] = self.provider_error_code
        if self.http_status is not None:
            payload["http_status"] = self.http_status
        return payload


def _terminal(
    *,
    reason_code: str,
    retry_number: int,
    policy: RetryPolicy,
    signal: Mapping[str, Any],
    base_delay_seconds: float = 0.0,
    applied_delay_seconds: float = 0.0,
) -> RetryDecision:
    return RetryDecision(
        should_retry=False,
        reason_code=reason_code,
        retry_number=retry_number,
        max_retries=policy.max_retries,
        failure_class=str(signal.get("failure_class") or "unknown"),
        base_delay_seconds=base_delay_seconds,
        applied_delay_seconds=applied_delay_seconds,
        retry_after_seconds=_positive_seconds(signal.get("retry_after_seconds")),
        provider_error_code=_safe_code(signal.get("provider_error_code")),
        http_status=_http_status(signal.get("http_status")),
    )


def decide_retry(
    *,
    signal: Mapping[str, Any],
    retry_number: int,
    policy: RetryPolicy,
    remaining_deadline_seconds: float | None = None,
    rng: Callable[[], float],
) -> RetryDecision:
    """Decide whether retry ``retry_number`` may run and how long to wait.

    ``retry_number`` starts at one for the first retry (the second invocation).
    The caller is the sole owner of sleeping and issuing the next invocation.
    """
    normalized = normalize_retry_signal(signal)
    retry_index = max(1, int(retry_number))
    failure_class = str(normalized.get("failure_class") or "unknown")

    if normalized.get("retryable") is not True:
        return _terminal(
            reason_code="not_retryable",
            retry_number=retry_index,
            policy=policy,
            signal=normalized,
        )
    if failure_class not in {"provider", "transient"}:
        return _terminal(
            reason_code="failure_class_not_retryable",
            retry_number=retry_index,
            policy=policy,
            signal=normalized,
        )
    external_status = str(normalized.get("external_effect_status") or "")
    if external_status in {"timeout_unknown", "running", "succeeded"}:
        return _terminal(
            reason_code="external_effect_ambiguous",
            retry_number=retry_index,
            policy=policy,
            signal=normalized,
        )
    if external_status == "suppressed":
        return _terminal(
            reason_code="external_effect_not_replayable",
            retry_number=retry_index,
            policy=policy,
            signal=normalized,
        )

    semantics = str(normalized.get("request_semantics") or "")
    if semantics == "mutation":
        if external_status != "failed":
            return _terminal(
                reason_code="mutation_effect_not_definitively_failed",
                retry_number=retry_index,
                policy=policy,
                signal=normalized,
            )
        if normalized.get("definitely_no_external_effect") is not True:
            return _terminal(
                reason_code="mutation_retry_requires_reconciliation",
                retry_number=retry_index,
                policy=policy,
                signal=normalized,
            )
    if semantics == "reconciled":
        return _terminal(
            reason_code="reconciled_retry_requires_durable_provenance",
            retry_number=retry_index,
            policy=policy,
            signal=normalized,
        )
    if semantics not in {"safe_read", "mutation"}:
        return _terminal(
            reason_code="request_semantics_not_retryable",
            retry_number=retry_index,
            policy=policy,
            signal=normalized,
        )
    if retry_index > policy.max_retries:
        return _terminal(
            reason_code="retry_exhausted",
            retry_number=retry_index,
            policy=policy,
            signal=normalized,
        )

    multiplier = 1 if policy.strategy == "flat" else 2 ** (retry_index - 1)
    base = policy.initial_backoff_seconds * multiplier
    if policy.max_backoff_seconds > 0:
        base = min(policy.max_backoff_seconds, base)
    jitter_fraction = min(1.0, max(0.0, float(rng())))
    delay = base + base * MAX_JITTER_RATIO * jitter_fraction
    if policy.max_backoff_seconds > 0:
        delay = min(policy.max_backoff_seconds, delay)
    retry_after = _positive_seconds(normalized.get("retry_after_seconds"))
    if retry_after is not None:
        if policy.max_backoff_seconds > 0 and retry_after > policy.max_backoff_seconds:
            return _terminal(
                reason_code="retry_delay_exceeds_policy",
                retry_number=retry_index,
                policy=policy,
                signal=normalized,
                base_delay_seconds=base,
                applied_delay_seconds=retry_after,
            )
        delay = max(delay, retry_after)
    remaining = _positive_seconds(remaining_deadline_seconds)
    if remaining_deadline_seconds is not None and (remaining is None or delay > remaining):
        return _terminal(
            reason_code="retry_delay_exceeds_deadline",
            retry_number=retry_index,
            policy=policy,
            signal=normalized,
            base_delay_seconds=base,
            applied_delay_seconds=delay,
        )
    return RetryDecision(
        should_retry=True,
        reason_code="retry_scheduled",
        retry_number=retry_index,
        max_retries=policy.max_retries,
        failure_class=failure_class,
        base_delay_seconds=base,
        applied_delay_seconds=delay,
        retry_after_seconds=retry_after,
        provider_error_code=_safe_code(normalized.get("provider_error_code")),
        http_status=_http_status(normalized.get("http_status")),
    )


__all__ = [
    "DEFAULT_INITIAL_BACKOFF_SECONDS",
    "DEFAULT_MAX_BACKOFF_SECONDS",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_OPERATION_DEADLINE_SECONDS",
    "EXTERNAL_EFFECT_STATUSES",
    "FAILURE_CLASSES",
    "OPERATION_KINDS",
    "REQUEST_SEMANTICS",
    "RetryDecision",
    "RetryPolicy",
    "decide_retry",
    "normalize_retry_signal",
    "parse_retry_after",
    "policy_from_state",
    "request_semantics_for_operation_kind",
]
