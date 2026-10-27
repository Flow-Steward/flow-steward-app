"""Extension provider attachments operations."""

from __future__ import annotations

import base64
import os
from typing import Any

from core.application.extensions.provider_runtime import (
    DEFAULT_MIME_TYPE,
    _as_dict,
    _as_str,
    _default_account_id,
    _guess_attachment_filename,
    _process_provider_project_state_updates,
    resolve_extension_runtime_config,
)


def fetch_provider_attachments(
    provider_key: str,
    *,
    attachments: list[dict[str, Any]] | None,
    project_config: dict | None,
    bl=None,
    project_id: int | str | None = None,
    task_id: str = "",
    metadata: dict[str, Any] | None = None,
    trigger_config: dict[str, Any] | None = None,
    allowed_mimes: set[str] | None = None,
    allowed_extensions: set[str] | None = None,
    max_size_bytes: int = 20 * 1024 * 1024,
    max_files: int = 10,
    scope: Any = None,
) -> list[dict[str, Any]]:
    normalized_provider_key = _as_str(provider_key).lower()
    declared = [_as_dict(item) for item in (attachments or []) if isinstance(item, dict)]
    if not normalized_provider_key or not declared:
        return []

    trimmed = declared[: max(0, int(max_files or 0))] if max_files else declared
    candidates: list[dict[str, Any]] = []
    allowed_mime_set = {
        str(item).strip().lower() for item in (allowed_mimes or set()) if str(item).strip()
    }
    allowed_extension_set = {
        str(item).strip().lower() for item in (allowed_extensions or set()) if str(item).strip()
    }
    for raw in trimmed:
        filename = _guess_attachment_filename(raw)
        mime_type = (
            _as_str(raw.get("mime_type") or raw.get("mimetype") or DEFAULT_MIME_TYPE)
            or DEFAULT_MIME_TYPE
        )
        size_hint = raw.get("size_bytes")
        if size_hint in (None, ""):
            size_hint = raw.get("size")
        try:
            size_bytes = int(size_hint or 0)
        except (TypeError, ValueError):
            size_bytes = 0
        extension = os.path.splitext(filename)[1].lower()
        if size_bytes > 0 and max_size_bytes > 0 and size_bytes > int(max_size_bytes):
            continue
        if (
            allowed_extension_set
            and allowed_mime_set
            and extension not in allowed_extension_set
            and mime_type.lower() not in allowed_mime_set
        ):
            continue
        candidates.append(
            {
                "attachment_id": _as_str(raw.get("attachment_id") or raw.get("id")),
                "filename": filename,
                "mime_type": mime_type,
                "url": _as_str(raw.get("url")),
                "size_bytes": size_bytes,
            }
        )

    if not candidates:
        return []
    if bl is None or not hasattr(bl, "get_extension"):
        raise RuntimeError("Extension runtime is not available")

    extension = bl.get_extension(normalized_provider_key)
    if not extension:
        raise RuntimeError(f"Extension '{normalized_provider_key}' is not available")
    if not bool(extension.get("enabled", True)):
        raise RuntimeError(f"Extension '{normalized_provider_key}' is disabled")

    from core.infrastructure.db.ingress_runtime import (
        format_task_ingress_error,
        normalize_task_ingress_response,
    )
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    resolved_config = resolve_extension_runtime_config(
        normalized_provider_key,
        project_config,
        bl=bl,
        project_id=project_id,
    )
    if not bool(resolved_config.get("enabled", False)):
        raise RuntimeError(
            f"Extension '{normalized_provider_key}' is not configured for attachment fetch"
        )

    runtime_context: dict[str, Any] = {}
    if getattr(scope, "account_id", None):
        runtime_context["account_id"] = scope.account_id
    if getattr(scope, "workflow_id", None):
        runtime_context["workflow_id"] = scope.workflow_id
    if getattr(scope, "actor_id", None):
        runtime_context["actor_id"] = scope.actor_id
    if getattr(scope, "principal_type", None):
        runtime_context["actor_type"] = scope.principal_type

    invocation = run_extension_action(
        extension,
        action_id="fetch_attachments",
        input_payload={
            "task_id": _as_str(task_id),
            "attachments": candidates,
            "metadata": _as_dict(metadata),
            "trigger_config": _as_dict(trigger_config),
            "provider": {
                "provider_key": normalized_provider_key,
                "provider_config": _as_dict(resolved_config.get("config")),
            },
        },
        runtime_context=runtime_context or None,
    )
    project_connection = _as_dict(resolved_config.get("project_connection"))
    _process_provider_project_state_updates(
        invocation,
        extension=extension,
        db=bl,
        account_id=(
            _as_str(getattr(scope, "account_id", None))
            or _as_str(project_connection.get("account_id"))
            or _as_str(resolved_config.get("account_id"))
            or _as_str(extension.get("account_id"))
            or _default_account_id()
        ),
        project_id=(
            _as_str(getattr(scope, "project_id", None))
            or _as_str(project_id)
            or _as_str(resolved_config.get("project_id"))
        ),
        connection_id=_as_str(project_connection.get("connection_id")) or None,
        actor_id=_as_str(getattr(scope, "actor_id", None)) or None,
    )
    if invocation.get("status") != "succeeded":
        raise RuntimeError(
            format_task_ingress_error(
                {"code": "runtime_error", "message": invocation.get("detail")},
                fallback="Attachment fetch invocation failed",
            )
        )

    response = normalize_task_ingress_response(
        _as_dict(invocation.get("response")),
        default_error_message="Attachment fetch action returned ok=false",
    )
    if not bool(response.get("ok", False)):
        errors = response.get("errors") if isinstance(response.get("errors"), list) else []
        first_error = errors[0] if errors else {}
        raise RuntimeError(
            format_task_ingress_error(
                first_error,
                fallback="Attachment fetch action returned ok=false",
            )
        )

    result = _as_dict(response.get("result"))
    if not isinstance(result.get("attachments"), list):
        raise RuntimeError(
            format_task_ingress_error(
                {
                    "code": "invalid_response",
                    "message": "Attachment fetch response is missing result.attachments list",
                },
                fallback="Attachment fetch response is invalid",
            )
        )
    rows: list[dict[str, Any]] = []
    for raw in result.get("attachments") or []:
        item = _as_dict(raw)
        encoded = _as_str(item.get("content_base64"))
        if not encoded:
            continue
        try:
            content = base64.b64decode(encoded)
        except Exception as exc:
            raise RuntimeError(
                f"Attachment fetch returned invalid content for {item.get('filename') or 'attachment'}"
            ) from exc
        filename = _guess_attachment_filename(item)
        mime_type = (
            _as_str(item.get("mime_type") or item.get("mimetype") or DEFAULT_MIME_TYPE)
            or DEFAULT_MIME_TYPE
        )
        extension = os.path.splitext(filename)[1].lower()
        if (
            allowed_extension_set
            and allowed_mime_set
            and extension not in allowed_extension_set
            and mime_type.lower() not in allowed_mime_set
        ):
            continue
        if max_size_bytes > 0 and len(content) > int(max_size_bytes):
            continue
        rows.append(
            {
                "attachment_id": _as_str(item.get("attachment_id") or item.get("id")),
                "filename": filename,
                "mime_type": mime_type,
                "url": _as_str(item.get("url")),
                "size_bytes": int(item.get("size_bytes") or len(content) or 0),
                "content": content,
            }
        )
    return rows
