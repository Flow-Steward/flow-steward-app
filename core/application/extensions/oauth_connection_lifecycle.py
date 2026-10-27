from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from core.application.auth.principal import principal_type_has_minimum_role
from core.application.management.errors import (
    ServiceConflictError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceUnavailableError,
)
from core.application.management.scope import ScopeContext

from .oauth_callback_policy import _connection_owned_refs, _mapping, _text
from .oauth_contract import (
    OAuthProviderContract,
    compile_oauth_providers,
    provider_revocation_fingerprint,
    provider_security_fingerprint,
)
from .oauth_lifecycle import (
    PreparedOAuthRevocation,
    execute_extension_oauth_revocation,
    prepare_extension_oauth_revocation,
    record_extension_oauth_lifecycle_audit,
)

_MESSAGE_CONNECTION_NOT_FOUND = "Connection not found"
_MESSAGE_CONNECTION_CANNOT_BE_DISCONNECTED = "Connection cannot be disconnected"
_MESSAGE_CONNECTION_CHANGED_RETRY_DISCONNECT = "Connection changed; retry disconnect"

_DISCONNECT_LEASE_SECONDS = 300


@dataclass(frozen=True)
class _PreparedDisconnect:
    scope: dict[str, str]
    blocked_revision: int
    operation_id: str
    revocation: PreparedOAuthRevocation | None


