"""Scoped connector connection resolution, validation and session persistence."""

from __future__ import annotations

import logging
from typing import Any

from core.application.orchestration.connector_mapping import _to_object

logger = logging.getLogger(__name__)


def _connection_id_from_binding(connection_binding: dict[str, Any]) -> str:
    for key in ("connection_id", "project_connection_id", "connection_ref"):
        value = str(connection_binding.get(key) or "").strip()
        if value:
            return value
    return ""


def _operation_declares_connection_ref(extension: dict[str, Any], operation_id: str) -> bool:
    """True when the operation contract declares a ``connection_ref`` input.

    The extension UI action path injects the resolved connection id under that name,
    so workflow steps must do the same or such extensions reject the call with
    "A required field is missing".
    """
    from core.application.extensions.operation_service import contract_action_input_names

    names = contract_action_input_names(extension, operation_id)
    return names is not None and "connection_ref" in names


class ConnectionResolutionError(RuntimeError):
    """Raised when an explicit workflow connection binding cannot be resolved."""


def _load_runtime_connection(
    bl: Any,
    *,
    connection_id: str,
    runtime_context: dict[str, Any],
    extension_id: str = "",
    connector_install_id: str = "",
) -> dict[str, Any]:
    if bl is None or not connection_id:
        return {}
    project_id = str(runtime_context.get("project_id") or "").strip()
    account_id = str(runtime_context.get("account_id") or "").strip()
    row = None
    getter = getattr(bl, "get_project_connection", None)
    if callable(getter):
        try:
            row = getter(
                project_id=project_id or None,
                connection_id=connection_id,
                enabled_only=True,
            )
        except Exception:
            row = None
    if row is None and hasattr(bl, "cursor"):
        try:
            from core.infrastructure.db import project_connection_store

            row = project_connection_store.get_project_connection_row(bl, connection_id)
        except Exception:
            row = None
    connection = _to_object(row)
    if not connection:
        return {}
    if project_id and str(connection.get("project_id") or "").strip() != project_id:
        return {}
    if account_id and str(connection.get("account_id") or "").strip() != account_id:
        return {}
    if connection.get("enabled") is False:
        return {}
    expected_extension_id = str(extension_id or "").strip()
    expected_install_id = str(connector_install_id or "").strip()
    actual_extension_id = str(connection.get("extension_id") or "").strip()
    actual_install_id = str(connection.get("extension_install_id") or "").strip()
    connection_kind = str(connection.get("connection_kind") or "extension").strip()
    if connection_kind == "extension_runtime":
        if expected_install_id and actual_install_id != expected_install_id:
            return {}
    else:
        if expected_extension_id and actual_extension_id != expected_extension_id:
            return {}
        if expected_install_id and actual_install_id and actual_install_id != expected_install_id:
            return {}
        if expected_install_id and not actual_install_id:
            return {}
    return connection


def _connection_session_scope(connection_binding: dict[str, Any]) -> dict[str, Any]:
    scope = _to_object(connection_binding.get("session_scope"))
    scopes = connection_binding.get("scopes")
    if isinstance(scopes, list):
        scope["scopes"] = [str(item or "").strip() for item in scopes if str(item or "").strip()]
    return scope


