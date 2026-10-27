"""Application-owned project-connection operations used by the runtime adapter."""

from __future__ import annotations

from typing import Any

from core.application.extensions.connection_partition_validation import (
    connection_partition_schema,
    validate_connection_partition,
)
from core.application.extensions.project_connection_ingress import (
    IngressPatchRejected,
    patch_project_connection_ingress,
)
from core.application.extensions.project_connection_secrets import (
    SecretsSaveRejected,
    save_project_connection_secrets,
)
from core.application.extensions.project_connection_state import carried_connection_fields
from core.application.extensions.runtime_host_grants import trusted_mutation_grant


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _connection_for_extension(
    store: Any, *, project_id: str, extension_id: str, extension_install_id: str
) -> dict[str, Any]:
    getter = getattr(store, "get_project_connection", None)
    if callable(getter):
        row = getter(
            project_id=project_id,
            extension_id=extension_id,
            extension_install_id=extension_install_id,
            enabled_only=False,
        )
        if isinstance(row, dict):
            return dict(row)
    return {}


def run_platform_connection_operation(
    operation: str,
    *,
    store: Any,
    project_id: str,
    extension_id: str,
    extension_install_id: str = "",
    account_id: str = "",
    trusted_grant: dict[str, Any] | None = None,
    connection_type: str = "",
    config: dict[str, Any] | None = None,
    input_payload: dict[str, Any] | None = None,
    connection_config: dict[str, Any] | None = None,
    ingress_schema: dict[str, Any] | None = None,
    secret_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a bounded application use case for the infrastructure adapter."""

    try:
        if operation == "config_save":
            existing = _connection_for_extension(
                store,
                project_id=_text(project_id),
                extension_id=_text(extension_id),
                extension_install_id=_text(extension_install_id),
            )
            carried = carried_connection_fields(
                existing,
                extension_install_id=extension_install_id,
                account_id=account_id,
            )
            carried["connection_config"] = _as_dict(connection_config)
            carried["connection_type"] = _text(connection_type) or carried["connection_type"]
            saved = store.upsert_project_connection(
                project_id=_text(project_id),
                extension_id=_text(extension_id),
                **carried,
            )
            return {"ok": True, "outcome": _as_dict(saved)}
        if operation == "ingress_patch":
            outcome = patch_project_connection_ingress(
                store,
                project_id=_text(project_id),
                extension_id=_text(extension_id),
                config=_as_dict(config),
                input_payload=_as_dict(input_payload),
                extension_install_id=_text(extension_install_id),
                connection_type=_text(connection_type),
                account_id=_text(account_id),
                ingress_schema=_as_dict(ingress_schema),
            )
            return {"ok": True, "outcome": outcome}
        if operation == "secrets_save":
            outcome = save_project_connection_secrets(
                store,
                project_id=_text(project_id),
                extension_id=_text(extension_id),
                extension_install_id=_text(extension_install_id),
                account_id=_text(account_id),
                trusted_grant=_as_dict(trusted_grant),
                config=_as_dict(config),
                input_payload=_as_dict(input_payload),
                secret_schema=_as_dict(secret_schema),
            )
            return {"ok": True, "outcome": outcome}
    except IngressPatchRejected as exc:
        return {"ok": False, "error": exc.code, "errors": [str(exc)]}
    except SecretsSaveRejected as exc:
        return {"ok": False, "error": exc.code}
    except Exception:
        return {"ok": False, "error": "project_connection_storage_unavailable"}
    return {"ok": False, "error": "unsupported_platform_connection_operation"}


def run_platform_connection_contract(
    operation: str,
    *,
    extension: dict[str, Any],
    connection_type: str = "",
    partition: str = "",
    value: Any = None,
    operation_id: str = "",
) -> Any:
    """Resolve application-owned schema and mutation policy for the runtime adapter."""
    if operation == "partition_errors":
        return validate_connection_partition(
            extension,
            connection_type=connection_type,
            partition=partition,
            value=value,
        )
    if operation == "partition_schema":
        return connection_partition_schema(
            extension,
            connection_type=connection_type,
            partition=partition,
        )
    if operation == "trusted_mutation_grant":
        return trusted_mutation_grant(extension, operation_id)
    return None


def install_platform_connection_operation(runtime_context: dict[str, Any]) -> None:
    runtime_context["platform_connection_operation"] = run_platform_connection_operation
    runtime_context["platform_connection_contract"] = run_platform_connection_contract


__all__ = [
    "install_platform_connection_operation",
    "run_platform_connection_contract",
    "run_platform_connection_operation",
]
