"""Extension provider runtime operations."""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import unquote, urlparse

from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID

logger = logging.getLogger(__name__)
DEFAULT_MIME_TYPE = "application/octet-stream"


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_str(value: Any) -> str:
    return str(value or "").strip()


def _normalize_string_list(value: Any) -> list[str]:
    normalized: list[str] = []
    for item in value or []:
        text = str(item or "").strip()
        if text:
            normalized.append(text)
    return list(dict.fromkeys(normalized))


def _connection_secret_refs(value: Any) -> list[str] | dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return _normalize_string_list(value)


def _guess_attachment_filename(attachment: dict[str, Any]) -> str:
    for key in ("filename", "title", "name"):
        candidate = _as_str(attachment.get(key))
        if candidate:
            return candidate
    url = _as_str(attachment.get("url"))
    if not url:
        return ""
    parsed = urlparse(url)
    return os.path.basename(unquote(parsed.path or ""))


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _default_account_id() -> str:
    return DEFAULT_ACCOUNT_ID


def _extension_runtime_id(extension: dict[str, Any] | None) -> str:
    payload = _as_dict(extension)
    compiled = _as_dict(payload.get("compiled_manifest"))
    return (
        _as_str(payload.get("extension_key"))
        or _as_str(payload.get("extension_id"))
        or _as_str(compiled.get("extension_id"))
    )


def _process_provider_project_state_updates(
    invocation: dict[str, Any],
    *,
    extension: dict[str, Any],
    db: Any,
    account_id: str,
    project_id: int | str | None,
    connection_id: str | None = None,
    actor_id: str | None = None,
) -> None:
    if invocation.get("status") != "succeeded":
        return
    try:
        from core.application.extensions.response_envelope import process_project_state_updates

        process_project_state_updates(
            invocation,
            extension=extension,
            extension_id=_extension_runtime_id(extension),
            db=db,
            account_id=_as_str(account_id) or _default_account_id(),
            project_id=_as_str(project_id),
            connection_id=_as_str(connection_id) or None,
            actor_id=_as_str(actor_id) or None,
        )
    except Exception as exc:
        invocation["status"] = "failed"
        invocation["detail"] = "project_state_update_failed"
        invocation["errors"] = [
            *list(invocation.get("errors") or []),
            {"code": "project_state_update_failed", "message": str(exc)},
        ]


def _project_id_from_config(
    project_config: dict | None, explicit_project_id: int | str | None = None
) -> str | None:
    if explicit_project_id is not None:
        normalized = _as_str(explicit_project_id)
        return normalized or None
    config = _as_dict(project_config)
    normalized = _as_str(config.get("project_id"))
    return normalized or None


def _connection_id_from_config(project_config: dict | None) -> str:
    config = _as_dict(project_config)
    for key in ("connection_id", "project_connection_id", "connection_ref"):
        normalized = _as_str(config.get(key))
        if normalized:
            return normalized
    return ""


def principal_scope_context(
    principal: dict | None,
    *,
    project_id: int | str | None = None,
    workflow_id: str | None = None,
) -> dict:
    payload = _as_dict(principal)
    allowed_project_ids: set[str] = set()
    for item in payload.get("allowed_project_ids") or []:
        normalized = _as_str(item)
        if normalized:
            allowed_project_ids.add(normalized)
    allowed_workflow_ids = {
        str(item or "").strip()
        for item in (payload.get("allowed_workflow_ids") or [])
        if str(item or "").strip()
    }
    requested_project_id = _as_str(project_id) or None
    requested_workflow_id = str(workflow_id or "").strip() or None
    return {
        "account_id": str(payload.get("account_id") or _default_account_id()).strip()
        or _default_account_id(),
        "allowed_project_ids": sorted(allowed_project_ids),
        "allowed_workflow_ids": sorted(allowed_workflow_ids),
        "project_allowed": (
            True
            if requested_project_id is None or not allowed_project_ids
            else requested_project_id in allowed_project_ids
        ),
        "workflow_allowed": (
            True
            if requested_workflow_id is None or not allowed_workflow_ids
            else requested_workflow_id in allowed_workflow_ids
        ),
    }


