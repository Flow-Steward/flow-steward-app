"""What a failed step means: its effective policy, failure class and safe report.

Effective authored policy is the first one declared in the order step →
immediate For each → enclosing Group, taken whole from one source, else
``fail`` (ADR 0038). The pinned definition already refused unknown tokens
and keys, so nothing here maps legacy aliases. The failure report is built only
from fields that cannot carry secrets.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from core.application.orchestration.active_step import ActiveStep
from core.application.workflows.workflow_definition_validation import (
    ERROR_POLICY_FAIL,
    ERROR_POLICY_TOKENS,
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
FAILED_STATUSES = frozenset({"fail", "blocked", "escalate"})


def normalize_failure_class(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in FAILURE_CLASSES else "unknown"


@dataclass(frozen=True)
class StepErrorPolicy:
    on_error: str
    #: ``step``, ``for_each``, ``group`` or ``default`` (nothing declared).
    source: str
    max_retries: int = 0
    backoff_seconds: int = 0
    retry_on: tuple[str, ...] = ()
    #: The business id of the container that declared it, when one did.
    source_id: str | None = None

    def retry_matches(self, failure_class: str) -> bool:
        if not self.retry_on:
            return True
        return normalize_failure_class(failure_class) in set(self.retry_on)


def _policy_from(raw: Any, source: str, source_id: str | None = None) -> StepErrorPolicy | None:
    if not isinstance(raw, Mapping):
        return None
    token = str(raw.get("on_error") or "")
    if token not in ERROR_POLICY_TOKENS:
        return None
    retry_on = raw.get("retry_on")
    if isinstance(retry_on, str):
        values = [retry_on]
    elif isinstance(retry_on, list):
        values = retry_on
    else:
        values = []
    wildcard = any(str(item).strip().lower() in {"any", "*", "all"} for item in values)
    return StepErrorPolicy(
        on_error=token,
        source=source,
        source_id=source_id,
        max_retries=max(0, int(raw.get("max_retries") or 0)),
        backoff_seconds=max(0, int(raw.get("backoff_seconds") or 0)),
        retry_on=()
        if wildcard
        else tuple(sorted({normalize_failure_class(item) for item in values if str(item).strip()})),
    )


def container_kind(container: Mapping[str, Any]) -> tuple[str, str]:
    """``(kind, id)`` of a Group or For each definition."""
    if container.get("for_each_id"):
        return "for_each", str(container["for_each_id"])
    return "group", str(container.get("group_id") or "")


def container_policy(container: Mapping[str, Any]) -> StepErrorPolicy | None:
    """The policy a container declares itself, or None."""
    kind, container_id = container_kind(container)
    return _policy_from(container.get("error_policy"), kind, container_id)


def effective_step_policy(
    step: Mapping[str, Any], containers: Sequence[Mapping[str, Any]] = ()
) -> StepErrorPolicy:
    """The step's own policy, else its containers' nearest first, else ``fail``."""
    policy = _policy_from(step.get("error_policy"), "step")
    if policy is not None:
        return policy
    for container in containers:
        policy = container_policy(container)
        if policy is not None:
            return policy
    return StepErrorPolicy(on_error=ERROR_POLICY_FAIL, source="default")


def _is_platform_blocker_text(text: str) -> bool:
    lower = str(text or "").lower()
    if not lower:
        return False
    markers = (
        "bwrap",
        "bubblewrap",
        "no permissions to create a new namespace",
        "namespace",
        "sandbox",
        "mcp call",
        "mcp calls",
        "user cancelled",
        "tool call cancelled",
        "tool calls cancelled",
        "command not found",
        "executable not found",
        "exec format error",
    )
    return any(marker in lower for marker in markers)


def _is_platform_blocker(
    *,
    artifacts: dict,
    errors: list[str],
    summary: str,
    feedback: str = "",
) -> bool:
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    texts: list[str] = [str(summary or ""), str(feedback or "")]
    texts.extend(str(item or "") for item in (errors or []))
    for key in (
        "blocked_reason",
        "failure_feedback",
        "stderr",
        "stdout",
        "error",
        "runner_error",
    ):
        value = artifacts.get(key)
        if isinstance(value, str):
            texts.append(value)
    for key in ("blocking_issues", "checks_run", "checks_skipped"):
        value = artifacts.get(key)
        if isinstance(value, (list, tuple)):
            texts.extend(str(item or "") for item in value)
    return any(_is_platform_blocker_text(text) for text in texts)


def _classify_failure(
    *,
    status: str,
    artifacts: dict,
    errors: list[str],
    summary: str,
) -> tuple[str, bool | None]:
    status_norm = str(status or "").strip().lower()
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    error_lines = [str(item) for item in (errors or []) if str(item).strip()]
    summary_line = str(summary or "").strip()
    merged_lower = " | ".join([summary_line, *error_lines]).lower()

    if status_norm == "blocked":
        if _is_platform_blocker(
            artifacts=artifacts,
            errors=error_lines,
            summary=summary_line,
        ):
            return "platform_blocker", False
        return "human_blocked", False

    from core.application.orchestration.retry_policy import normalize_retry_signal

    retry_signal = normalize_retry_signal(artifacts)
    closed_failure_class = str(retry_signal.get("failure_class") or "")
    if closed_failure_class:
        closed_retryable = retry_signal.get("retryable")
        return (
            closed_failure_class,
            closed_retryable if isinstance(closed_retryable, bool) else None,
        )

    provider_code = str(artifacts.get("provider_error_code") or "").strip()
    http_status = artifacts.get("http_status")
    retryable_hint = artifacts.get("retryable")
    retryable = retryable_hint if isinstance(retryable_hint, bool) else None
    http_status_int: int | None
    try:
        if http_status in (None, "") or isinstance(http_status, bool):
            http_status_int = None
        else:
            http_status_int = int(str(http_status))
    except (TypeError, ValueError):
        http_status_int = None
    if retryable is None and http_status_int is not None:
        retryable = http_status_int in {408, 429, 500, 502, 503, 504}
    if provider_code or http_status_int is not None:
        return "provider", retryable

    transient_markers = (
        "timeout",
        "timed out",
        "temporarily unavailable",
        "try again",
        "rate limit",
        "connection reset",
        "connection refused",
        "network",
        "gateway",
        "overloaded",
        "temporary",
        "disconnected",
        "reconnecting",
        "websocket closed",
        "stream disconnected",
    )
    if any(marker in merged_lower for marker in transient_markers):
        return "transient", True

    if artifacts.get("_schema_errors") or artifacts.get("_contract_errors"):
        return "contract", False

    if artifacts.get("tool_name") or artifacts.get("tool_error") or artifacts.get("tool_call"):
        return "tool", True
    if "tool_call" in merged_lower or "tool error" in merged_lower:
        return "tool", True

    if (
        "schema validation" in merged_lower
        or "contract error" in merged_lower
        or "contract invalid" in merged_lower
    ):
        return "contract", False

    if status_norm in {"fail", "error"}:
        return "validation", False
    if status_norm == "escalate":
        return "human_blocked", False
    return "unknown", None


def infer_failure_class(message: Mapping[str, Any]) -> tuple[str, bool | None]:
    """The existing failure classification of a step result."""
    artifacts = message.get("artifacts")
    errors = message.get("errors")
    return _classify_failure(
        status=str(message.get("status") or ""),
        artifacts=artifacts if isinstance(artifacts, dict) else {},
        errors=[str(item) for item in errors] if isinstance(errors, list) else [],
        summary=str(message.get("summary") or ""),
    )


_SAFE_ERROR_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_UNSAFE_MESSAGE_FRAGMENTS = (
    "password",
    "token",
    "secret",
    "://",
    "dsn",
    "params=",
    "api_key",
    "apikey",
    "credential",
    "authorization",
)
_MAX_SAFE_MESSAGE_CHARS = 512


def safe_error_code(error: dict) -> str:
    """Return the upstream failure code when it is safe to show to an operator."""
    raw = str((error or {}).get("code") or "").strip().lower()
    return raw if _SAFE_ERROR_CODE_PATTERN.fullmatch(raw) else ""


def safe_upstream_message(error: dict) -> str:
    """Return the upstream message only when it cannot be carrying a secret.

    Structured runtimes produce genuinely useful text (row counts, unsupported
    action names, provider validation messages) and losing it is what forces an
    operator to re-run the operation by hand. A message that could contain a DSN,
    a credential or an echoed request body is dropped rather than sanitised, so a
    new upstream format can never leak by default.
    """
    raw = str((error or {}).get("message") or (error or {}).get("detail") or "").strip()
    if not raw or len(raw) > _MAX_SAFE_MESSAGE_CHARS or "\n" in raw:
        return ""
    lowered = raw.lower()
    if any(fragment in lowered for fragment in _UNSAFE_MESSAGE_FRAGMENTS):
        return ""
    return raw


def safe_step_error_rows(errors: Any) -> list[dict]:
    """Project a runtime's structured errors into rows safe to persist and show.

    Only the identifying fields survive — an upstream row may also carry the
    request body, the connection string or provider credentials, and those must
    never reach the job record or the operator's screen.
    """
    if not isinstance(errors, list):
        return []
    rows: list[dict] = []
    for item in errors:
        if isinstance(item, str):
            message = safe_upstream_message({"message": item})
            if message:
                rows.append({"message": message})
            continue
        if not isinstance(item, dict):
            continue
        row: dict = {}
        code = safe_error_code(item) or str(item.get("reason") or "").strip()
        if code:
            row["code"] = code
        message = safe_upstream_message(item)
        if message:
            row["message"] = message
        path = str(item.get("path") or "").strip()
        if path and len(path) <= 256:
            row["path"] = path
        selector = str(item.get("selector") or "").strip()
        if selector and len(selector) <= 256:
            row["selector"] = selector
        if row:
            rows.append(row)
    return rows


def safe_step_error_texts(errors: Any) -> list[str]:
    """Render safe error rows as the plain strings an AgentMessage carries."""
    texts: list[str] = []
    for row in safe_step_error_rows(errors):
        code = str(row.get("code") or "").strip()
        message = str(row.get("message") or "").strip()
        location = str(row.get("selector") or row.get("path") or "").strip()
        text = ": ".join(part for part in (code, message) if part)
        if location:
            text = f"{text} (at {location})" if text else f"at {location}"
        if text:
            texts.append(text)
    return texts


def unresolved_step_failure_payload(
    current: ActiveStep,
    *,
    status: str,
    message: Mapping[str, Any] | None,
) -> dict:
    """Return the durable safe context for an unhandled active-step failure."""
    raw_artifacts = message.get("artifacts") if isinstance(message, dict) else None
    artifacts = raw_artifacts if isinstance(raw_artifacts, dict) else {}
    raw_errors = message.get("errors") if isinstance(message, dict) else None
    errors = raw_errors if isinstance(raw_errors, list) else []
    raw_transform = artifacts.get("transform")
    transform = raw_transform if isinstance(raw_transform, dict) else {}
    raw_transform_errors = transform.get("errors")
    transform_errors = raw_transform_errors if isinstance(raw_transform_errors, list) else []
    transform_error = next(
        (item for item in transform_errors if isinstance(item, dict)),
        {},
    )
    raw_tabular = artifacts.get("tabular_feed")
    tabular = raw_tabular if isinstance(raw_tabular, dict) else {}
    raw_tabular_errors = tabular.get("errors")
    tabular_errors = raw_tabular_errors if isinstance(raw_tabular_errors, list) else []
    tabular_error = next(
        (item for item in tabular_errors if isinstance(item, dict)),
        {},
    )
    raw_dataset = artifacts.get("dataset_query")
    dataset = raw_dataset if isinstance(raw_dataset, dict) else {}
    raw_dataset_errors = dataset.get("errors")
    dataset_errors = raw_dataset_errors if isinstance(raw_dataset_errors, list) else []
    dataset_error = next(
        (item for item in dataset_errors if isinstance(item, dict)),
        {},
    )
    diagnostic_kind = ""
    if transform_error:
        diagnostic_kind = "transform"
    elif tabular_error:
        diagnostic_kind = "tabular_feed"
    elif dataset_error:
        diagnostic_kind = "dataset_query"
    diagnostic_error = transform_error or tabular_error or dataset_error
    diagnostic_reason = str(diagnostic_error.get("reason") or "").strip()
    if not diagnostic_reason:
        # tabular_feed and transform report their failure under "code"; only "reason"
        # was ever read, so every coded failure collapsed to the step-kind default.
        diagnostic_reason = safe_error_code(diagnostic_error)
    raw_selector = str(diagnostic_error.get("selector") or "").strip()
    diagnostic_selector = (
        raw_selector
        if len(raw_selector) <= 256
        and (raw_selector == "state" or raw_selector.startswith("state."))
        else ""
    )
    detail = str(artifacts.get("detail") or "").strip()
    summary = str((message or {}).get("summary") or "").strip()
    fallback_error = next(
        (str(item).strip() for item in errors if str(item).strip()),
        "",
    )
    diagnostic_path = ""
    dataset_message = ""
    if diagnostic_kind == "dataset_query":
        raw_code = str(diagnostic_error.get("code") or "").strip().lower()
        raw_message = str(diagnostic_error.get("message") or "").strip()
        raw_path = str(diagnostic_error.get("path") or diagnostic_error.get("detail") or "").strip()
        message_lower = raw_message.lower()
        if (
            "sql is required" in message_lower
            or "query_definition_jsonb.sql is required" in message_lower
        ):
            diagnostic_reason = "query_sql_missing"
        elif "read-only select" in message_lower or "where clause or limit" in message_lower:
            diagnostic_reason = "query_unsafe"
        elif "resource scope" in message_lower:
            diagnostic_reason = "dataset_scope_invalid"
        elif "parameter" in message_lower and raw_code in {
            "invalid_config",
            "query_parameter_mapping_invalid",
            "saved_query_definition_invalid",
        }:
            diagnostic_reason = "query_parameters_invalid"
        elif "connection refused" in message_lower:
            diagnostic_reason = "provider_unavailable"
        elif raw_code == "timeout":
            diagnostic_reason = "connector_timeout"
        elif re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", raw_code):
            diagnostic_reason = raw_code
        else:
            diagnostic_reason = "provider_rejected"

        unsafe_fragments = ("password", "token", "secret", "://", "dsn", "params=")
        safe_raw_message = (
            bool(raw_message)
            and len(raw_message) <= 512
            and "\n" not in raw_message
            and not any(fragment in message_lower for fragment in unsafe_fragments)
        )
        if diagnostic_reason == "connector_timeout":
            dataset_message = "Dataset connector timed out"
        elif diagnostic_reason == "provider_unavailable":
            dataset_message = "Dataset provider is unavailable"
        elif diagnostic_reason in {"provider_rejected", "connector_runtime_failed"}:
            dataset_message = "Dataset connector rejected the request"
        elif safe_raw_message:
            dataset_message = raw_message
        else:
            dataset_message = "Dataset query failed"
        if (
            raw_path
            and len(raw_path) <= 256
            and re.fullmatch(r"[A-Za-z0-9_.\[\]-]+", raw_path)
            and not any(
                fragment in raw_path.lower() for fragment in ("password", "token", "secret")
            )
        ):
            diagnostic_path = raw_path
    if diagnostic_kind == "transform" and diagnostic_reason == "input_item_not_object":
        safe_message = (
            f"Transform input at {diagnostic_selector} must resolve to an object."
            if diagnostic_selector
            else "Transform input must resolve to an object."
        )
    elif diagnostic_kind == "tabular_feed" and diagnostic_reason == "state_reference_missing":
        safe_message = (
            f"Tabular input at {diagnostic_selector} references missing workflow state."
            if diagnostic_selector
            else "Tabular input references missing workflow state."
        )
    elif diagnostic_kind == "dataset_query":
        safe_message = dataset_message
    elif diagnostic_kind in {"transform", "tabular_feed"}:
        safe_message = (
            safe_upstream_message(diagnostic_error)
            or (f"{diagnostic_kind} step failed: {diagnostic_reason}" if diagnostic_reason else "")
            or summary
            or detail
            or fallback_error
            or "Workflow step failed."
        )
    else:
        safe_message = summary or detail or fallback_error or "Workflow step failed."
    payload = {
        "status": str(status or "fail").strip().lower() or "fail",
        "step_id": current.step_id,
        "step_execution_id": current.step_execution_id,
        "attempt": current.attempt,
        "correlation_id": str((message or {}).get("correlation_id") or "").strip(),
        "error_code": str(
            diagnostic_reason
            or artifacts.get("error_code")
            or artifacts.get("error")
            or status
            or "step_failed"
        ).strip(),
        "message": safe_message,
    }
    if diagnostic_selector:
        payload["selector"] = diagnostic_selector
    if diagnostic_path:
        payload["path"] = diagnostic_path
    if diagnostic_kind:
        payload["diagnostic_kind"] = diagnostic_kind
    if current.group_id:
        payload["group_id"] = current.group_id
    occurrence = artifacts.get("occurrence")
    if occurrence not in (None, ""):
        payload["occurrence"] = occurrence
    return payload


__all__ = [
    "FAILED_STATUSES",
    "FAILURE_CLASSES",
    "StepErrorPolicy",
    "container_kind",
    "container_policy",
    "effective_step_policy",
    "infer_failure_class",
    "normalize_failure_class",
    "safe_step_error_rows",
    "safe_step_error_texts",
    "unresolved_step_failure_payload",
]
