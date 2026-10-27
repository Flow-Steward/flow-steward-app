"""Runtime-aware execution for persisted data connections."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.infrastructure.db.platform_connection_actions import run_platform_connection_action
from core.infrastructure.extension_runtime.extension_runtime import run_extension_action
from core.infrastructure.extension_runtime.extension_runtime_helpers import (
    resolve_extension_runtime_record,
    resolve_extension_runtime_record_by_install_id,
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _object(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _failed(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "result": {}, "errors": [{"code": code, "message": message}]}


def _runtime_kind(connection: Mapping[str, Any]) -> str:
    ref = _object(connection.get("connector_ref_jsonb") or connection.get("connector_ref"))
    explicit = _text(connection.get("runtime_kind") or ref.get("runtime_kind")).lower()
    if explicit:
        return explicit
    if _text(ref.get("connector_id")).startswith("platform."):
        return "platform"
    return "local_extension"


def _resolved_secret_refs(db: Any, connection: Mapping[str, Any]) -> dict[str, Any]:
    refs = _object(connection.get("secret_refs_jsonb") or connection.get("secret_refs"))
    if db is None:
        return refs
    from core.application.orchestration.dataset_query_step_runtime import (
        _resolved_secret_refs as resolve,
    )

    return _object(resolve(db, dict(connection)))


def _response_from_invocation(invocation: Any) -> dict[str, Any]:
    payload = _object(invocation)
    response = _object(payload.get("response"))
    if payload.get("status") == "succeeded":
        return response or {"ok": True, "result": {}, "errors": []}
    errors = list(response.get("errors") or [])
    errors.extend(item for item in payload.get("errors") or [] if isinstance(item, Mapping))
    detail = _text(payload.get("detail"))
    if detail and not errors:
        errors.append({"code": detail, "message": detail})
    return {"ok": False, "result": _object(response.get("result")), "errors": errors}


def _process_project_state_updates(
    invocation: dict[str, Any],
    *,
    extension: dict[str, Any],
    db: Any,
    connection: Mapping[str, Any],
) -> None:
    if invocation.get("status") != "succeeded" or db is None:
        return
    try:
        from core.application.auth.config import DEFAULT_ACCOUNT_ID
        from core.application.extensions.response_envelope import process_project_state_updates

        extension_id = _text(extension.get("extension_key") or extension.get("extension_id"))
        process_project_state_updates(
            invocation,
            extension=extension,
            extension_id=extension_id,
            db=db,
            account_id=_text(connection.get("account_id")) or DEFAULT_ACCOUNT_ID,
            project_id=_text(connection.get("project_id")),
            connection_id=_text(connection.get("connection_id")) or None,
            actor_id=None,
            extension_install_id=_text(connection.get("extension_install_id")) or None,
        )
    except Exception as exc:
        invocation["status"] = "failed"
        invocation["detail"] = "project_state_update_failed"
        invocation["errors"] = [
            *list(invocation.get("errors") or []),
            {"code": "project_state_update_failed", "message": str(exc)},
        ]


def _resolve_extension(connection: Mapping[str, Any], db: Any) -> dict[str, Any] | None:
    ref = _object(connection.get("connector_ref_jsonb") or connection.get("connector_ref"))
    account_id = _text(connection.get("account_id")) or None
    get_logger_only = None
    dsn = _text(getattr(db, "dsn", "") if db is not None else "")
    if dsn:
        from core.infrastructure.db.platform_stores import PlatformIdentityStore

        def get_logger_only() -> PlatformIdentityStore:
            return PlatformIdentityStore(dsn)

    install_id = _text(ref.get("install_id") or connection.get("extension_install_id"))
    candidates = (
        [install_id]
        if install_id
        else [
            _text(ref.get(key)) for key in ("extension_key", "extension_id", "source_extension_id")
        ]
    )
    for candidate in candidates:
        if not candidate:
            continue
        resolver = (
            resolve_extension_runtime_record_by_install_id
            if candidate == install_id
            else resolve_extension_runtime_record
        )
        extension, _logger = resolver(
            candidate, get_logger_only=get_logger_only, account_id=account_id
        )
        if extension:
            return _object(extension)
    return None


def execute_connection_action(
    connection: Mapping[str, Any],
    action_id: str,
    *,
    db: Any = None,
    source: str = "data_connection",
) -> dict[str, Any]:
    runtime_kind = _runtime_kind(connection)
    if runtime_kind == "platform":
        secret_refs = _resolved_secret_refs(db, connection)
        return run_platform_connection_action(connection, action_id, secret_refs=secret_refs)
    if runtime_kind not in {"local_extension", "remote_extension"}:
        return _failed("connector_runtime_unavailable", "Data connector is unavailable.")
    try:
        extension = _resolve_extension(connection, db)
    except ValueError:
        return _failed(
            "connector_binding_invalid",
            "The selected data connector is invalid. Save the connection again.",
        )
    if not extension:
        return _failed("connector_runtime_unavailable", "Data connector is unavailable.")
    if extension.get("enabled") is False:
        return _failed(
            "extension_disabled",
            "This extension is disabled—ask an admin to enable it.",
        )
    secret_refs = _resolved_secret_refs(db, connection)
    invocation = run_extension_action(
        extension,
        action_id=action_id,
        context={
            "workspace_id": _text(connection.get("workspace_id")),
            "connection_id": _text(connection.get("connection_id")),
        },
        input_payload={
            "connection_config": _object(
                connection.get("connection_config_jsonb") or connection.get("connection_config")
            ),
            "secret_refs": secret_refs,
        },
        runtime_context={"source": source},
        timeout_seconds=30,
    )
    if isinstance(invocation, dict):
        _process_project_state_updates(
            invocation,
            extension=extension,
            db=db,
            connection=connection,
        )
    return _response_from_invocation(invocation)