def get_project_connection(
    project_config: dict | None,
    *,
    extension_id: str = "",
    connection_id: str = "",
    enabled_only: bool = True,
    connection_kind: str = "extension",
    bl: Any = None,
    project_id: int | str | None = None,
) -> dict | None:
    """Resolve a project connection from the DB.  Returns None if not found."""
    _ = connection_kind
    normalized_connection_id = _as_str(connection_id) or _connection_id_from_config(project_config)
    if bl is not None and hasattr(bl, "get_project_connection") and project_id is not None:
        try:
            row = bl.get_project_connection(
                project_id=project_id,
                extension_id=extension_id,
                connection_id=normalized_connection_id,
                enabled_only=enabled_only,
            )
            if row is not None:
                return dict(row)
        except Exception:
            pass
    return None


def get_project_provider_binding(
    project_config: dict | None,
    *,
    provider_key: str = "",
    provider_type_key: str = "",
    enabled_only: bool = True,
) -> dict | None:
    """Resolve a provider binding via project_connections.  Returns None if not found."""
    normalized_key = str(provider_key or "").strip().lower()
    normalized_type = str(provider_type_key or "").strip().lower()
    connection = get_project_connection(
        project_config,
        extension_id=normalized_key,
        enabled_only=enabled_only,
        connection_kind="extension",
    )
    if connection:
        return _project_connection_as_provider_binding(
            connection,
            provider_key=normalized_key,
            provider_type_key=normalized_type,
        )
    return None


def _normalized_binding_hints(binding_hints: dict[str, Any] | None) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in _as_dict(binding_hints).items():
        normalized_key = str(key or "").strip()
        normalized_value = _as_str(value)
        if normalized_key and normalized_value:
            normalized[normalized_key] = normalized_value
    return normalized


def _binding_matches_hints(binding: dict[str, Any], binding_hints: dict[str, Any] | None) -> bool:
    hints = _normalized_binding_hints(binding_hints)
    if not hints:
        return True
    binding_config = _as_dict(binding.get("binding_config"))
    provider_config = _as_dict(binding.get("provider_config"))
    for key, expected in hints.items():
        actual = _as_str(binding_config.get(key) or provider_config.get(key))
        if not actual or actual != expected:
            return False
    return True


def _connection_matches_hints(
    connection: dict[str, Any], binding_hints: dict[str, Any] | None
) -> bool:
    hints = _normalized_binding_hints(binding_hints)
    if not hints:
        return True
    connection_config = _as_dict(connection.get("connection_config"))
    ingress_config = _as_dict(connection.get("ingress_config"))
    for key, expected in hints.items():
        actual = _as_str(connection_config.get(key) or ingress_config.get(key))
        if not actual or actual != expected:
            return False
    return True


def _project_connection_as_provider_binding(
    connection: dict[str, Any],
    *,
    provider_key: str,
    provider_type_key: str,
) -> dict[str, Any]:
    normalized_provider_key = _as_str(provider_key) or _as_str(connection.get("extension_id"))
    normalized_provider_type = _as_str(provider_type_key)
    connection_config = _as_dict(connection.get("connection_config"))
    return {
        "provider_key": normalized_provider_key,
        "provider_type_key": normalized_provider_type,
        "display_name": _as_str(connection.get("extension_id")) or normalized_provider_key,
        "implementation_kind": "extension",
        "integration_key": _as_str(connection.get("extension_id")) or normalized_provider_key,
        "provider_config": dict(connection_config),
        "binding_config": dict(connection_config),
        "enabled": _coerce_bool(connection.get("enabled"), True),
        "provider_enabled": True,
        "binding_source": "project_connection",
        "connection_id": _as_str(connection.get("connection_id")),
    }


