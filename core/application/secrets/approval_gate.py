"""The use check: a connection's catalog credentials are read only for its approved configuration.

The check reloads the connection from the database rather than trusting the row a
caller holds, recomputes the executable projection with the one implementation in
`projection`, re-checks that every bound secret is still available to the
connection's project, and compares the fingerprint with the approval. It runs
before any value is read.
"""

from __future__ import annotations

from typing import Any

from core.application.secrets import repository
from core.application.secrets.projection import (
    DISPATCH_FIELDS,
    connection_projection,
    projection_fingerprint,
)
from core.infrastructure.security.stored_secrets import StoredSecretError


class SecretConfigurationUnapproved(StoredSecretError):
    """The configuration around a catalog secret changed since a person approved it."""

    code = "secret_configuration_unapproved"
    safe_message = (
        "This configuration uses stored secrets and has not been approved since it changed."
    )


def _text(value: Any) -> str:
    return str(value or "").strip()


def catalog_marker(secret_refs: Any) -> dict[str, str]:
    """The bindings projected into a connection's secret_refs (field -> secret id)."""
    catalog = secret_refs.get("catalog") if isinstance(secret_refs, dict) else None
    return {str(k): str(v) for k, v in catalog.items()} if isinstance(catalog, dict) else {}


def catalog_projection_only(secret_refs: Any) -> dict[str, Any]:
    """The part of a connection's secret_refs the catalog owns, for paths that clear the rest."""
    marker = catalog_marker(secret_refs)
    if not marker:
        return {}
    refs = secret_refs.get("refs") if isinstance(secret_refs, dict) else None
    refs = refs if isinstance(refs, dict) else {}
    return {
        "refs": {field: refs[field] for field in marker if field in refs},
        "catalog": dict(marker),
    }


def require_approved_connection_use(
    conn: Any, connection: dict[str, Any], *, secret_refs: Any = None
) -> None:
    """Refuse unless the configuration the caller will dispatch is the approved one.

    The caller's row is what it sends: its executable fields must equal the current
    row's, and its refs for bound fields must point at the bound secrets. Only then is
    the current row's fingerprint compared with the approval. A caller holding an
    older configuration cannot pass because a newer one was approved.
    """
    account_id = _text(connection.get("account_id"))
    connection_id = _text(connection.get("connection_id"))
    if not account_id or not connection_id:
        if catalog_marker(connection.get("secret_refs")):
            raise SecretConfigurationUnapproved()
        return
    with conn.cursor() as cur:
        current = repository.lock_connection(
            cur, account_id=account_id, connection_id=connection_id, for_update=False
        )
        if current is None:
            raise SecretConfigurationUnapproved()
        bindings = repository.connection_bindings(
            cur, account_id=account_id, connection_id=connection_id
        )
        marker = catalog_marker(current.get("secret_refs"))
        if not bindings and not marker:
            return
        if bindings != marker:
            raise SecretConfigurationUnapproved()
        caller_view = {
            **current,
            **{key: connection[key] for key in DISPATCH_FIELDS if key in connection},
        }
        project = repository.project_scope(
            cur, account_id=account_id, project_id=_text(current.get("project_id"))
        )
        for secret_id in bindings.values():
            secret = repository.get_secret(cur, account_id=account_id, secret_id=secret_id)
            if not secret or not project or not _available(secret, project):
                raise SecretConfigurationUnapproved()
        storage_keys = {}
        for field, secret_id in bindings.items():
            secret = repository.get_secret(cur, account_id=account_id, secret_id=secret_id)
            storage_keys[field] = (secret or {}).get("storage_ref_key")
        release = repository.install_release(
            cur, install_id=_text(current.get("extension_install_id"))
        )
        fingerprint = projection_fingerprint(
            connection_projection(current, bindings=bindings, release=release)
        )
        caller_fingerprint = projection_fingerprint(
            connection_projection(caller_view, bindings=bindings, release=release)
        )
        approval = repository.connection_approval(
            cur, account_id=account_id, connection_id=connection_id
        )
    if caller_fingerprint != fingerprint:
        # The caller holds a configuration that is no longer current.
        raise SecretConfigurationUnapproved()
    caller_refs = (secret_refs if secret_refs is not None else connection.get("secret_refs")) or {}
    refs = caller_refs.get("refs") if isinstance(caller_refs, dict) else None
    refs = refs if isinstance(refs, dict) else {}
    for field, storage_key in storage_keys.items():
        if field in refs and _text(refs[field]) != _text(storage_key):
            raise SecretConfigurationUnapproved()
    if not approval or approval.get("fingerprint") != fingerprint:
        raise SecretConfigurationUnapproved()


def _available(secret: dict[str, Any], project: dict[str, Any]) -> bool:
    if _text(secret.get("project_id")):
        return _text(secret.get("project_id")) == _text(project.get("project_id"))
    return _text(secret.get("workspace_id")) == _text(project.get("workspace_id"))


__all__ = [
    "SecretConfigurationUnapproved",
    "catalog_marker",
    "catalog_projection_only",
    "require_approved_connection_use",
]
