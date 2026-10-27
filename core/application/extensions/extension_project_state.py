"""Application helpers for non-secret extension project state."""

from __future__ import annotations

import json
from typing import Any, NoReturn

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.db import extension_project_state_store

DEFAULT_MAX_STATE_BYTES = 64 * 1024
MAX_STATE_BYTES_CAP = 1024 * 1024
_SECRETISH_KEYS = {
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
}


class ExtensionProjectStateError(ValueError):
    """Raised when extension project state input is invalid."""


class ExtensionProjectStateRevisionConflict(ExtensionProjectStateError):
    """Raised when optimistic concurrency revision checks fail."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _max_state_bytes() -> int:
    try:
        configured = int(_runtime_env.get("FS_EXTENSION_PROJECT_STATE_MAX_BYTES") or 0)
    except ValueError:
        configured = 0
    if configured <= 0:
        configured = DEFAULT_MAX_STATE_BYTES
    return max(1024, min(configured, MAX_STATE_BYTES_CAP))


def _encoded_size(value: dict[str, Any]) -> int:
    return len(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    )


def _walk_secretish_keys(value: Any, *, path: str = "") -> list[str]:
    hits: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key or "").strip()
            lowered = key_text.lower().replace("-", "_")
            if lowered in _SECRETISH_KEYS or any(
                token in lowered for token in ("password", "secret", "token", "credential")
            ):
                hits.append(f"{path}.{key_text}".strip("."))
            hits.extend(_walk_secretish_keys(item, path=f"{path}.{key_text}".strip(".")))
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            hits.extend(_walk_secretish_keys(item, path=f"{path}[{idx}]"))
    return hits


def _validate_identity(
    *,
    account_id: str,
    project_id: str,
    extension_id: str,
    extension_install_id: str,
    namespace: str,
    state_key: str,
) -> None:
    missing = [
        name
        for name, value in (
            ("account_id", account_id),
            ("project_id", project_id),
            ("extension_id", extension_id),
            ("extension_install_id", extension_install_id),
            ("namespace", namespace),
            ("state_key", state_key),
        )
        if not _text(value)
    ]
    if missing:
        raise ExtensionProjectStateError(
            f"Missing extension project state fields: {', '.join(missing)}"
        )
    for label, value in (("namespace", namespace), ("state_key", state_key)):
        lowered = _text(value).lower().replace("-", "_")
        if lowered in (_SECRETISH_KEYS | {"secrets", "credentials"}) or lowered.startswith(
            ("password:", "secret:", "token:", "credential:")
        ):
            raise ExtensionProjectStateError(f"{label} must not identify secret material")


def _validate_value(value_jsonb: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value_jsonb, dict):
        raise ExtensionProjectStateError("value_jsonb must be an object")
    size = _encoded_size(value_jsonb)
    max_size = _max_state_bytes()
    if size > max_size:
        raise ExtensionProjectStateError(f"value_jsonb exceeds maximum size of {max_size} bytes")
    secretish = _walk_secretish_keys(value_jsonb)
    if secretish:
        raise ExtensionProjectStateError(
            "extension_project_state must not store secrets; rejected keys: "
            + ", ".join(secretish[:5])
        )
    return dict(value_jsonb)


def _raise_ownership_error(exc: Exception) -> NoReturn:
    raise ExtensionProjectStateError(str(exc)) from exc


def get_state(
    db: Any,
    *,
    account_id: str,
    project_id: str,
    extension_id: str,
    extension_install_id: str | None = None,
    namespace: str,
    state_key: str,
    connection_id: str | None = None,
) -> dict[str, Any] | None:
    _validate_identity(
        account_id=account_id,
        project_id=project_id,
        extension_id=extension_id,
        extension_install_id=extension_install_id or "",
        namespace=namespace,
        state_key=state_key,
    )
    try:
        return extension_project_state_store.get_extension_project_state(
            db,
            account_id=_text(account_id),
            project_id=_text(project_id),
            extension_id=_text(extension_id),
            extension_install_id=_text(extension_install_id) or None,
            namespace=_text(namespace),
            state_key=_text(state_key),
            connection_id=_text(connection_id) or None,
        )
    except extension_project_state_store.ExtensionProjectStateOwnershipError as exc:
        _raise_ownership_error(exc)


def put_state(
    db: Any,
    *,
    account_id: str,
    project_id: str,
    extension_id: str,
    extension_install_id: str | None = None,
    namespace: str,
    state_key: str,
    value_jsonb: dict[str, Any],
    connection_id: str | None = None,
    expected_revision: int | None = None,
    expires_at: str | None = None,
    actor_id: str | None = None,
) -> dict[str, Any]:
    _validate_identity(
        account_id=account_id,
        project_id=project_id,
        extension_id=extension_id,
        extension_install_id=extension_install_id or "",
        namespace=namespace,
        state_key=state_key,
    )
    value = _validate_value(value_jsonb)
    try:
        row = extension_project_state_store.put_extension_project_state(
            db,
            account_id=_text(account_id),
            project_id=_text(project_id),
            extension_id=_text(extension_id),
            extension_install_id=_text(extension_install_id) or None,
            namespace=_text(namespace),
            state_key=_text(state_key),
            value_jsonb=value,
            connection_id=_text(connection_id) or None,
            expected_revision=expected_revision,
            expires_at=_text(expires_at) or None,
            actor_id=_text(actor_id) or None,
        )
    except extension_project_state_store.ExtensionProjectStateOwnershipError as exc:
        _raise_ownership_error(exc)
    if row is None:
        raise ExtensionProjectStateRevisionConflict(
            f"State revision conflict for {_text(extension_id)}/{_text(namespace)}/{_text(state_key)}"
        )
    return row


def list_state(
    db: Any,
    *,
    account_id: str,
    project_id: str,
    extension_id: str,
    extension_install_id: str | None = None,
    namespace: str | None = None,
    connection_id: str | None = None,
    include_all_connections: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    if not _text(account_id) or not _text(project_id) or not _text(extension_id):
        raise ExtensionProjectStateError("account_id, project_id, and extension_id are required")
    try:
        return extension_project_state_store.list_extension_project_state(
            db,
            account_id=_text(account_id),
            project_id=_text(project_id),
            extension_id=_text(extension_id),
            extension_install_id=_text(extension_install_id) or None,
            namespace=_text(namespace) or None,
            connection_id=_text(connection_id) or None,
            include_all_connections=include_all_connections,
            limit=limit,
        )
    except extension_project_state_store.ExtensionProjectStateOwnershipError as exc:
        _raise_ownership_error(exc)


def delete_state(
    db: Any,
    *,
    account_id: str,
    project_id: str,
    extension_id: str,
    extension_install_id: str | None = None,
    namespace: str,
    state_key: str,
    connection_id: str | None = None,
    expected_revision: int | None = None,
) -> int:
    _validate_identity(
        account_id=account_id,
        project_id=project_id,
        extension_id=extension_id,
        extension_install_id=extension_install_id or "",
        namespace=namespace,
        state_key=state_key,
    )
    try:
        deleted = extension_project_state_store.delete_extension_project_state(
            db,
            account_id=_text(account_id),
            project_id=_text(project_id),
            extension_id=_text(extension_id),
            extension_install_id=_text(extension_install_id) or None,
            namespace=_text(namespace),
            state_key=_text(state_key),
            connection_id=_text(connection_id) or None,
            expected_revision=expected_revision,
        )
    except extension_project_state_store.ExtensionProjectStateOwnershipError as exc:
        _raise_ownership_error(exc)
    if expected_revision is not None and deleted == 0:
        raise ExtensionProjectStateRevisionConflict(
            f"State revision conflict for {_text(extension_id)}/{_text(namespace)}/{_text(state_key)}"
        )
    return deleted
