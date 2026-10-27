"""Shared formatting helpers for report UI projections."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from core.application.observability_vocabulary import job_status_meta
from core.infrastructure.security.prompt_safety import (
    is_canonical_structured_registry_identifier,
    redact_structure_for_persistence,
    redact_text_for_persistence,
)
from core.infrastructure.security.secret_detection import (
    SECRET_PATTERNS,
    contains_detected_secret,
    redact_detected_secrets,
)

_SPECIAL_WORDS = {
    "api": "API",
    "cli": "CLI",
    "id": "ID",
    "json": "JSON",
    "ui": "UI",
    "url": "URL",
}

_STEP_STATUS_META = {
    "completed": {"label": "Completed", "tone": "green"},
    "in_progress": {"label": "In progress", "tone": "blue"},
    "awaiting_review": {"label": "Waiting for review", "tone": "amber"},
    "pending": {"label": "Not started", "tone": "gray"},
    "failed": {"label": "Failed", "tone": "red"},
    "interrupted": {"label": "Interrupted", "tone": "red"},
    "skipped": {"label": "Skipped", "tone": "gray"},
    "cancelled": {"label": "Cancelled", "tone": "gray"},
}

NOT_RECORDED = "Not recorded"
_AUTONOMY_GATE_SENTINEL = "Step autonomy gate requires operator review before execution."
_RAW_PREVIEW_CHAR_BUDGET = 12_000
_RAW_PREVIEW_MAX_DEPTH = 6
_RAW_PREVIEW_MAX_ITEMS = 2
_RAW_PREVIEW_MAX_KEYS = 32
_RAW_PREVIEW_STRING_LIMIT = 1_000
_RAW_PREVIEW_MARKER = "_flow_steward_projection"
_TOKEN_CONTINUATION_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-./+="
_CANONICAL_SECRET_MARKERS = tuple(
    f"[SECRET:{pattern.pattern_id}:REDACTED]" for pattern in SECRET_PATTERNS
)


def _visible_product_label(value: Any) -> str:
    return str(value or "")


def _normalize_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _normalize_rows(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _normalize_role(role: Any) -> str:
    normalized = str(role or "").strip().lower()
    if normalized in {"admin", "operator", "viewer"}:
        return normalized
    return "viewer"


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _format_datetime(value: Any) -> str:
    dt = _as_datetime(value)
    if dt is None:
        return NOT_RECORDED
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _format_duration_ms(value: Any) -> str:
    try:
        total_seconds = max(0, int(value or 0) // 1000)
    except Exception:
        return NOT_RECORDED
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def _format_currency(value: Any) -> str:
    try:
        amount = float(value or 0.0)
    except Exception:
        amount = 0.0
    return f"${amount:,.2f}"


def _format_cost_knownness_display(known_cost: Any, knownness: str, unknown_tokens: Any) -> str:
    display = _format_currency(known_cost)
    try:
        token_count = int(unknown_tokens or 0)
    except Exception:
        token_count = 0
    normalized = str(knownness or "").strip().lower()
    if normalized == "partial" and token_count > 0:
        return f"{display} known + unknown cost ({_format_token_count(token_count)} tokens)"
    if normalized == "unknown" and token_count > 0:
        return f"Unknown cost ({_format_token_count(token_count)} tokens)"
    return display


def _format_token_count(value: Any) -> str:
    try:
        count = int(value or 0)
    except Exception:
        count = 0
    return f"{count:,}"


def _truncate_text(value: Any, *, limit: int = 220) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _step_runtime_summary(step_status: str) -> str:
    if step_status == "in_progress":
        return "This is the step Flow Steward is actively running."
    if step_status == "awaiting_review":
        return "This step produced output that now needs human review."
    if step_status == "completed":
        return "This step completed earlier in this run."
    return "Flow Steward has not reached this step yet."


def _step_runtime_result(step_status: str) -> str:
    if step_status == "interrupted":
        return "This step stopped before Flow Steward could move forward."
    if step_status == "pending":
        return "This step has not started yet."
    if step_status == "in_progress":
        return "This step is still producing work."
    return "This step completed."


def _decision_tone(action: str, approved: bool) -> str:
    if action in {"skip", "new_task"}:
        return "amber"
    if action == "abort":
        return "red"
    if action:
        return "blue"
    return "green" if approved else "red"


def _human_readable_machine_output(value: Any, *, limit: int = 220) -> str:
    if value in (None, "", {}, []):
        return ""
    if isinstance(value, dict):
        for key in ("summary", "message", "result", "detail", "title", "output", "text", "report"):
            text = str(value.get(key) or "").strip()
            if text:
                return _truncate_text(text, limit=limit)
        return ""
    if isinstance(value, list):
        for item in value:
            text = _human_readable_machine_output(item, limit=limit)
            if text:
                return text
        return ""

    text = str(value or "").strip()
    if not text:
        return ""
    if text[:1] in {"{", "["}:
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if parsed is not None:
            extracted = _human_readable_machine_output(parsed, limit=limit)
            if extracted:
                return extracted
            return ""
    return _truncate_text(text, limit=limit)


def _redact_for_ui(
    value: Any,
    *,
    project_id: str | None = None,
    job_id: str | None = None,
    context_type: str = "display",
) -> Any:
    try:
        if isinstance(value, str):
            return redact_text_for_persistence(
                value,
                project_id=project_id,
                job_id=job_id,
                context_type=context_type,
            )
        return redact_structure_for_persistence(
            value,
            project_id=project_id,
            job_id=job_id,
            context_type=context_type,
        )
    except Exception:
        return value


def _bounded_raw_preview(
    value: Any,
    *,
    project_id: str | None = None,
    job_id: str | None = None,
    max_items: int = _RAW_PREVIEW_MAX_ITEMS,
) -> Any:
    """Build a redacted, shape-preserving preview with bounded traversal.

    Container breadth and depth are bounded before descent, while every leaf
    selected for display is redacted in full before it is truncated. This
    prevents a credential spanning the preview boundary from being exposed as
    an unrecognisable partial token without deep-walking the omitted payload.
    """
    remaining = _RAW_PREVIEW_CHAR_BUDGET

    def _marker(**metadata: Any) -> dict[str, Any]:
        return {
            _RAW_PREVIEW_MARKER: {
                "truncated": True,
                "preview_only": True,
                **metadata,
            }
        }

    def _neutralize_secret_markers(redacted_text: str) -> str:
        normalized = redacted_text
        for marker in _CANONICAL_SECRET_MARKERS:
            normalized = normalized.replace(marker, "[REDACTED]")
        return normalized

    def _redact_leaf(text: str) -> str:
        try:
            canonically_redacted = _neutralize_secret_markers(redact_detected_secrets(text))
            redacted = str(
                redact_text_for_persistence(
                    canonically_redacted,
                    project_id=project_id,
                    job_id=job_id,
                    context_type="display",
                )
            )
            return _neutralize_secret_markers(redacted)
        except Exception:
            # A diagnostic preview must fail closed instead of returning an
            # unredacted credential when the policy engine is unavailable.
            return "[REDACTED: preview unavailable]"

    def _visit(current: Any, *, depth: int, current_key: str = "") -> Any:
        nonlocal remaining
        if remaining <= 0:
            return _marker(reason="preview_budget_exhausted")
        if current is None or isinstance(current, (bool, int, float)):
            remaining -= len(str(current))
            return current
        if isinstance(current, str):
            safe_text = (
                current
                if is_canonical_structured_registry_identifier(current_key, current)
                else _redact_leaf(current)
            )
            allowed = max(0, min(_RAW_PREVIEW_STRING_LIMIT, remaining))
            remaining -= min(len(safe_text), allowed)
            if len(safe_text) <= allowed:
                return safe_text
            omitted = len(safe_text) - allowed
            safe_prefix = safe_text[:allowed]
            if (
                safe_prefix
                and allowed < len(safe_text)
                and safe_prefix[-1] in _TOKEN_CONTINUATION_CHARS
                and safe_text[allowed] in _TOKEN_CONTINUATION_CHARS
            ):
                safe_prefix = (
                    safe_prefix.rstrip(_TOKEN_CONTINUATION_CHARS) + "[REDACTED_TRUNCATED_TOKEN]"
                )
            safe_prefix = _neutralize_secret_markers(redact_detected_secrets(safe_prefix))
            if contains_detected_secret(safe_prefix):
                safe_prefix = "[REDACTED: preview unavailable]"
            return f"{safe_prefix}… [truncated {omitted} redacted chars]"
        if depth >= _RAW_PREVIEW_MAX_DEPTH:
            return _marker(reason="max_depth", value_type=type(current).__name__)
        if isinstance(current, dict):
            preview: dict[str, Any] = {}
            for index, (raw_key, raw_value) in enumerate(current.items()):
                if index >= _RAW_PREVIEW_MAX_KEYS:
                    break
                if remaining <= 0:
                    break
                key = _redact_leaf(str(raw_key))[:160]
                remaining -= len(key)
                preview[key] = _visit(
                    raw_value,
                    depth=depth + 1,
                    current_key=str(raw_key),
                )
            omitted_keys = max(0, len(current) - len(preview))
            if omitted_keys or remaining <= 0:
                preview[_RAW_PREVIEW_MARKER] = {
                    "truncated": True,
                    "preview_only": True,
                    "omitted_keys": omitted_keys,
                }
            return preview
        if isinstance(current, (list, tuple)):
            preview_rows = [
                _visit(item, depth=depth + 1, current_key=current_key)
                for item in current[:max_items]
                if remaining > 0
            ]
            omitted_items = max(0, len(current) - len(preview_rows))
            if omitted_items or remaining <= 0:
                preview_rows.append(
                    _marker(omitted_items=omitted_items, original_type=type(current).__name__)
                )
            return preview_rows
        text = str(current)
        return _visit(text, depth=depth + 1, current_key=current_key)

    return _visit(value, depth=0)


def _bounded_diagnostic_value(
    value: Any,
    *,
    project_id: str | None = None,
    job_id: str | None = None,
    max_items: int = _RAW_PREVIEW_MAX_ITEMS,
) -> Any:
    return _bounded_raw_preview(
        value,
        project_id=project_id,
        job_id=job_id,
        max_items=max_items,
    )


def _serialize_raw(
    value: Any,
    *,
    project_id: str | None = None,
    job_id: str | None = None,
) -> str:
    if value in (None, "", {}, []):
        return ""
    redacted_value = _bounded_diagnostic_value(
        value,
        project_id=project_id,
        job_id=job_id,
    )
    if isinstance(redacted_value, str):
        return redacted_value
    try:
        return json.dumps(redacted_value, indent=2, sort_keys=True, ensure_ascii=True)
    except Exception:
        return str(redacted_value)


def _humanize_identifier(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    raw_parts = [part for part in text.replace("-", " ").replace("_", " ").split() if part]
    words: list[str] = []
    for part in raw_parts:
        lowered = part.lower()
        words.append(_SPECIAL_WORDS.get(lowered, part.capitalize()))
    return " ".join(words)


def _status_meta(status: Any) -> dict[str, str]:
    meta = job_status_meta(status)
    return {
        "label": str(meta.get("label") or "Unknown status"),
        "tone": str(meta.get("tone") or "gray"),
        "headline": str(
            meta.get("headline") or "Flow Steward recorded this run with an uncommon status."
        ),
    }


def _map_step_status(status: Any) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"ok", "completed", "complete", "done", "success", "passed", "pass"}:
        return "completed"
    if normalized in {
        "running",
        "in_progress",
        "resume_pending",
        "active",
        # A Wait or child invocation step is still running while it is paused.
        "awaiting_timer",
        "awaiting_child",
        "external_wait",
    }:
        return "in_progress"
    if normalized in {"awaiting_review", "awaiting_approval", "paused"}:
        return "awaiting_review"
    if normalized in {"error", "failed", "fail"}:
        return "failed"
    if normalized in {"retryable_interrupted_step", "interrupted"}:
        return "interrupted"
    if normalized in {"pending", "queued", "planned"}:
        return "pending"
    # A disabled step the run skipped never ran: it is not a completed step.
    if normalized == "skipped":
        return "skipped"
    # A step the run stopped before it finished did not complete either.
    if normalized in {"cancelled", "canceled"}:
        return "cancelled"
    return "completed"


def _message_summary(message: dict[str, Any]) -> str:
    summary = str(message.get("summary") or "").strip()
    if summary:
        return summary
    artifacts = _normalize_dict(message.get("artifacts"))
    for key, value in artifacts.items():
        if key.startswith("_") or key in {"raw_output"}:
            continue
        text = _human_readable_machine_output(value, limit=220)
        if text:
            return text
    raw_output_text = _human_readable_machine_output(artifacts.get("raw_output"), limit=220)
    if raw_output_text:
        return raw_output_text
    if artifacts.get("raw_output") not in (None, "", {}, []):
        return "Flow Steward recorded machine-formatted output for this step."
    errors = message.get("errors")
    if isinstance(errors, list) and errors:
        return _truncate_text(errors[0], limit=220)
    return "Flow Steward recorded this handoff without a human-readable summary."


def _message_evidence(
    message: dict[str, Any], *, field_label_keys: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    artifacts = _normalize_dict(message.get("artifacts"))
    cards: list[dict[str, Any]] = []
    for key, value in artifacts.items():
        if key.startswith("_") or key in {
            "raw_output",
            "source_artifact",
            "input_artifact",
            "catalog_artifact",
            "artifact",
        }:
            continue
        text = _human_readable_machine_output(value, limit=280)
        if not text:
            continue
        cards.append(
            {
                "title": _humanize_identifier(key) or "Artifact",
                **(
                    {"title_key": field_label_keys[key]}
                    if field_label_keys and key in field_label_keys
                    else {}
                ),
                "summary": text,
                "meta": _humanize_identifier(key),
                "raw": _bounded_diagnostic_value(value),
                "tone": "blue",
            }
        )
        if len(cards) >= 2:
            break
    seen_artifact_ids: set[str] = set()
    for key, label in (
        ("source_artifact", "Input catalog CSV"),
        ("input_artifact", "Input file"),
        ("catalog_artifact", "Generated catalog CSV"),
        ("artifact", "Generated artifact"),
    ):
        artifact = _normalize_dict(artifacts.get(key))
        artifact_id = str(artifact.get("artifact_id") or "").strip()
        if artifact_id and artifact_id in seen_artifact_ids:
            continue
        filename = str(artifact.get("filename") or "").strip()
        download_url = str(artifact.get("download_url") or "").strip()
        if not filename and not artifact_id and not download_url:
            continue
        if artifact_id:
            seen_artifact_ids.add(artifact_id)
        row_count = str(artifact.get("row_count") or "").strip()
        size_bytes = str(artifact.get("size_bytes") or "").strip()
        details = []
        if row_count:
            details.append(f"{row_count} rows")
        if size_bytes:
            details.append(f"{size_bytes} bytes")
        summary = filename or artifact_id or download_url
        if details:
            summary = f"{summary} ({', '.join(details)})"
        cards.append(
            {
                "title": label,
                "summary": _truncate_text(summary, limit=280),
                "href": download_url,
                "meta": "Workflow artifact",
                "raw": _bounded_diagnostic_value(artifact),
                "tone": "blue",
            }
        )
        if len(cards) >= 3:
            break
    errors = message.get("errors")
    if isinstance(errors, list):
        for error in errors[:1]:
            cards.append(
                {
                    "title": "Error detail",
                    "summary": _truncate_text(error, limit=280),
                    "meta": "Agent message error",
                    "raw": _bounded_diagnostic_value(str(error or "")),
                    "tone": "red",
                }
            )
    return cards[:3]


def _message_reason(message: dict[str, Any]) -> str:
    caused_by = message.get("caused_by")
    if isinstance(caused_by, list) and caused_by:
        return "This handoff followed an earlier step in the same workflow."
    step_label = _humanize_identifier(message.get("step_id"))
    if step_label:
        return f"Flow Steward ran {step_label} and produced a new handoff."
    return "Flow Steward progressed to the next recorded handoff."


def _step_scope_id(
    *,
    step_id: Any = "",
    agent: Any = "",
    skill: Any = "",
    fallback: Any = "",
) -> str:
    explicit = str(step_id or "").strip()
    if explicit:
        return explicit
    agent_text = str(agent or "").strip()
    skill_text = str(skill or "").strip()
    if agent_text and skill_text:
        return f"{agent_text}:{skill_text}"
    if skill_text:
        return skill_text
    if agent_text:
        return agent_text
    return str(fallback or "").strip()
