"""Connection-scoped credential helpers.

Project connections store long-lived credentials as account-scoped secret refs.
The connection row keeps only the ref mapping; decrypted values are resolved by
host runtime code immediately before invoking a credentialed extension action.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from core.infrastructure.security import connection_secret_store as _secret_store
from core.infrastructure.security.stored_secrets import StoredSecretError, StoredSecretReader

_logger = logging.getLogger(__name__)

connection_secret_ref_key = _secret_store.connection_secret_ref_key
connection_secret_ref_map = _secret_store.connection_secret_ref_map
delete_connection_secret_refs = _secret_store.delete_connection_secret_refs
save_connection_secret = _secret_store.save_connection_secret
save_connection_secrets = _secret_store.save_connection_secrets
read_local_connection_secrets = _secret_store.read_local_connection_secrets


def secret_ref_names(secret_refs: Any) -> list[str]:
    return list(_secret_store.normalize_connection_secret_refs(secret_refs))


def resolve_connection_secrets(
    conn: Any,
    *,
    account_id: str,
    secret_refs: Any,
    reader: StoredSecretReader | None = None,
    connection: dict[str, Any] | None = None,
) -> dict[str, str]:
    """A connection's static credentials; OAuth grant values are never included.

    Credentials bound to catalog secrets are read only while the connection's
    configuration matches its approval (ADR 0055). Callers pass the `connection`
    they are resolving; refs that carry catalog bindings without one are refused.
    """
    from core.application.secrets.approval_gate import (
        SecretConfigurationUnapproved,
        catalog_marker,
        require_approved_connection_use,
    )

    if connection is not None:
        require_approved_connection_use(conn, connection, secret_refs=secret_refs)
    elif catalog_marker(secret_refs):
        raise SecretConfigurationUnapproved()
    return _secret_store.resolve_connection_secrets(
        conn,
        account_id=account_id,
        secret_refs=secret_refs,
        reader=reader,
    )


@dataclass(frozen=True)
class ConnectionSecretFailure:
    """A connection's credentials could not be produced, described without the cause's text."""

    detail: str
    code: str
    retryable: bool
    failure_class: str


def connection_secret_failure(connection_id: Any, exc: BaseException) -> ConnectionSecretFailure:
    """Describe a credential resolution failure safely for errors, step results and logs.

    A driver or provider message can carry a URL, a path or a value; only the
    closed code of a `StoredSecretError` and the connection identifier leave here.
    """
    if isinstance(exc, StoredSecretError):
        code, retryable, failure_class = exc.code, exc.retryable, exc.failure_class
    else:
        _logger.warning("connection value resolution failed: %s", type(exc).__name__)
        code, retryable, failure_class = StoredSecretError.code, False, "validation"
    return ConnectionSecretFailure(
        detail=f"connection_secret_resolution_failed:{str(connection_id or '').strip()}:{code}",
        code=code,
        retryable=retryable,
        failure_class=failure_class,
    )
