"""Connection credentials borrowed from an OAuth configuration (``credentials_from``).

A connection type that authenticates as an application (the extension runs the
client-credentials exchange itself) would otherwise repeat the same client
values on every connection. Instead it may declare::

    credentials_from:
      oauth_provider_id: example_oauth
      fields:                     # OAuth credential field id -> place in target.connection
        client_id: {into: config, as: client_id}
        client_secret: {into: secrets, as: client_secret}
        tenant_id: {into: config, as: tenant_id}

Such a connection stores ``credentials_configuration_id``: one OAuth
configuration of the same install and project for that provider. Everywhere the
platform builds ``target.connection`` it reads the mapped fields from that
configuration's current revision, so a rotated secret reaches every linked
connection on its next call. A missing or incomplete configuration fails with
``credentials_unavailable``; secret values never leave the server.

The link has its own column: ``oauth_configuration_id`` marks a connection that
holds its own OAuth grant, and every OAuth lifecycle path (refresh, disconnect,
reauthorization) acts on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.application.extensions.oauth_contract import contract_section

CREDENTIALS_UNAVAILABLE = "credentials_unavailable"
CREDENTIALS_FROM_INTO = frozenset({"config", "secrets"})
USAGE_EXCHANGE = "exchange"
USAGE_RUNTIME = "runtime"
CREDENTIAL_USAGES = frozenset({USAGE_EXCHANGE, USAGE_RUNTIME})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


class CredentialsUnavailable(RuntimeError):
    """The connection's borrowed credentials cannot be produced."""

    code = CREDENTIALS_UNAVAILABLE

    def __init__(self, detail: str = "") -> None:
        super().__init__(
            f"{CREDENTIALS_UNAVAILABLE}:{detail}" if detail else CREDENTIALS_UNAVAILABLE
        )
        self.detail = detail


@dataclass(frozen=True)
class CredentialsFrom:
    provider_id: str
    #: OAuth credential field id -> (``config`` or ``secrets``, name in target.connection)
    fields: dict[str, tuple[str, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class BorrowedCredentials:
    config: dict[str, str] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)


def _connection_type_row(extension: dict[str, Any] | None, connection_type: str) -> dict[str, Any]:
    payload = _as_dict(extension)
    for descriptor in (payload, _as_dict(payload.get("compiled_manifest"))):
        for row in contract_section(descriptor, "connection_types"):
            if _text(row.get("connection_type_id")) == _text(connection_type):
                return row
    return {}


def credentials_from_spec(
    extension: dict[str, Any] | None, connection_type: str
) -> CredentialsFrom | None:
    """The type's ``credentials_from`` declaration, or None when it has none."""
    block = _as_dict(_connection_type_row(extension, connection_type).get("credentials_from"))
    provider_id = _text(block.get("oauth_provider_id"))
    if not provider_id:
        return None
    mapped: dict[str, tuple[str, str]] = {}
    for field_id, target in _as_dict(block.get("fields")).items():
        target_map = _as_dict(target)
        into = _text(target_map.get("into"))
        name = _text(target_map.get("as")) or _text(field_id)
        if _text(field_id) and into in CREDENTIALS_FROM_INTO and name:
            mapped[_text(field_id)] = (into, name)
    return CredentialsFrom(provider_id=provider_id, fields=mapped)


def provider_credential_fields(
    extension: dict[str, Any] | None, provider_id: str
) -> list[dict[str, Any]]:
    payload = _as_dict(extension)
    for descriptor in (payload, _as_dict(payload.get("compiled_manifest"))):
        for row in contract_section(descriptor, "oauth_providers"):
            if _text(row.get("provider_id")) == _text(provider_id):
                fields = row.get("credential_fields")
                return (
                    [item for item in fields if isinstance(item, dict)]
                    if isinstance(fields, list)
                    else []
                )
    return []


def _configuration_row(bl: Any, configuration_id: str) -> dict[str, Any]:
    conn = getattr(bl, "conn", None)
    if conn is None or not _text(configuration_id):
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """SELECT configuration_id::text, account_id::text, project_id::text,
                      extension_install_id, extension_id, provider_id,
                      configuration_revision, non_secret_config, secret_refs
                 FROM extension_oauth_configurations
                WHERE configuration_id::text = %s""",
            (_text(configuration_id),),
        )
        row = cur.fetchone()
        if not row:
            return {}
        if isinstance(row, dict):
            return dict(row)
        names = [column[0] for column in cur.description]
        return dict(zip(names, row, strict=False))