class OAuthConnectionLifecycleService:
    def __init__(
        self,
        *,
        repository: Any,
        install_loader: Callable[[str, str], Mapping[str, Any] | None],
        token_snapshot: Callable[..., dict[str, Any]],
        configuration_secret_value: Callable[..., str],
        delete_secret: Callable[..., None],
        revoke_token: Callable[..., None],
        transaction_factory: Callable[[], AbstractContextManager[Any]],
        revocation_audit: Callable[..., None] | None = None,
    ) -> None:
        self._repository = repository
        self._install_loader = install_loader
        self._token_snapshot = token_snapshot
        self._configuration_secret_value = configuration_secret_value
        self._delete_secret = delete_secret
        self._revoke_token = revoke_token
        self._transaction_factory = transaction_factory
        self._revocation_audit = revocation_audit

    def disconnect(
        self,
        scope: ScopeContext,
        install_id: str,
        provider_id: str,
        configuration_id: str,
        connection_id: str,
    ) -> dict[str, Any]:
        if not principal_type_has_minimum_role(scope.principal_type, "operator"):
            raise ServiceForbiddenError("Operator role required")
        with self._transaction_factory():
            exact_scope, provider = self._resolve_scope(
                scope, install_id, provider_id, configuration_id, connection_id
            )
            prepared = self._prepare_disconnect(exact_scope, provider)
        outcome = "not_available"
        provider_current = False
        try:
            with self._transaction_factory():
                remote_scope, remote_provider = self._resolve_scope(
                    scope, install_id, provider_id, configuration_id, connection_id
                )
            provider_current = remote_scope == prepared.scope and (
                provider_security_fingerprint(remote_provider)
                == provider_security_fingerprint(provider)
                and provider_revocation_fingerprint(remote_provider)
                == provider_revocation_fingerprint(provider)
            )
        except Exception:
            # Provider/install drift can make remote revocation unavailable, but it
            # must never strand credentials that were already blocked locally.
            provider_current = False
        if prepared.revocation is not None and provider_current:
            outcome = (
                "succeeded"
                if execute_extension_oauth_revocation(
                    prepared.revocation, revoke_token=self._revoke_token
                )
                else "failed"
            )
        with self._transaction_factory():
            self._finalize_disconnect(prepared)
        if self._revocation_audit is not None:
            self._revocation_audit(
                extension_id=prepared.scope["extension_id"],
                account_id=prepared.scope["account_id"],
                project_id=prepared.scope["project_id"],
                event="connection_disconnect",
                outcome=outcome,
                resource_id=connection_id,
            )
        return {
            "status": "disconnected",
            "connection_id": connection_id,
            "remote_revocation": outcome,
        }

    def _resolve_scope(
        self,
        scope: ScopeContext,
        install_id: str,
        provider_id: str,
        configuration_id: str,
        connection_id: str,
    ) -> tuple[dict[str, str], OAuthProviderContract]:
        values = {
            "tenant_id": _text(scope.tenant_id),
            "account_id": _text(scope.account_id),
            "project_id": _text(scope.project_id),
            "extension_install_id": _text(install_id),
            "provider_id": _text(provider_id),
            "configuration_id": _text(configuration_id),
            "connection_id": _text(connection_id),
        }
        if not all(values.values()):
            raise ServiceNotFoundError(_MESSAGE_CONNECTION_NOT_FOUND)
        install = self._install_loader(values["extension_install_id"], values["account_id"])
        if not isinstance(install, Mapping):
            raise ServiceNotFoundError(_MESSAGE_CONNECTION_NOT_FOUND)
        extension_id = _text(install.get("extension_id"))
        release_id = _text(install.get("release_id"))
        descriptor = install.get("compiled_manifest") or install.get("manifest") or {}
        try:
            providers = compile_oauth_providers(
                dict(descriptor) if isinstance(descriptor, Mapping) else {}
            )
        except ValueError as exc:
            raise ServiceNotFoundError(_MESSAGE_CONNECTION_NOT_FOUND) from exc
        provider = next((row for row in providers if row.provider_id == provider_id), None)
        if provider is None or not extension_id or not release_id:
            raise ServiceNotFoundError(_MESSAGE_CONNECTION_NOT_FOUND)
        values.update(extension_id=extension_id, extension_release_id=release_id)
        return values, provider

    def _prepare_disconnect(
        self, scope: dict[str, str], provider: OAuthProviderContract
    ) -> _PreparedDisconnect:
        row = self._repository.lock_connection_for_lifecycle(**scope)
        if row is None:
            raise ServiceNotFoundError(_MESSAGE_CONNECTION_NOT_FOUND)
        # A connection pinned to an earlier release of the extension (an update
        # that did not move it) still exists and still holds credentials. It
        # answered "not found" to every disconnect, so it could only be removed
        # by deleting the whole OAuth configuration. It is disconnected locally;
        # only the remote revocation needs the pins to match the current provider.
        provider_matches = self._row_matches(row, scope, provider)
        lifecycle_status = _text(
            _mapping(_mapping(row.get("connection_config")).get("oauth")).get("lifecycle_status")
        )
        oauth = _mapping(_mapping(row.get("connection_config")).get("oauth"))
        revision = int(row.get("credential_revision") or 0)
        operation_id = secrets.token_urlsafe(32)
        resuming = lifecycle_status == "disconnecting"
        if revision < 1:
            raise ServiceConflictError(_MESSAGE_CONNECTION_CANNOT_BE_DISCONNECTED)
        if resuming:
            previous_operation_id = _text(oauth.get("disconnect_operation_id"))
            if row.get("enabled") is not False:
                raise ServiceConflictError(_MESSAGE_CONNECTION_CANNOT_BE_DISCONNECTED)
            if not self._repository.claim_stale_connection_disconnect(
                **scope,
                expected_credential_revision=revision,
                expected_disconnect_operation_id=previous_operation_id,
                disconnect_operation_id=operation_id,
                lease_seconds=_DISCONNECT_LEASE_SECONDS,
            ):
                raise ServiceConflictError("Disconnect already in progress")
        elif row.get("enabled") is not True or lifecycle_status == "disconnected":
            raise ServiceConflictError(_MESSAGE_CONNECTION_CANNOT_BE_DISCONNECTED)
        configuration = self._configuration(row, scope)
        revocation = (
            prepare_extension_oauth_revocation(
                provider=provider,
                configuration=configuration,
                connection=row,
                token_snapshot=self._token_snapshot,
                configuration_secret_value=self._configuration_secret_value,
            )
            if provider_matches
            else None
        )
        if resuming:
            return _PreparedDisconnect(
                scope=dict(scope),
                blocked_revision=revision + 1,
                operation_id=operation_id,
                revocation=revocation,
            )
        if revision < 1 or not self._repository.block_connection_disconnect(
            **scope,
            expected_credential_revision=revision,
            disconnect_operation_id=operation_id,
            lease_seconds=_DISCONNECT_LEASE_SECONDS,
        ):
            raise ServiceConflictError(_MESSAGE_CONNECTION_CHANGED_RETRY_DISCONNECT)
        self._repository.invalidate_connection_lifecycle_states(**scope)
        return _PreparedDisconnect(
            scope=dict(scope),
            blocked_revision=revision + 1,
            operation_id=operation_id,
            revocation=revocation,
        )

    def _finalize_disconnect(self, prepared: _PreparedDisconnect) -> None:
        row = self._repository.lock_connection_for_disconnect_finalize(**prepared.scope)
        if (
            row is None
            or int(row.get("credential_revision") or 0) != prepared.blocked_revision
            or _text(
                _mapping(_mapping(row.get("connection_config")).get("oauth")).get(
                    "disconnect_operation_id"
                )
            )
            != prepared.operation_id
            or _text(
                _mapping(_mapping(row.get("connection_config")).get("oauth")).get(
                    "lifecycle_status"
                )
            )
            != "disconnecting"
        ):
            raise ServiceConflictError(_MESSAGE_CONNECTION_CHANGED_RETRY_DISCONNECT)
        refs = _connection_owned_refs(prepared.scope["connection_id"], row.get("secret_refs"))
        if refs is None:
            raise ServiceConflictError("Connection credentials changed; retry disconnect")
        for ref_key in sorted(set(refs.values())):
            self._delete_secret(account_id=prepared.scope["account_id"], ref_key=ref_key)
        if not self._repository.finalize_connection_disconnect(
            **prepared.scope,
            expected_credential_revision=prepared.blocked_revision,
            expected_disconnect_operation_id=prepared.operation_id,
        ):
            raise ServiceConflictError(_MESSAGE_CONNECTION_CHANGED_RETRY_DISCONNECT)

    @staticmethod
    def _configuration(row: Mapping[str, Any], scope: Mapping[str, str]) -> dict[str, Any]:
        return {
            "configuration_id": scope["configuration_id"],
            "account_id": scope["account_id"],
            "extension_release_id": row.get("configuration_release_id"),
            "non_secret_config": row.get("configuration_non_secret_config"),
            "secret_refs": row.get("configuration_secret_refs"),
            "provider_security_fingerprint": row.get("configuration_security_fingerprint"),
            "provider_revocation_fingerprint": row.get("configuration_revocation_fingerprint"),
        }

    @staticmethod
    def _row_matches(
        row: Mapping[str, Any], scope: Mapping[str, str], provider: OAuthProviderContract
    ) -> bool:
        return (
            _text(row.get("oauth_extension_release_id")) == scope["extension_release_id"]
            and _text(row.get("configuration_release_id")) == scope["extension_release_id"]
            and secrets.compare_digest(
                _text(row.get("oauth_provider_security_fingerprint")),
                provider_security_fingerprint(provider),
            )
            and secrets.compare_digest(
                _text(row.get("oauth_provider_revocation_fingerprint")),
                provider_revocation_fingerprint(provider),
            )
            and secrets.compare_digest(
                _text(row.get("configuration_security_fingerprint")),
                provider_security_fingerprint(provider),
            )
            and secrets.compare_digest(
                _text(row.get("configuration_revocation_fingerprint")),
                provider_revocation_fingerprint(provider),
            )
        )


