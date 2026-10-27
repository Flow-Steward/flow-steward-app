"""`project_connection_secrets_save`: a secret and the connection that uses it, as one write.

The platform-owned `provider_secrets_save` stores a secret and stops. The
connection that makes the secret usable used to be created by an extension-side
branch that Contract v2 routing never reaches, so a valid token could exist with
no connection to carry it — saved, verified against the provider, and useless.

This operation does both, in one transaction. A failure leaves neither a new
token without its connection nor a connection claiming a token that was never
saved. On an existing connection it preserves the connection id and every piece
of state the operator already configured, and merges only the secret references
it was declared to own.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from core.application.extensions.json_schema_subset import validate_json_schema_value
from core.application.extensions.project_connection_state import carried_connection_fields

#: Manifest keys this operation reads. Anything else in `config` is ignored, so
#: an extension cannot widen the write through configuration.
CONFIG_PROVIDER_KEY = "provider_key"
CONFIG_CONNECTION_TYPE = "connection_type_id"
CONFIG_SECRET_FIELDS = "secret_fields"


class SecretsSaveRejected(ValueError):
    """The request cannot be honoured; nothing was written."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _declared_secret_fields(config: dict[str, Any]) -> list[tuple[str, str]]:
    """The closed input-field → secret-key mapping the manifest declares."""
    fields: list[tuple[str, str]] = []
    for raw in _as_list(config.get(CONFIG_SECRET_FIELDS)):
        spec = _as_dict(raw)
        input_field = _text(spec.get("input")) or _text(spec.get("field"))
        secret_key = _text(spec.get("secret_key")) or input_field
        if input_field and secret_key:
            fields.append((input_field, secret_key))
    return fields


def _existing_secret_refs(connection: dict[str, Any]) -> dict[str, Any]:
    """The complete persisted reference declaration, without secret values."""
    return _as_dict(connection.get("secret_refs"))


@contextmanager
def _transaction_for(store: Any) -> Iterator[Any]:
    """Use the operation store's explicit atomic provider-configuration port."""
    begin = getattr(store, "provider_configuration_transaction", None)
    if not callable(begin):
        raise SecretsSaveRejected(
            "project_connection_storage_unavailable",
            "Project connection transaction storage is unavailable.",
        )
    with begin() as transaction:
        yield transaction