def _configuration_secret(bl: Any, *, account_id: str, ref_key: str) -> str:
    from core.application.auth.secrets import decrypt_secret
    from core.infrastructure.db import model_provider_store

    ciphertext = model_provider_store.get_secret_ref_ciphertext(
        getattr(bl, "conn", None), account_id=account_id, ref_key=ref_key
    )
    return decrypt_secret(ciphertext) if ciphertext else ""


def _extension_for_connection(bl: Any, connection: dict[str, Any]) -> dict[str, Any]:
    """The installed extension record of the connection's install, read from the store."""
    install_id = _text(connection.get("extension_install_id"))
    lookup = getattr(bl, "get_extension_by_install_id_for_runtime_gate", None) or getattr(
        bl, "get_extension_by_install_id", None
    )
    if not install_id or not callable(lookup):
        return {}
    try:
        return _as_dict(lookup(install_id, account_id=_text(connection.get("account_id")) or None))
    except Exception:
        return {}


def linked_configuration_id(connection: dict[str, Any] | None) -> str:
    return _text(_as_dict(connection).get("credentials_configuration_id"))


def resolve_borrowed_credentials(
    bl: Any,
    *,
    connection: dict[str, Any],
    extension: dict[str, Any] | None = None,
) -> BorrowedCredentials | None:
    """The mapped values for this connection, or None when it borrows nothing.

    Raises :class:`CredentialsUnavailable` when the connection is linked but the
    configuration is gone, belongs elsewhere, or lacks a required value.
    """
    configuration_id = linked_configuration_id(connection)
    if not configuration_id:
        return None
    descriptor = extension if extension else _extension_for_connection(bl, connection)
    spec = credentials_from_spec(descriptor, _text(connection.get("connection_type")))
    if spec is None:
        raise CredentialsUnavailable("connection_type_has_no_credentials_from")
    configuration = _configuration_row(bl, configuration_id)
    if not configuration:
        raise CredentialsUnavailable("configuration_missing")
    for key in ("account_id", "project_id", "extension_install_id", "extension_id"):
        if _text(configuration.get(key)) != _text(connection.get(key)):
            raise CredentialsUnavailable("configuration_out_of_scope")
    if _text(configuration.get("provider_id")) != spec.provider_id:
        raise CredentialsUnavailable("configuration_out_of_scope")
    declared = {
        _text(item.get("field_id")): item
        for item in provider_credential_fields(descriptor, spec.provider_id)
    }
    non_secret = _as_dict(configuration.get("non_secret_config"))
    refs = _as_dict(_as_dict(configuration.get("secret_refs")).get("refs"))
    borrowed = BorrowedCredentials()
    from core.application.extensions.oauth_configuration_secrets import (
        configuration_secret_ref_key,
    )

    for field_id, (into, name) in spec.fields.items():
        field_spec = declared.get(field_id, {})
        if field_spec.get("secret") is True:
            expected_ref = configuration_secret_ref_key(configuration_id, field_id)
            value = (
                _configuration_secret(
                    bl, account_id=_text(configuration.get("account_id")), ref_key=expected_ref
                )
                if _text(refs.get(field_id)) == expected_ref
                else ""
            )
        else:
            value = _text(non_secret.get(field_id))
        if not value:
            if field_spec.get("required") is True:
                raise CredentialsUnavailable(f"configuration_incomplete:{field_id}")
            continue
        (borrowed.secrets if into == "secrets" else borrowed.config)[name] = value
    return borrowed


