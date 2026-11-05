"""Start an extension OAuth grant from a refresh token the operator pastes (ADR 0058).

Some providers issue a refresh token in their own console, with no redirect the
platform could take part in. The operator pastes it here; Core exchanges it once
at the provider's declared token endpoint and, on success, commits the grant
through the same path the authorization-code callback uses. Nothing here knows
any provider: the endpoint, the client authentication and the connection
settings all come from the extension's declaration.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from core.application.auth.principal import principal_type_has_minimum_role
from core.application.extensions.connection_partition_validation import (
    ConnectionPartitionSchemaRejected,
    connection_partition_schema,
    validate_connection_partition,
)
from core.application.extensions.oauth_authorization import (
    lock_replaceable_connection,
    requested_connection_scopes,
    require_authorizable_configuration,
)
from core.application.extensions.oauth_contract import (
    OAuthProviderContract,
    compile_oauth_providers,
    oauth_error_help_target,
    provider_security_fingerprint,
)
from core.application.extensions.oauth_contract_models import FLOW_REFRESH_TOKEN_IMPORT
from core.application.management.errors import (
    ServiceConflictError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServicePreconditionError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.management.scope import ScopeContext
from core.infrastructure.extension_runtime.oauth_transport import (
    OAuthErrorCode,
    OAuthProviderError,
)

from .oauth_callback_models import OAuthCallbackRepository, OAuthCallbackTokenStore
from .oauth_callback_policy import _required_connection_scopes, _scopes
from .oauth_grant_commit import OAuthGrant, OAuthGrantCommitter, oauth_client_credentials

_logger = logging.getLogger(__name__)

#: Long enough for any refresh token seen in practice, short enough that a
#: pasted document is refused rather than sent to a provider.
REFRESH_TOKEN_MAX_LENGTH = 8192

_MESSAGE_PROVIDER_NOT_FOUND = "OAuth provider not found"
_MESSAGE_INVALID_REFRESH_TOKEN = "The refresh token is not in a usable format"
_MESSAGE_REFRESH_TOKEN_REJECTED = "The provider did not accept this refresh token"

#: Provider refusals that are about the token itself.
_TOKEN_REFUSALS = frozenset(
    {
        OAuthErrorCode.INVALID_GRANT,
        OAuthErrorCode.AUTHORIZATION_REQUIRED,
        OAuthErrorCode.AUTHORIZATION_DENIED,
    }
)


class OAuthRefreshTokenImport(BaseModel):
    """One operator request to connect an account from a pasted refresh token."""

    model_config = ConfigDict(extra="forbid")

    configuration_id: UUID
    connection_type: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=200)
    #: Write-only. A SecretStr so that no repr, log line or validation error
    #: built from this model can carry the value.
    refresh_token: SecretStr
    connection_config: dict[str, Any] = Field(default_factory=dict)
    existing_connection_id: str | None = Field(default=None, max_length=200)
    connection_credential_revision: int | None = Field(default=None, ge=1)

    @field_validator("connection_type", "display_name", "existing_connection_id", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _replacement_names_its_revision(self) -> OAuthRefreshTokenImport:
        # Replacing a grant without saying which revision was seen would let a
        # stale form overwrite a newer one.
        if bool(self.existing_connection_id) != (self.connection_credential_revision is not None):
            raise ValueError(
                "existing_connection_id and connection_credential_revision go together"
            )
        return self


OAuthRefreshTokenImportOutcome = Literal[
    "connected",
    "needs_attention",
    "authorization_required",
    "restart_required",
]
_IMPORT_OUTCOMES = frozenset(
    {"connected", "needs_attention", "authorization_required", "restart_required"}
)


class OAuthRefreshTokenImportResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: `restart_required` here means the connection was saved but its
    #: post-connect action could not run; the retry action recovers it.
    outcome: OAuthRefreshTokenImportOutcome
    connection_id: str
    connection_type: str
    help_target: dict[str, str] | None = None


def _normalized_refresh_token(value: SecretStr) -> str:
    """The pasted token, trimmed, or a refusal that never repeats it."""
    token = value.get_secret_value().strip()
    if (
        not token
        or len(token) > REFRESH_TOKEN_MAX_LENGTH
        or any(not 0x21 <= ord(character) <= 0x7E for character in token)
    ):
        raise ServiceValidationError(
            _MESSAGE_INVALID_REFRESH_TOKEN,
            payload={"error": "invalid_oauth_refresh_token", "field": "refresh_token"},
        )
    return token


def _provider_refusal(provider: OAuthProviderContract, code: OAuthErrorCode) -> Exception:
    """A closed, token-free error for a failed exchange."""
    help_target = oauth_error_help_target(provider, code.value)
    if code in _TOKEN_REFUSALS:
        return ServiceValidationError(
            _MESSAGE_REFRESH_TOKEN_REJECTED,
            payload={
                "error": "oauth_refresh_token_rejected",
                "field": "refresh_token",
                "help_target": help_target,
            },
        )
    if code is OAuthErrorCode.INVALID_CLIENT:
        return ServicePreconditionError(
            "The provider did not accept this configuration's client credentials",
            payload={"error": "oauth_invalid_client", "help_target": help_target},
        )
    return ServiceUnavailableError(
        "The provider could not be reached",
        payload={"error": "oauth_provider_unavailable", "help_target": help_target},
    )


class OAuthRefreshTokenImportService:
    def __init__(
        self,
        *,
        repository: OAuthCallbackRepository,
        token_store: OAuthCallbackTokenStore,
        install_loader: Callable[[str, str], Mapping[str, Any] | None],
        secret_present: Callable[..., bool],
        configuration_secret_value: Callable[..., str],
        refresh_access_token: Callable[..., dict[str, Any]],
        transaction_factory: Callable[[], AbstractContextManager[Any]],
        invoke_post_connect: Callable[..., Mapping[str, Any]] | None = None,
        connection_id_factory: Callable[[], str] = lambda: f"conn_{secrets.token_hex(8)}",
    ) -> None:
        self._repository = repository
        self._install_loader = install_loader
        self._secret_present = secret_present
        self._configuration_secret_value = configuration_secret_value
        self._refresh_access_token = refresh_access_token
        self._transaction_factory = transaction_factory
        self._connection_id_factory = connection_id_factory
        self._grant_committer = OAuthGrantCommitter(
            repository=repository,
            token_store=token_store,
            install_loader=install_loader,
            transaction_factory=transaction_factory,
            invoke_post_connect=invoke_post_connect,
        )

    def _install_provider(
        self, *, install_id: str, account_id: str, provider_id: str
    ) -> tuple[dict[str, Any], dict[str, Any], OAuthProviderContract]:
        raw = self._install_loader(install_id, account_id)
        install = dict(raw) if isinstance(raw, Mapping) else {}
        descriptor_value = install.get("compiled_manifest") or install.get("manifest")
        descriptor = dict(descriptor_value) if isinstance(descriptor_value, Mapping) else {}
        if not install or not bool(install.get("enabled")) or not descriptor:
            raise ServiceNotFoundError(_MESSAGE_PROVIDER_NOT_FOUND)
        try:
            providers = compile_oauth_providers(descriptor)
        except ValueError as exc:
            raise ServiceValidationError(
                "Installed OAuth provider contract is invalid",
                payload={"error": "invalid_oauth_provider_contract"},
            ) from exc
        provider = next((row for row in providers if row.provider_id == provider_id), None)
        if provider is None:
            raise ServiceNotFoundError(_MESSAGE_PROVIDER_NOT_FOUND)
        if not provider.is_refresh_token_import:
            raise ServiceValidationError(
                "This OAuth provider connects through an authorization redirect",
                payload={"error": "unsupported_oauth_flow"},
            )
        return install, descriptor, provider

    @staticmethod
    def _validated_settings(
        descriptor: dict[str, Any], *, connection_type: str, settings: dict[str, Any]
    ) -> tuple[dict[str, Any], frozenset[str]]:
        """The submitted settings, held to exactly what the connection type declares.

        Returns the settings and the declared keys. Only declared keys are ever
        stored or replaced: a type without a config_schema accepts no settings,
        and a key the schema does not name is refused rather than kept, whatever
        `additionalProperties` says.
        """
        if "oauth" in settings:
            raise ServiceValidationError(
                "connection_config.oauth is owned by the platform",
                payload={
                    "error": "oauth_connection_config_reserved",
                    "field": "connection_config.oauth",
                },
            )
        try:
            schema = connection_partition_schema(
                descriptor, connection_type=connection_type, partition="config"
            )
        except ConnectionPartitionSchemaRejected:
            schema = {}
        properties = schema.get("properties") if isinstance(schema, dict) else None
        declared = frozenset(
            key for key in (properties if isinstance(properties, dict) else {}) if key != "oauth"
        )
        undeclared = sorted(key for key in settings if key not in declared)
        if undeclared:
            raise ServiceValidationError(
                "Connection settings name a field the connection type does not declare",
                payload={
                    "error": "invalid_connection_config",
                    "field": f"connection_config.{undeclared[0]}"
                    if declared
                    else "connection_config",
                },
            )
        errors = validate_connection_partition(
            descriptor, connection_type=connection_type, partition="config", value=settings
        )
        if errors:
            # Only the path: a message may quote the value, and an operator who
            # pasted a token into the wrong field must not see it echoed back.
            field = errors[0].split(":", 1)[0].strip() or "connection_config"
            raise ServiceValidationError(
                "Connection settings do not match the connection type",
                payload={"error": "invalid_connection_config", "field": field},
            )
        return dict(settings), declared

    def import_refresh_token(
        self,
        scope: ScopeContext,
        install_id: str,
        provider_id: str,
        command: OAuthRefreshTokenImport,
    ) -> OAuthRefreshTokenImportResult:
        if not principal_type_has_minimum_role(scope.principal_type, "operator"):
            raise ServiceForbiddenError("Operator role required")
        scope_values = {
            "tenant_id": str(scope.tenant_id or "").strip(),
            "account_id": str(scope.account_id or "").strip(),
            "project_id": str(scope.project_id or "").strip(),
        }
        if not all(scope_values.values()):
            raise ServiceNotFoundError(_MESSAGE_PROVIDER_NOT_FOUND)
        refresh_token = _normalized_refresh_token(command.refresh_token)
        normalized_install_id = str(install_id or "").strip()
        normalized_provider_id = str(provider_id or "").strip()
        configuration_id = str(command.configuration_id)
        install, _, _ = self._install_provider(
            install_id=normalized_install_id,
            account_id=scope_values["account_id"],
            provider_id=normalized_provider_id,
        )
        extension_id = str(install.get("extension_id") or "").strip()
        if not extension_id:
            raise ServiceNotFoundError(_MESSAGE_PROVIDER_NOT_FOUND)
        repository_scope = {
            **scope_values,
            "extension_install_id": normalized_install_id,
            "extension_id": extension_id,
            "provider_id": normalized_provider_id,
            "configuration_id": configuration_id,
        }

        # Every check runs under the configuration lock, and the lock is let go
        # before the provider is called: nothing is written until the provider
        # has answered, and the commit repeats every lineage check under lock.
        with self._transaction_factory():
            configuration = self._repository.get_configuration_for_scope_for_update(
                **repository_scope
            )
            if not configuration:
                raise ServiceNotFoundError("Configuration not found")
            install, descriptor, provider = self._install_provider(
                install_id=normalized_install_id,
                account_id=scope_values["account_id"],
                provider_id=normalized_provider_id,
            )
            if str(install.get("extension_id") or "").strip() != extension_id:
                raise ServiceNotFoundError(_MESSAGE_PROVIDER_NOT_FOUND)
            release_id = str(install.get("release_id") or "").strip()
            require_authorizable_configuration(
                configuration,
                provider=provider,
                release_id=release_id,
                configuration_id=configuration_id,
                account_id=scope_values["account_id"],
                secret_present=self._secret_present,
            )
            requested_scopes = requested_connection_scopes(
                descriptor,
                connection_type=command.connection_type,
                provider_id=normalized_provider_id,
            )
            settings, declared_settings = self._validated_settings(
                descriptor,
                connection_type=command.connection_type,
                settings=command.connection_config,
            )
            existing_connection_id = str(command.existing_connection_id or "").strip()
            expected_fingerprint = provider_security_fingerprint(provider)
            if existing_connection_id:
                connection = lock_replaceable_connection(
                    self._repository,
                    connection_id=existing_connection_id,
                    repository_scope=repository_scope,
                    connection_type=command.connection_type,
                    release_id=release_id,
                    expected_fingerprint=expected_fingerprint,
                )
                if int(connection.get("credential_revision") or 0) != int(
                    command.connection_credential_revision or 0
                ):
                    raise ServiceConflictError(
                        "The connection changed since this form was opened",
                        payload={"error": "oauth_connection_revision_conflict"},
                    )
            credentials = oauth_client_credentials(
                configuration,
                provider=provider,
                configuration_id=configuration_id,
                account_id=scope_values["account_id"],
                configuration_secret_value=self._configuration_secret_value,
            )
            if credentials is None:
                raise ServicePreconditionError(
                    "OAuth configuration is not ready",
                    payload={"error": "oauth_configuration_not_ready"},
                )
            state: dict[str, Any] = {
                **repository_scope,
                "extension_release_id": release_id,
                "configuration_revision": int(configuration.get("configuration_revision") or 0),
                "authorization_revision": int(configuration.get("authorization_revision") or 0),
                "provider_security_fingerprint": expected_fingerprint,
                "callback_id": provider.callback_id,
                "flow_type": FLOW_REFRESH_TOKEN_IMPORT,
                "connection_type": command.connection_type,
                "intent_id": "",
                "existing_connection_id": existing_connection_id,
                "display_name": command.display_name,
                "requested_scopes": requested_scopes,
                "intent_payload": (
                    {"connection_credential_revision": command.connection_credential_revision}
                    if existing_connection_id
                    else {}
                ),
            }

        required_scopes = _required_connection_scopes(
            descriptor,
            connection_type=command.connection_type,
            provider_id=normalized_provider_id,
        )
        if required_scopes is None:
            raise ServiceValidationError(
                "Installed OAuth connection scope contract is invalid",
                payload={"error": "invalid_oauth_provider_contract"},
            )
        # A provider may rotate the refresh token on this very call, so a state
        # that could never commit must not reach it. The commit repeats the
        # same checks under its own lock; this only narrows the window.
        if not self._grant_committer.lineage_is_current(state):
            raise ServiceConflictError(
                "The OAuth configuration changed while connecting; try again",
                payload={"error": "oauth_import_conflict"},
            )
        client_id, client_secret = credentials
        try:
            token_response = self._refresh_access_token(
                endpoint=provider.token_endpoint,
                refresh_token=refresh_token,
                client_id=client_id,
                client_secret=client_secret,
                implicit_token_type=provider.code_exchange.implicit_token_type,
                required_response_fields=provider.code_exchange.required_response_fields,
            )
        except OAuthProviderError as exc:
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- only the closed OAuthErrorCode value is logged, never the token.
            _logger.warning("OAuth refresh-token import failed: code=%s", exc.code.value)
            if exc.code is OAuthErrorCode.INVALID_CLIENT:
                # The same record runtime refresh and the callback make, so the
                # configuration shows it needs repair. The route is operator-only.
                self._grant_committer.mark_invalid_client(state)
            raise _provider_refusal(provider, exc.code) from None
        except Exception:
            raise _provider_refusal(provider, OAuthErrorCode.PROVIDER_UNAVAILABLE) from None

        # Nothing was requested from the provider, so an omitted `scope` can only
        # stand for the declared requirement — never for optional scopes, which
        # would be invented. With no requirement, no grant is recorded at all.
        try:
            granted_scopes = _scopes(
                token_response.get("scope"),
                fallback=sorted(required_scopes),
                provider=provider,
            )
        except ValueError:
            raise _provider_refusal(provider, OAuthErrorCode.MALFORMED_RESPONSE) from None
        if not required_scopes.issubset(granted_scopes):
            raise ServicePreconditionError(
                "The refresh token does not grant every scope this connection type requires",
                payload={
                    "error": "oauth_scope_required",
                    "help_target": oauth_error_help_target(provider, "authorization_required"),
                },
            )

        connection_id = existing_connection_id or str(self._connection_id_factory() or "").strip()
        result = self._grant_committer.complete(
            state=state,
            grant=OAuthGrant(
                token_response=token_response,
                requested_scopes=requested_scopes,
                granted_scopes=granted_scopes,
                connection_id=connection_id,
                fallback_refresh_token=refresh_token,
                connection_settings=settings,
                replaced_setting_keys=declared_settings,
                keep_identity_until_verified=True,
            ),
            return_page_id=None,
        )
        if not result.connection_id or result.outcome not in _IMPORT_OUTCOMES:
            # The commit found the configuration, install or connection changed
            # under it, and nothing was written. If the provider answered with a
            # new refresh token it may already have retired the pasted one, so
            # the operator is told a fresh token may be needed.
            rotated = str(token_response.get("refresh_token") or "")
            if rotated and rotated != refresh_token:
                raise ServiceConflictError(
                    "The configuration changed while connecting and the provider "
                    "may have rotated the refresh token; issue a new one if retrying fails",
                    payload={"error": "oauth_import_conflict_token_rotated"},
                )
            raise ServiceConflictError(
                "The OAuth configuration changed while connecting; try again",
                payload={"error": "oauth_import_conflict"},
            )
        return OAuthRefreshTokenImportResult(
            outcome=cast(OAuthRefreshTokenImportOutcome, result.outcome),
            connection_id=result.connection_id,
            connection_type=command.connection_type,
            help_target=(
                oauth_error_help_target(provider, result.outcome)
                if result.outcome in {"needs_attention", "authorization_required"}
                else None
            ),
        )


__all__ = [
    "REFRESH_TOKEN_MAX_LENGTH",
    "OAuthRefreshTokenImport",
    "OAuthRefreshTokenImportResult",
    "OAuthRefreshTokenImportService",
]