def _build_runtime_connection_target(
    bl: Any,
    *,
    connection_binding: dict[str, Any],
    runtime_context: dict[str, Any],
    extension_id: str = "",
    connector_install_id: str = "",
    required_connection_type_ids: frozenset[str] = frozenset(),
    required_oauth_scopes: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], dict[str, Any]]:
    connection_id = _connection_id_from_binding(connection_binding)
    if not connection_id:
        return {}, {}
    connection = _load_runtime_connection(
        bl,
        connection_id=connection_id,
        runtime_context=runtime_context,
        extension_id=extension_id,
        connector_install_id=connector_install_id,
    )
    if not connection:
        raise ConnectionResolutionError(
            f"connection_id '{connection_id}' is missing, disabled, or outside runtime scope"
        )

    granted_scopes = connection.get("oauth_granted_scopes")
    normalized_granted_scopes = (
        {
            str(value).strip()
            for value in granted_scopes
            if isinstance(value, str) and str(value).strip()
        }
        if isinstance(granted_scopes, list)
        else set()
    )
    if (
        required_connection_type_ids
        and str(connection.get("connection_type") or "").strip() not in required_connection_type_ids
    ) or not set(required_oauth_scopes).issubset(normalized_granted_scopes):
        raise ConnectionResolutionError(f"connection_runtime_requirement_failed:{connection_id}")

    secrets: dict[str, str] = {}
    session: dict[str, Any] | None = None
    if hasattr(bl, "cursor"):
        try:
            from core.application.management.project_connection_secrets import (
                resolve_connection_secrets,
            )

            secrets = resolve_connection_secrets(
                bl,
                account_id=str(connection.get("account_id") or "").strip(),
                secret_refs=connection.get("secret_refs"),
            )
        except Exception as exc:
            raise ConnectionResolutionError(
                f"connection_secret_resolution_failed:{connection_id}:{exc}"
            ) from exc
        try:
            if (
                str(connection.get("oauth_configuration_id") or "").strip()
                and str(connection.get("oauth_provider_id") or "").strip()
            ):
                from core.application.extensions.operation_targets import (
                    _resolve_extension_owned_oauth_runtime,
                )

                oauth_secrets = _resolve_extension_owned_oauth_runtime(
                    bl,
                    connection=connection,
                )
            else:
                from core.application.auth.oauth_client import (
                    resolve_oauth_secrets_for_runtime,
                )

                oauth_secrets = resolve_oauth_secrets_for_runtime(bl, connection=connection)
        except Exception as exc:
            raise ConnectionResolutionError(
                f"connection_oauth_resolution_failed:{connection_id}:{exc}"
            ) from exc
        if oauth_secrets:
            secrets.update(oauth_secrets)
        try:
            from core.application.management.project_connection_sessions import (
                get_connection_session,
            )

            session = get_connection_session(
                bl,
                connection=connection,
                scope=_connection_session_scope(connection_binding),
                subject_key=str(connection_binding.get("subject_key") or "").strip(),
            )
        except Exception:
            session = None

    runtime_payload = {
        "connection_id": str(connection.get("connection_id") or "").strip(),
        "account_id": str(connection.get("account_id") or "").strip(),
        "project_id": str(connection.get("project_id") or "").strip(),
        "display_name": str(connection.get("display_name") or "").strip(),
        "connection_type": str(connection.get("connection_type") or "").strip(),
        "environment": str(connection.get("environment") or "").strip(),
        "credential_revision": int(connection.get("credential_revision") or 1),
        "config": _to_object(connection.get("connection_config")),
        "secret_refs": _to_object(connection.get("secret_refs")),
        "secrets": secrets,
        "session": session or {},
    }
    logged_payload = {
        key: value for key, value in runtime_payload.items() if key not in {"secrets", "session"}
    }
    logged_payload["secrets"] = dict.fromkeys(secrets, "<redacted>")
    logged_payload["session"] = {
        "present": bool(session and session.get("payload")),
        "expires_at": (session or {}).get("expires_at"),
        "refresh_after": (session or {}).get("refresh_after"),
    }
    return runtime_payload, logged_payload


def _session_update_from_invocation(invocation: dict[str, Any]) -> dict[str, Any]:
    response = _to_object(invocation.get("response"))
    result = _to_object(response.get("result"))
    candidates = [
        response.get("session_update"),
        response.get("session"),
        result.get("session_update"),
        result.get("session"),
    ]
    for candidate in candidates:
        payload = _to_object(candidate)
        if payload:
            return payload
    return {}


def _strip_connection_session_updates(invocation: dict[str, Any]) -> None:
    response = invocation.get("response")
    if not isinstance(response, dict):
        return
    response.pop("session_update", None)
    response.pop("session", None)
    result = response.get("result")
    if isinstance(result, dict):
        result.pop("session_update", None)
        result.pop("session", None)


def _persist_connection_session_update(
    bl: Any,
    *,
    runtime_connection: dict[str, Any],
    connection_binding: dict[str, Any],
    invocation: dict[str, Any],
    fail_closed: bool = False,
) -> None:
    session_update = _session_update_from_invocation(invocation)
    if not session_update:
        return
    if bl is None or not hasattr(bl, "cursor") or not runtime_connection:
        if fail_closed:
            raise RuntimeError("connection session persistence unavailable")
        return
    session_payload = _to_object(
        session_update.get("payload") or session_update.get("session_payload")
    )
    if not session_payload:
        if fail_closed:
            raise RuntimeError("connection session payload invalid")
        return
    try:
        expires_at = float(session_update.get("expires_at") or 0)
    except (TypeError, ValueError):
        expires_at = 0
    if expires_at <= 0:
        if fail_closed:
            raise RuntimeError("connection session expiry invalid")
        return
    refresh_after = session_update.get("refresh_after")
    try:
        normalized_refresh_after = float(refresh_after) if refresh_after is not None else None
    except (TypeError, ValueError):
        if fail_closed:
            raise RuntimeError("connection session refresh time invalid") from None
        normalized_refresh_after = None
    try:
        from core.application.management.project_connection_sessions import (
            upsert_connection_session,
        )

        upsert_connection_session(
            bl,
            connection=runtime_connection,
            session_payload=session_payload,
            expires_at=expires_at,
            refresh_after=normalized_refresh_after,
            scope=_connection_session_scope(connection_binding),
            subject_key=str(connection_binding.get("subject_key") or "").strip(),
            metadata={"source": "connector_step_runtime"},
        )
    except Exception as exc:
        logger.warning("failed to persist project connection session update: %s", exc)
        if fail_closed:
            raise