def resolve_provider_project_run_config(
    bl,
    *,
    provider_key: str,
    provider_type_key: str = "task_provider",
    explicit_project_id: int | str | None = None,
    binding_hints: dict[str, Any] | None = None,
) -> tuple[str | None, dict | None]:
    normalized_provider_key = _as_str(provider_key).lower()
    normalized_provider_type = _as_str(provider_type_key).lower()
    if not normalized_provider_key:
        return None, None

    if explicit_project_id:
        project_id = _as_str(explicit_project_id)
        if not project_id:
            return None, None
        try:
            run_cfg = bl.get_project_run_config(project_id)
        except Exception:
            return None, None
        connection = get_project_connection(
            run_cfg,
            extension_id=normalized_provider_key,
            enabled_only=True,
            connection_kind="extension",
            bl=bl,
            project_id=project_id,
        )
        if connection and _connection_matches_hints(connection, binding_hints):
            return project_id, run_cfg
        binding = get_project_provider_binding(
            run_cfg,
            provider_key=normalized_provider_key,
            provider_type_key=normalized_provider_type,
            enabled_only=True,
        )
        if not binding or not _binding_matches_hints(binding, binding_hints):
            return None, None
        return project_id, run_cfg

    projects = bl.get_projects() or []
    matches: list[tuple[str, dict]] = []
    for project in projects:
        candidate_project_id = project.get("id")
        if candidate_project_id is None:
            continue
        try:
            run_cfg = bl.get_project_run_config(candidate_project_id)
        except Exception:
            continue
        connection = get_project_connection(
            run_cfg,
            extension_id=normalized_provider_key,
            enabled_only=True,
            connection_kind="extension",
            bl=bl,
            project_id=str(candidate_project_id),
        )
        if connection and _connection_matches_hints(connection, binding_hints):
            matches.append((str(candidate_project_id), run_cfg))
            continue
        binding = get_project_provider_binding(
            run_cfg,
            provider_key=normalized_provider_key,
            provider_type_key=normalized_provider_type,
            enabled_only=True,
        )
        if not binding or not _binding_matches_hints(binding, binding_hints):
            continue
        matches.append((str(candidate_project_id), run_cfg))

    if len(matches) == 1:
        return matches[0]
    return None, None


def _extension_row(bl=None, extension_key: str = "") -> dict:
    if bl is None or not hasattr(bl, "get_extension"):
        return {}
    try:
        row = bl.get_extension(str(extension_key or "").strip())
    except Exception:
        row = None
    return dict(row or {}) if isinstance(row, dict) else {}


def _project_connection_override(project_config: dict | None, extension_key: str) -> dict:
    connection = get_project_connection(
        project_config,
        extension_id=extension_key,
        connection_id=_connection_id_from_config(project_config),
        enabled_only=False,
        connection_kind="extension",
    )
    if not connection:
        return {}
    payload = dict(connection)
    payload["connection_config"] = _as_dict(payload.get("connection_config"))
    payload["ingress_config"] = _as_dict(payload.get("ingress_config"))
    payload["workflow_mappings"] = _as_dict(payload.get("workflow_mappings"))
    payload["secret_refs"] = _connection_secret_refs(payload.get("secret_refs"))
    return payload


def _project_connection_row(
    *,
    extension_key: str,
    project_config: dict | None,
    bl=None,
    project_id: int | str | None = None,
    connection_id: str = "",
) -> dict:
    normalized_connection_id = _as_str(connection_id) or _connection_id_from_config(project_config)
    connection = _project_connection_override(project_config, extension_key)
    if connection:
        return connection
    if bl is None or project_id is None:
        return {}
    getter = getattr(bl, "get_project_connection", None)
    if callable(getter):
        try:
            row = getter(
                project_id=str(project_id),
                extension_id=extension_key,
                connection_id=normalized_connection_id,
                enabled_only=False,
            )
        except Exception:
            row = None
        if isinstance(row, dict):
            payload = dict(row)
            if (
                normalized_connection_id
                and _as_str(payload.get("connection_id")) != normalized_connection_id
            ):
                return {}
            payload["connection_config"] = _as_dict(payload.get("connection_config"))
            payload["ingress_config"] = _as_dict(payload.get("ingress_config"))
            payload["workflow_mappings"] = _as_dict(payload.get("workflow_mappings"))
            payload["secret_refs"] = _connection_secret_refs(payload.get("secret_refs"))
            return payload
    lister = getattr(bl, "list_project_connections", None)
    if callable(lister):
        try:
            rows = lister(str(project_id), enabled_only=False) or []
        except Exception:
            rows = []
        if not isinstance(rows, list):
            rows = []
        for row in rows:
            payload = _as_dict(row)
            if _as_str(payload.get("extension_id")).lower() != _as_str(extension_key).lower():
                continue
            if (
                normalized_connection_id
                and _as_str(payload.get("connection_id")) != normalized_connection_id
            ):
                continue
            payload = dict(payload)
            payload["connection_config"] = _as_dict(payload.get("connection_config"))
            payload["ingress_config"] = _as_dict(payload.get("ingress_config"))
            payload["workflow_mappings"] = _as_dict(payload.get("workflow_mappings"))
            payload["secret_refs"] = _connection_secret_refs(payload.get("secret_refs"))
            return payload
    return {}