def save_project_connection_secrets(
    store: Any,
    *,
    project_id: str,
    extension_id: str,
    extension_install_id: str,
    account_id: str = "",
    trusted_grant: dict[str, Any] | None = None,
    config: dict[str, Any],
    input_payload: dict[str, Any],
    transaction: Callable[[Any], Any] | None = None,
    secret_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Save the declared secrets and establish or update the connection that uses them.

    Returns secret keys and readiness metadata. Never a value.
    """
    normalized_project_id = _text(project_id)
    normalized_extension_id = _text(extension_id)
    if not normalized_project_id:
        raise SecretsSaveRejected("project_context_required", "A project is required.")
    if not normalized_extension_id:
        raise SecretsSaveRejected("extension_context_required", "An extension is required.")

    config = _as_dict(config)
    payload = _as_dict(input_payload)
    provider_key = _text(config.get(CONFIG_PROVIDER_KEY)) or normalized_extension_id
    grant = _as_dict(trusted_grant)
    if not grant or _text(grant.get("scope")) != "owning_extension":
        raise SecretsSaveRejected(
            "host_mutation_not_granted",
            "The host did not grant this extension secret-write access.",
        )
    trusted_context = {
        "project_id": normalized_project_id,
        "extension_id": normalized_extension_id,
        "extension_install_id": _text(extension_install_id),
    }
    if any(_text(grant.get(key)) != value for key, value in trusted_context.items()):
        raise SecretsSaveRejected(
            "host_mutation_context_mismatch",
            "The host mutation grant does not match this extension installation context.",
        )
    allowed_provider_key = _text(grant.get(CONFIG_PROVIDER_KEY))
    if provider_key != allowed_provider_key:
        raise SecretsSaveRejected(
            "provider_secret_namespace_forbidden",
            "The extension cannot write another provider's secret namespace.",
        )
    connection_type = _text(config.get(CONFIG_CONNECTION_TYPE))
    declared = _declared_secret_fields(config)
    if not declared:
        raise SecretsSaveRejected(
            "secret_fields_undeclared", "The manifest declares no secret fields to save."
        )
    allowed_secret_keys = {
        _text(item) for item in _as_list(grant.get("secret_keys")) if _text(item)
    }
    if not allowed_secret_keys or any(key not in allowed_secret_keys for _field, key in declared):
        raise SecretsSaveRejected(
            "provider_secret_key_forbidden",
            "The extension requested a secret key outside its host grant.",
        )
    allowed_connection_types = {
        _text(item) for item in _as_list(grant.get("connection_type_ids")) if _text(item)
    }
    if not connection_type or connection_type not in allowed_connection_types:
        raise SecretsSaveRejected(
            "project_connection_type_forbidden",
            "The extension requested a connection type outside its host grant.",
        )

    # Rejected before any write: an empty required secret must alter nothing,
    # and there is no partial state to roll back if we never begin.
    supplied_values: dict[str, Any] = {}
    for input_field, secret_key in declared:
        if input_field not in payload:
            continue
        value = payload.get(input_field)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise SecretsSaveRejected(
                "provider_secret_value_required",
                f"Secret '{input_field}' cannot be empty.",
            )
        supplied_values[secret_key] = value
    if not supplied_values:
        raise SecretsSaveRejected(
            "provider_secret_value_required", "No declared secret was supplied."
        )
    schema_errors: list[str] = []
    if secret_schema:
        validate_json_schema_value(
            schema=secret_schema,
            value=supplied_values,
            path="connection_secret",
            errors=schema_errors,
        )
    if schema_errors:
        raise SecretsSaveRejected(
            "invalid_payload",
            "Secret payload failed the declared connection schema.",
        )
    supplied = [
        (input_field, secret_key, str(supplied_values[secret_key]))
        for input_field, secret_key in declared
        if secret_key in supplied_values
    ]

    existing = _as_dict(
        store.get_project_connection(
            project_id=normalized_project_id,
            extension_id=normalized_extension_id,
            extension_install_id=_text(extension_install_id),
            enabled_only=False,
        )
    )
    merged_refs = _existing_secret_refs(existing)
    provider_secret_refs = _as_dict(merged_refs.get("provider_secrets"))
    provider_secret_keys = _as_dict(provider_secret_refs.get("keys"))
    for input_field, secret_key, _value in supplied:
        provider_secret_keys[input_field] = secret_key
    merged_refs["provider_secrets"] = {
        "provider_key": provider_key,
        "keys": provider_secret_keys,
    }

    begin = transaction or _transaction_for
    with begin(store):
        for _input_field, secret_key, value in supplied:
            saved_secret = store.save_provider_secret(
                provider_key=provider_key,
                secret_key=secret_key,
                secret_value=value,
                project_id=normalized_project_id,
                active=True,
                metadata={"source": "platform_action", "extension_id": normalized_extension_id},
            )
            if saved_secret is not True:
                raise SecretsSaveRejected(
                    "provider_secret_storage_failed",
                    f"Secret '{secret_key}' was not saved.",
                )
        # The upsert overwrites every JSON column with what it is given, so every
        # piece of existing state is carried across explicitly. Omitting a field
        # here is how a token replacement would silently erase the operator's
        # channels, routes and inbound settings.
        carried = carried_connection_fields(
            existing,
            extension_install_id=extension_install_id,
            account_id=account_id,
        )
        carried["connection_type"] = connection_type or carried["connection_type"]
        carried["secret_refs"] = merged_refs
        # A successful project-bound save is what makes the connection usable.
        carried["enabled"] = True
        saved = store.upsert_project_connection(
            project_id=normalized_project_id,
            extension_id=normalized_extension_id,
            **carried,
        )

    saved_row = _as_dict(saved)
    return {
        "saved_keys": [input_field for input_field, _k, _v in supplied],
        "secret_keys": [secret_key for _f, secret_key, _v in supplied],
        "connection_id": _text(saved_row.get("connection_id")),
        "connection_enabled": True,
        "connection_type": connection_type or _text(saved_row.get("connection_type")),
    }


__all__ = [
    "CONFIG_CONNECTION_TYPE",
    "CONFIG_PROVIDER_KEY",
    "CONFIG_SECRET_FIELDS",
    "SecretsSaveRejected",
    "save_project_connection_secrets",
]
