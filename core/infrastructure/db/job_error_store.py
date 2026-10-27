"""Reusable helpers for structured runtime error normalization and storage."""

from __future__ import annotations

import json
import traceback
from typing import Any

from core.infrastructure.db import psycopg_runtime as psycopg_runtime
from core.infrastructure.db.platform_logger_common import _step_execution_id
from core.infrastructure.security.prompt_safety import (
    redact_diagnostic_identifier_for_persistence,
)


def insert_job_error(
    *,
    conn,
    table_exists,
    normalize_error_source,
    normalize_error_category,
    truncate_text,
    job_id: str,
    source: str = "unknown",
    category: str = "unknown",
    provider_name: str | None = None,
    http_status: int | None = None,
    provider_error_code: str | None = None,
    provider_error_type: str | None = None,
    request_id: str | None = None,
    retryable: bool | None = None,
    error_message: str = "",
    response_excerpt: str | None = None,
    traceback_text: str | None = None,
    context_jsonb: dict | None = None,
    message_id: str | None = None,
    agent_id: str | None = None,
    group_id: str | None = None,
    step_id: str | None = None,
    step_execution_id: str | None = None,
) -> dict | None:
    """Persist one structured runtime error row."""
    if not table_exists("job_errors"):
        return None

    normalized_source = normalize_error_source(source)
    normalized_category = normalize_error_category(category)

    clean_message = truncate_text(error_message, 16000) or "Unknown runtime error"
    clean_response_excerpt = truncate_text(response_excerpt, 4000)
    clean_traceback = truncate_text(traceback_text, 32000)
    clean_provider_name = truncate_text(provider_name, 200)
    clean_provider_code = truncate_text(
        redact_diagnostic_identifier_for_persistence(
            "provider_error_code",
            str(provider_error_code or ""),
            project_id=None,
            job_id=str(job_id),
            context_type="display",
        ),
        200,
    )
    clean_provider_type = truncate_text(
        redact_diagnostic_identifier_for_persistence(
            "provider_error_type",
            str(provider_error_type or ""),
            project_id=None,
            job_id=str(job_id),
            context_type="display",
        ),
        200,
    )
    clean_request_id = truncate_text(
        redact_diagnostic_identifier_for_persistence(
            "request_id",
            str(request_id or ""),
            project_id=None,
            job_id=str(job_id),
            context_type="display",
        ),
        200,
    )
    clean_agent_id = truncate_text(agent_id, 120)
    clean_group_id = truncate_text(group_id, 120)
    clean_step_id = truncate_text(step_id, 120)
    clean_step_execution_id = _step_execution_id(step_execution_id)
    clean_message_id = str(message_id or "").strip() or None
    clean_context = context_jsonb if isinstance(context_jsonb, dict) else {}

    clean_http_status = None
    if http_status is not None:
        try:
            candidate = int(http_status)
        except (TypeError, ValueError):
            candidate = None
        if candidate is not None and 100 <= candidate <= 599:
            clean_http_status = candidate

    if retryable is None and clean_http_status is not None:
        retryable = clean_http_status == 429 or clean_http_status >= 500

    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO job_errors (
                job_id,
                message_id,
                agent_id,
                group_id,
                step_id,
                step_execution_id,
                source,
                category,
                provider_name,
                http_status,
                provider_error_code,
                provider_error_type,
                request_id,
                retryable,
                error_message,
                response_excerpt,
                traceback,
                context_jsonb
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING *
            """,
            (
                job_id,
                clean_message_id,
                clean_agent_id,
                clean_group_id,
                clean_step_id,
                clean_step_execution_id,
                normalized_source,
                normalized_category,
                clean_provider_name,
                clean_http_status,
                clean_provider_code,
                clean_provider_type,
                clean_request_id,
                retryable,
                clean_message,
                clean_response_excerpt,
                clean_traceback,
                json.dumps(clean_context),
            ),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def list_job_errors(
    *,
    conn,
    table_exists,
    job_id: str,
    limit: int = 200,
) -> list[dict[str, Any]]:
    if not table_exists("job_errors"):
        return []
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT *
            FROM job_errors
            WHERE job_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            (job_id, max(1, min(int(limit or 200), 2000))),
        )
        rows = cur.fetchall()
    return [dict(row) for row in rows]


def infer_exception_hints(
    error: BaseException | str | None,
    *,
    context: dict | None = None,
) -> tuple[str, str]:
    """Infer source/category hints for normalize_exception_error."""
    err_obj = error if isinstance(error, BaseException) else None
    text_parts = [str(error or "")]
    if err_obj is not None:
        text_parts.append(str(err_obj.__class__.__name__ or ""))
        text_parts.append(str(getattr(err_obj.__class__, "__module__", "") or ""))
        response_obj = getattr(err_obj, "response", None)
        if response_obj is not None:
            text_parts.append(str(getattr(response_obj, "text", "") or ""))
    if isinstance(context, dict):
        try:
            text_parts.append(json.dumps(context, sort_keys=True))
        except Exception:
            text_parts.append(str(context))
    lowered = " ".join(text_parts).lower()

    provider_tokens = ("openai", "anthropic", "gemini", "googleapi", "x-request-id", "provider")
    if any(token in lowered for token in provider_tokens):
        return "provider", "provider"

    tool_tokens = (
        "tool_call",
        "tool error",
        "tool failed",
        "tool_execution",
        "mcp",
        "playwright",
        "figma",
    )
    if any(token in lowered for token in tool_tokens):
        return "tool", "tool"

    return "system", "unknown"


def normalize_exception_error(
    error: BaseException | str | None,
    *,
    source_hint: str = "unknown",
    category_hint: str = "unknown",
    context: dict | None = None,
    normalize_error_source,
    normalize_error_category,
    truncate_text,
) -> dict[str, Any]:
    """Normalize runtime exception/object fields into job_errors-compatible payload."""
    err_obj = error if isinstance(error, BaseException) else None
    err_message = ""
    traceback_text = None
    if err_obj is not None:
        err_message = f"{err_obj.__class__.__name__}: {err_obj}"
        try:
            traceback_text = "".join(
                traceback.format_exception(type(err_obj), err_obj, err_obj.__traceback__)
            ).strip()
        except Exception:
            traceback_text = None
    else:
        err_message = str(error or "").strip()

    status_candidates = [
        getattr(err_obj, "status_code", None),
        getattr(err_obj, "http_status", None),
        getattr(getattr(err_obj, "response", None), "status_code", None),
    ]
    http_status = None
    for candidate in status_candidates:
        if candidate is None:
            continue
        try:
            parsed = int(str(candidate).strip())
        except (TypeError, ValueError):
            continue
        if 100 <= parsed <= 599:
            http_status = parsed
            break

    request_id_candidates = [
        getattr(err_obj, "request_id", None),
        getattr(err_obj, "x_request_id", None),
    ]
    response_obj = getattr(err_obj, "response", None)
    response_headers = getattr(response_obj, "headers", None)
    if isinstance(response_headers, dict):
        for key in ("x-request-id", "request-id", "openai-request-id"):
            header_value = response_headers.get(key)
            if header_value:
                request_id_candidates.append(header_value)
    request_id = None
    for candidate in request_id_candidates:
        if candidate and str(candidate).strip():
            request_id = str(candidate).strip()
            break

    provider_name = truncate_text(getattr(err_obj, "provider_name", None), 120) or truncate_text(
        getattr(err_obj, "provider", None), 120
    )
    if not provider_name and err_obj is not None:
        module_name = str(getattr(err_obj.__class__, "__module__", "") or "")
        if "openai" in module_name.lower():
            provider_name = "openai"
        elif "anthropic" in module_name.lower():
            provider_name = "anthropic"
        elif "google" in module_name.lower():
            provider_name = "google"

    provider_error_code = truncate_text(getattr(err_obj, "code", None), 120) or truncate_text(
        getattr(err_obj, "error_code", None), 120
    )
    provider_error_type = (
        truncate_text(getattr(err_obj, "type", None), 120)
        or truncate_text(getattr(err_obj, "error_type", None), 120)
        or (err_obj.__class__.__name__ if err_obj is not None else None)
    )

    response_excerpt = None
    response_text = getattr(response_obj, "text", None)
    if response_text:
        response_excerpt = truncate_text(response_text, 4000)
    if response_excerpt is None:
        body = getattr(response_obj, "body", None)
        if body:
            response_excerpt = truncate_text(body, 4000)

    retryable_attr = getattr(err_obj, "retryable", None)
    retryable = bool(retryable_attr) if isinstance(retryable_attr, bool) else None
    if retryable is None and http_status is not None:
        retryable = http_status == 429 or http_status >= 500

    source = normalize_error_source(source_hint)
    if source == "unknown":
        if http_status is not None:
            source = "http"
        elif provider_name:
            source = "provider"
        elif err_obj is not None:
            source = "system"

    category = normalize_error_category(category_hint)
    if category == "unknown":
        if source == "provider":
            category = "provider"
        elif source == "tool":
            category = "tool"
        elif source == "http" and retryable:
            category = "transient"

    return {
        "source": source,
        "category": category,
        "provider_name": provider_name,
        "http_status": http_status,
        "provider_error_code": provider_error_code,
        "provider_error_type": provider_error_type,
        "request_id": request_id,
        "retryable": retryable,
        "error_message": err_message or "Unknown runtime error",
        "response_excerpt": response_excerpt,
        "traceback_text": traceback_text,
        "context_jsonb": context if isinstance(context, dict) else {},
    }