__all__ = ["OAuthConnectionLifecycleService"]


def build_database_oauth_connection_lifecycle_service(
    logger: Any,
) -> OAuthConnectionLifecycleService:
    from core.application.auth.secrets import decrypt_secret
    from core.infrastructure.db import model_provider_store
    from core.infrastructure.db.db_pool import transaction_connection
    from core.infrastructure.extension_runtime.oauth_token_store import (
        DatabaseOAuthCallbackTokenStore,
    )
    from core.infrastructure.extension_runtime.oauth_transport import revoke_token

    from .oauth_repository import ExtensionOAuthRepository

    conn = getattr(logger, "conn", None)
    if conn is None:
        raise ServiceUnavailableError("Database not available")
    token_store = DatabaseOAuthCallbackTokenStore(conn)

    def configuration_secret_value(*, account_id: str, ref_key: str) -> str:
        ciphertext = model_provider_store.get_secret_ref_ciphertext(
            conn, account_id=account_id, ref_key=ref_key
        )
        return decrypt_secret(ciphertext) if ciphertext else ""

    def revocation_audit(**values: Any) -> None:
        with transaction_connection(conn):
            record_extension_oauth_lifecycle_audit(logger, **values)

    return OAuthConnectionLifecycleService(
        repository=ExtensionOAuthRepository(conn),
        install_loader=lambda install_id, account_id: logger.get_extension_install_for_operator(
            install_id, account_id=account_id
        ),
        token_snapshot=token_store.resolve_token_snapshot,
        configuration_secret_value=configuration_secret_value,
        delete_secret=lambda **values: model_provider_store.delete_secret_ref(conn, **values),
        revoke_token=revoke_token,
        transaction_factory=lambda: transaction_connection(conn),
        revocation_audit=revocation_audit,
    )


__all__.append("build_database_oauth_connection_lifecycle_service")