def credentials_status(bl: Any, connection: dict[str, Any]) -> str:
    """``""`` when the connection's borrowed credentials resolve (or it borrows none)."""
    try:
        resolve_borrowed_credentials(bl, connection=connection)
    except CredentialsUnavailable:
        return CREDENTIALS_UNAVAILABLE
    except Exception:
        return CREDENTIALS_UNAVAILABLE
    return ""


def apply_borrowed_credentials(
    bl: Any,
    connection: dict[str, Any],
    target: dict[str, Any],
    *,
    extension: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """*target* (a ``target.connection`` payload) with the borrowed values merged in."""
    borrowed = resolve_borrowed_credentials(bl, connection=connection, extension=extension)
    if borrowed is None:
        return target
    merged = dict(target)
    merged["config"] = {**_as_dict(target.get("config")), **borrowed.config}
    merged["secrets"] = {**_as_dict(target.get("secrets")), **borrowed.secrets}
    return merged


def validate_link(
    bl: Any,
    *,
    extension: dict[str, Any],
    connection_type: str,
    configuration_id: str,
    account_id: str,
    project_id: str,
    extension_install_id: str,
    extension_id: str,
) -> str:
    """The normalized configuration id a new or updated connection may link to.

    Raises ``ServiceValidationError`` when the type borrows nothing, the
    configuration is elsewhere or of another provider, or it lacks a required
    value (the configuration's authorization status does not matter).
    """
    from core.application.management.errors import ServiceValidationError

    spec = credentials_from_spec(extension, connection_type)
    if spec is None:
        raise ServiceValidationError(
            "This connection type does not take its credentials from an OAuth configuration",
            payload={
                "error": "credentials_from_not_declared",
                "code": "credentials_from_not_declared",
            },
        )
    probe = {
        "credentials_configuration_id": configuration_id,
        "connection_type": connection_type,
        "account_id": account_id,
        "project_id": project_id,
        "extension_install_id": extension_install_id,
        "extension_id": extension_id,
    }
    try:
        resolve_borrowed_credentials(bl, connection=probe, extension=extension)
    except CredentialsUnavailable as exc:
        raise ServiceValidationError(
            "The chosen OAuth configuration cannot provide this connection's credentials",
            payload={
                "error": CREDENTIALS_UNAVAILABLE,
                "code": CREDENTIALS_UNAVAILABLE,
                "reason": exc.detail,
            },
        ) from None
    return _text(configuration_id)


def list_linked_connections(bl_or_conn: Any, *, configuration_id: str) -> list[dict[str, Any]]:
    """Connections that take their credentials from this configuration."""
    conn = getattr(bl_or_conn, "conn", None) or bl_or_conn
    with conn.cursor() as cur:
        cur.execute(
            """SELECT connection_id, display_name, connection_type
                 FROM project_connections
                WHERE credentials_configuration_id::text = %s
                ORDER BY connection_id""",
            (_text(configuration_id),),
        )
        rows = cur.fetchall()
        names = [column[0] for column in cur.description]
    return [
        dict(row) if isinstance(row, dict) else dict(zip(names, row, strict=False)) for row in rows
    ]


__all__ = [
    "CREDENTIALS_FROM_INTO",
    "CREDENTIALS_UNAVAILABLE",
    "CREDENTIAL_USAGES",
    "USAGE_EXCHANGE",
    "USAGE_RUNTIME",
    "BorrowedCredentials",
    "CredentialsFrom",
    "CredentialsUnavailable",
    "apply_borrowed_credentials",
    "credentials_from_spec",
    "credentials_status",
    "linked_configuration_id",
    "list_linked_connections",
    "provider_credential_fields",
    "resolve_borrowed_credentials",
    "validate_link",
]
