"""Provider-neutral OAuth revoke preparation and lifecycle invalidation."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from core.application.extensions.oauth_callback_policy import (
    _connection_owned_refs,
    _mapping,
    _text,
)
from core.application.extensions.oauth_configuration_secrets import configuration_secret_ref_key
from core.application.extensions.oauth_contract import (
    OAuthProviderContract,
    provider_revocation_fingerprint,
    provider_security_fingerprint,
)


@dataclass(frozen=True, repr=False)
class PreparedOAuthRevocation:
    endpoint: Any
    token_endpoint_permission: Any
    token: str = field(repr=False)
    client_id: str = field(repr=False)
    client_secret: str = field(default="", repr=False)


def prepare_extension_oauth_revocation(
    *,
    provider: OAuthProviderContract,
    configuration: Mapping[str, Any],
    connection: Mapping[str, Any],
    token_snapshot: Callable[..., dict[str, Any]],
    configuration_secret_value: Callable[..., str],
) -> PreparedOAuthRevocation | None:
    endpoint = provider.revocation_endpoint
    if endpoint is None:
        return None
    configuration_id = _text(configuration.get("configuration_id"))
    connection_id = _text(connection.get("connection_id"))
    account_id = _text(connection.get("account_id"))
    if (
        not configuration_id
        or not connection_id
        or not account_id
        or _text(connection.get("oauth_configuration_id")) != configuration_id
        or _text(connection.get("oauth_provider_id")) != provider.provider_id
        or not secrets.compare_digest(
            _text(connection.get("oauth_provider_security_fingerprint")),
            provider_security_fingerprint(provider),
        )
        or not secrets.compare_digest(
            _text(configuration.get("provider_security_fingerprint")),
            provider_security_fingerprint(provider),
        )
        or not secrets.compare_digest(
            _text(configuration.get("provider_revocation_fingerprint")),
            provider_revocation_fingerprint(provider),
        )
        or not secrets.compare_digest(
            _text(connection.get("oauth_provider_revocation_fingerprint")),
            provider_revocation_fingerprint(provider),
        )
    ):
        return None
    owned_refs = _connection_owned_refs(connection_id, connection.get("secret_refs"))
    if owned_refs is None:
        return None
    snapshot = _mapping(token_snapshot(account_id=account_id, secret_refs={"refs": owned_refs}))
    token = _text(snapshot.get("refresh_token")) or _text(snapshot.get("access_token"))
    client_id = _text(_mapping(configuration.get("non_secret_config")).get("client_id"))
    if not token or not client_id:
        return None
    client_secret = ""
    if provider.token_endpoint.client_auth_method != "none":
        expected_ref = configuration_secret_ref_key(configuration_id, "client_secret")
        refs = _mapping(_mapping(configuration.get("secret_refs")).get("refs"))
        if _text(refs.get("client_secret")) != expected_ref:
            return None
        client_secret = _text(
            configuration_secret_value(account_id=account_id, ref_key=expected_ref)
        )
        if not client_secret:
            return None
    return PreparedOAuthRevocation(
        endpoint=endpoint,
        token_endpoint_permission=provider.token_endpoint,
        token=token,
        client_id=client_id,
        client_secret=client_secret,
    )


def execute_extension_oauth_revocation(
    prepared: PreparedOAuthRevocation,
    *,
    revoke_token: Callable[..., None],
) -> bool:
    try:
        revoke_token(
            endpoint=prepared.endpoint,
            token_endpoint_permission=prepared.token_endpoint_permission,
            token=prepared.token,
            client_id=prepared.client_id,
            client_secret=prepared.client_secret,
        )
    except Exception:
        return False
    return True


def record_extension_oauth_lifecycle_audit(
    logger: Any,
    *,
    extension_id: str,
    account_id: str,
    project_id: str,
    event: str,
    outcome: str,
    resource_id: str,
) -> None:
    """Persist only bounded identifiers and normalized lifecycle outcomes."""
    writer = getattr(logger, "log_extension_invocation", None)
    if not callable(writer):
        return
    try:
        writer(
            extension_key=_text(extension_id),
            invocation_type="oauth_lifecycle",
            status="ok" if outcome in {"succeeded", "not_available"} else "failed",
            request={"event": _text(event), "resource_id": _text(resource_id)},
            response={"remote_revocation": _text(outcome)},
            error_text=None,
            account_id=_text(account_id),
            request_context={"project_id": _text(project_id)} if _text(project_id) else {},
        )
    except Exception:
        return


def record_extension_oauth_uninstall_audit(
    logger: Any,
    *,
    extension_id: str,
    account_id: str,
    install_id: str,
    plan_revision: str,
    actor_type: str,
    actor_id: str,
    outcome: str,
) -> None:
    """Persist uninstall lifecycle evidence independently of extension metadata."""
    writer = getattr(logger, "log_command_execution", None)
    if not callable(writer):
        raise RuntimeError("Durable lifecycle audit storage is unavailable")
    normalized_outcome = _text(outcome)
    if normalized_outcome not in {"succeeded", "failed", "not_available"}:
        normalized_outcome = "failed"
    identity = "\0".join(
        (_text(extension_id), _text(account_id), _text(install_id), _text(plan_revision))
    )
    command_id = f"extension-oauth-uninstall:{hashlib.sha256(identity.encode()).hexdigest()}"
    result = writer(
        command_id=command_id,
        command_type="extension_oauth_uninstall_lifecycle",
        account_id=_text(account_id),
        actor_type=_text(actor_type) or "system_actor",
        actor_id=_text(actor_id) or "system",
        target={
            "extension_id": _text(extension_id),
            "install_id": _text(install_id),
        },
        payload={},
        status="executed",
        result={"remote_revocation": normalized_outcome},
        error_text=None,
    )
    if result is None:
        raise RuntimeError("Durable lifecycle audit storage is unavailable")


def invalidate_install_oauth_on_disable(
    transaction_conn: Any,
    *,
    install_id: str,
    extension_id: str,
    account_id: str,
) -> int:
    """Block OAuth use while retaining exact configurations and connections."""
    normalized = _text(extension_id)
    normalized_account = _text(account_id)
    normalized_install = _text(install_id)
    if not normalized or not normalized_account or not normalized_install:
        return 0
    with transaction_conn.cursor() as cur:
        cur.execute(
            """SELECT install_id, account_id
                 FROM extension_installs
                WHERE install_id = %s AND extension_id = %s
                  AND account_id::text = %s AND state <> 'active'
                FOR UPDATE""",
            (normalized_install, normalized, normalized_account),
        )
        installs = cur.fetchall() or []
        total = 0
        for raw in installs:
            install_id = _text(raw.get("install_id") if isinstance(raw, Mapping) else raw[0])
            account_id = _text(raw.get("account_id") if isinstance(raw, Mapping) else raw[1])
            if not install_id or not account_id:
                continue
            cur.execute(
                """UPDATE extension_oauth_authorization_states
                      SET consumed_at = COALESCE(consumed_at, now()), updated_at = now()
                    WHERE extension_install_id = %s
                      AND extension_id = %s
                      AND account_id::text = %s
                      AND consumed_at IS NULL""",
                (install_id, normalized, account_id),
            )
            cur.execute(
                """UPDATE project_connections
                      SET credential_revision = credential_revision + 1, updated_at = now()
                    WHERE extension_install_id = %s
                      AND extension_id = %s
                      AND account_id::text = %s""",
                (install_id, normalized, account_id),
            )
            total += int(getattr(cur, "rowcount", 0) or 0)
        return total


def resolve_install_id_for_disable(
    transaction_conn: Any, *, extension_id: str, account_id: str
) -> str:
    """Resolve one exact disabled install or fail closed on ambiguous storage."""
    with transaction_conn.cursor() as cur:
        cur.execute(
            """SELECT install_id
                 FROM extension_installs
                WHERE extension_id = %s AND account_id::text = %s
                  AND state <> 'active'
             ORDER BY install_id
                FOR UPDATE""",
            (_text(extension_id), _text(account_id)),
        )
        rows = cur.fetchall() or []
    install_ids = [
        _text(row.get("install_id") if isinstance(row, Mapping) else row[0]) for row in rows
    ]
    install_ids = [value for value in install_ids if value]
    if len(install_ids) != 1:
        return ""
    return install_ids[0]


__all__ = [
    "PreparedOAuthRevocation",
    "execute_extension_oauth_revocation",
    "invalidate_install_oauth_on_disable",
    "prepare_extension_oauth_revocation",
    "record_extension_oauth_lifecycle_audit",
    "record_extension_oauth_uninstall_audit",
    "resolve_install_id_for_disable",
]