def _load_extension_project_state_for_target(
    bl: Any,
    *,
    account_id: str,
    project_id: str,
    extension_id: str,
    extension_install_id: str,
    connection_id: str,
    extension: dict[str, Any] | None = None,
    input_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from core.application.extensions.project_state_hydration import (
        load_extension_project_state_for_target,
    )

    return load_extension_project_state_for_target(
        bl,
        account_id=account_id,
        project_id=project_id,
        extension_id=extension_id,
        extension_install_id=extension_install_id,
        connection_id=connection_id,
        extension=extension,
        input_payload=input_payload,
    )


def _is_validation_only_test_mode(
    *,
    extension: dict[str, Any],
    action_id: str,
    runtime_context: dict[str, Any],
) -> bool:
    if runtime_context.get("test_mode") is not True:
        return False
    from core.application.extensions.external_effects import (
        external_effect_declaration_for_operation,
    )

    declaration = external_effect_declaration_for_operation(extension, action_id)
    if str(declaration.get("test_mode_behavior") or "").strip().lower() == "suppress":
        return True
    return any(
        str(row.get("action_id") or row.get("id") or "").strip() == action_id
        and str(row.get("test_mode_behavior") or "").strip().lower() == "suppress"
        for row in extension.get("action_definitions") or []
        if isinstance(row, dict)
    )


def _test_mode_connection_failure(
    bl: Any,
    *,
    connection_id: str,
    runtime_context: dict[str, Any],
    extension_id: str,
    connector_install_id: str,
    required_connection_type_ids: frozenset[str],
    required_oauth_scopes: frozenset[str],
) -> dict[str, str] | None:
    """Validate an extension binding from non-secret DB metadata before test execution."""
    getter = getattr(bl, "get_project_connection_runtime_metadata", None)
    metadata = None
    if callable(getter):
        try:
            metadata = getter(connection_id)
        except Exception:
            metadata = None
    row = _to_object(metadata)
    oauth_metadata = _to_object(row.get("oauth_metadata"))
    exact_binding = bool(row) and all(
        (
            str(row.get("connection_id") or "").strip() == connection_id,
            str(row.get("project_id") or "").strip()
            == str(runtime_context.get("project_id") or "").strip(),
            str(row.get("account_id") or "").strip()
            == str(runtime_context.get("account_id") or "").strip(),
            str(row.get("extension_id") or "").strip() == extension_id,
            str(row.get("extension_install_id") or "").strip() == connector_install_id,
            not required_connection_type_ids
            or str(row.get("connection_type") or "").strip() in required_connection_type_ids,
            row.get("enabled") is True,
        )
    )
    if exact_binding:
        lifecycle = str(oauth_metadata.get("lifecycle_status") or "").strip().lower()
        runtime_status = str(row.get("runtime_status") or "").strip().lower()
        if (
            (lifecycle and lifecycle != "connected")
            or (required_oauth_scopes and lifecycle != "connected")
            or runtime_status == "unavailable"
        ):
            return {
                "code": "connection_unavailable",
                "message": "The selected extension connection is unavailable.",
            }
        raw_granted = row.get("oauth_granted_scopes")
        granted = (
            {
                str(value).strip()
                for value in raw_granted
                if isinstance(value, str) and str(value).strip()
            }
            if isinstance(raw_granted, list)
            else set()
        )
        if not set(required_oauth_scopes).issubset(granted):
            return {
                "code": "oauth_scope_required",
                "message": "Authorize the scopes required by this extension operation.",
            }
        return None
    return {
        "code": "connection_unavailable",
        "message": "The selected extension connection is unavailable.",
    }