def _inject_project_connection_secrets(
    *,
    provider_key: str,
    config: dict[str, Any],
    secret_refs: list[str] | dict[str, Any] | None,
    bl=None,
    project_id: int | str | None = None,
    account_id: str = "",
) -> dict[str, Any]:
    merged = dict(config)
    if isinstance(secret_refs, dict):
        refs = _as_dict(secret_refs.get("refs"))
        if refs and bl is not None and hasattr(bl, "cursor"):
            try:
                from core.application.management.project_connection_secrets import (
                    resolve_connection_secrets,
                )

                values = resolve_connection_secrets(
                    bl,
                    account_id=str(account_id or "").strip(),
                    secret_refs=secret_refs,
                )
            except Exception:
                values = {}
            for secret_key, secret_value in values.items():
                if not _as_str(merged.get(secret_key)):
                    merged[secret_key] = secret_value
            return merged

    if bl is None or project_id is None or not hasattr(bl, "get_provider_secret"):
        return merged
    for secret_key in _normalize_string_list(secret_refs):
        if _as_str(merged.get(secret_key)):
            continue
        try:
            secret_value = _as_str(
                bl.get_provider_secret(
                    provider_key=provider_key,
                    secret_key=secret_key,
                    project_id=str(project_id),
                    include_global=True,
                )
            )
        except Exception:
            secret_value = ""
        if secret_value:
            merged[secret_key] = secret_value
    return merged


def _extension_config_for_project(
    *,
    extension_config: dict,
    project_id: int | str | None,
) -> tuple[dict, str]:
    cfg = dict(extension_config or {})
    source = "extension_config"
    if project_id is None:
        return cfg, source

    project_map = _as_dict(cfg.get("project_overrides") or cfg.get("projects"))
    if not project_map:
        return cfg, source

    override = project_map.get(str(project_id))
    if not isinstance(override, dict):
        override = project_map.get("default")
    if not isinstance(override, dict):
        return cfg, source

    merged = dict(cfg)
    merged.update(dict(override))
    return merged, "extension_project_override"


def resolve_extension_runtime_config(
    extension_key: str,
    project_config: dict | None,
    *,
    bl=None,
    project_id: int | str | None = None,
) -> dict:
    normalized_key = str(extension_key or "").strip()
    resolved_project_id = _project_id_from_config(project_config, explicit_project_id=project_id)

    extension_row = _extension_row(bl=bl, extension_key=normalized_key)
    extension_enabled = bool(extension_row.get("enabled", False)) if extension_row else False
    extension_account_id = (
        str(extension_row.get("account_id") or _default_account_id()).strip()
        or _default_account_id()
    )

    extension_cfg = _as_dict(extension_row.get("config"))
    connection_row = _project_connection_row(
        extension_key=normalized_key,
        project_config=project_config,
        bl=bl,
        project_id=resolved_project_id,
        connection_id=_connection_id_from_config(project_config),
    )
    connection_cfg = _as_dict(connection_row.get("connection_config"))
    secret_refs = connection_row.get("secret_refs")

    merged_cfg = dict(extension_cfg)
    merged_cfg.update(connection_cfg)
    merged_cfg, merged_source = _extension_config_for_project(
        extension_config=merged_cfg,
        project_id=resolved_project_id,
    )
    merged_cfg = _inject_project_connection_secrets(
        provider_key=normalized_key,
        config=merged_cfg,
        secret_refs=secret_refs,
        bl=bl,
        project_id=resolved_project_id,
        account_id=str(connection_row.get("account_id") or extension_account_id).strip(),
    )

    config_enabled = _coerce_bool(merged_cfg.get("enabled"), True)
    connection_enabled = _coerce_bool(connection_row.get("enabled"), True)

    if connection_row:
        enabled = bool(extension_enabled and config_enabled and connection_enabled)
        merged_source = "project_connection"
    elif extension_row:
        enabled = bool(extension_enabled and config_enabled)
    else:
        enabled = bool(config_enabled and merged_cfg)

    return {
        "extension_key": normalized_key,
        "enabled": enabled,
        "account_id": extension_account_id,
        "binding_source": merged_source,
        "project_id": resolved_project_id,
        "config": merged_cfg,
        "project_connection": connection_row or {},
        "secret_refs": secret_refs,
        "connection_routes": list(connection_row.get("connection_routes") or []),
        "ingress_config": _as_dict(connection_row.get("ingress_config")),
        "workflow_mappings": _as_dict(connection_row.get("workflow_mappings")),
    }
