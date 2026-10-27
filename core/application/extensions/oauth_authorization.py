"""Project-scoped authorization start for extension-owned OAuth providers."""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlencode
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.application.auth.principal import principal_type_has_minimum_role
from core.application.extensions.callbacks import (
    OAuthCallbackPublicBaseURLUnavailable,
    OAuthCallbackRegistrationPreconditionError,
    OAuthCallbackRegistrationStorageError,
)
from core.application.extensions.oauth_configuration_secrets import (
    configuration_secret_ref_key,
)
from core.application.extensions.oauth_contract import (
    OAUTH_ID_MAX_LENGTH,
    OAUTH_ID_RE,
    OAuthProviderContract,
    compile_oauth_providers,
    contract_section,
    declared_ui_page_ids,
    provider_security_fingerprint,
    resolve_authorization_origin,
    validate_authorization_intent_inputs,
)
from core.application.management.errors import (
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServicePreconditionError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.management.scope import ScopeContext

_MESSAGE_OAUTH_PROVIDER_NOT_FOUND = "OAuth provider not found"
_MESSAGE_INSTALLED_OAUTH_CONNECTION_SCOPE_CONTRACT_IS_INVALID = (
    "Installed OAuth connection scope contract is invalid"
)
_MESSAGE_CONNECTION_NOT_FOUND = "Connection not found"

_AUTHORIZATION_TTL = timedelta(minutes=10)
_MAX_AUTHORIZATION_URL_LENGTH = 8192


class OAuthAuthorizationStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    configuration_id: UUID
    connection_type: str = Field(min_length=1, max_length=64)
    intent_id: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=200)
    return_page_id: str = Field(min_length=1, max_length=OAUTH_ID_MAX_LENGTH)
    intent_inputs: dict[str, str] = Field(default_factory=dict)
    existing_connection_id: str | None = Field(default=None, max_length=200)

    @field_validator(
        "configuration_id",
        "connection_type",
        "intent_id",
        "display_name",
        "return_page_id",
        "existing_connection_id",
        mode="before",
    )
    @classmethod
    def _strip_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        return str(value).strip()

    @field_validator("intent_inputs", mode="before")
    @classmethod
    def _strip_intent_input_values(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        return {key: raw.strip() if isinstance(raw, str) else raw for key, raw in value.items()}


class OAuthAuthorizationStartResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    authorization_url: str
    expires_at: datetime
    redirect_uri: str


class OAuthRedirectURIResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    redirect_uri: str


class OAuthProviderSetupStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    callback_registration_ready: bool


class OAuthAuthorizationRepository(Protocol):
    @property
    def get_configuration_for_scope_for_update(
        self,
    ) -> Callable[..., dict[str, Any] | None]: ...

    @property
    def lock_connection(self) -> Callable[..., dict[str, Any] | None]: ...

    @property
    def create_state(self) -> Callable[..., dict[str, Any] | None]: ...


def _default_state_token() -> str:
    return secrets.token_urlsafe(32)


def _default_verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode("ascii")


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _scalar_parameter(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    raise ServiceValidationError(
        "Installed OAuth provider contract is invalid",
        payload={"error": "invalid_oauth_provider_contract"},
    )


class OAuthAuthorizationService:
    def __init__(
        self,
        *,
        repository: OAuthAuthorizationRepository,
        install_loader: Callable[[str, str], Mapping[str, Any] | None],
        register_callback: Callable[..., str],
        secret_present: Callable[..., bool],
        encrypt_verifier: Callable[[str], str],
        transaction_factory: Callable[[], AbstractContextManager[Any]],
        callback_registration_ready: Callable[..., bool] = lambda **_values: False,
        state_token_factory: Callable[[], str] = _default_state_token,
        verifier_factory: Callable[[], str] = _default_verifier,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._install_loader = install_loader
        self._register_callback = register_callback
        self._secret_present = secret_present
        self._encrypt_verifier = encrypt_verifier
        self._transaction_factory = transaction_factory
        self._callback_registration_ready = callback_registration_ready
        self._state_token_factory = state_token_factory
        self._verifier_factory = verifier_factory
        self._now = now

    @staticmethod
    def _scope_values(scope: ScopeContext) -> dict[str, str]:
        values = {
            "tenant_id": str(scope.tenant_id or "").strip(),
            "account_id": str(scope.account_id or "").strip(),
            "project_id": str(scope.project_id or "").strip(),
        }
        if not all(values.values()):
            raise ServiceNotFoundError(_MESSAGE_OAUTH_PROVIDER_NOT_FOUND)
        return values

    @staticmethod
    def _require_operator(scope: ScopeContext) -> None:
        if not principal_type_has_minimum_role(scope.principal_type, "operator"):
            raise ServiceForbiddenError("Operator role required")

    def _load_install(
        self, *, install_id: str, account_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        raw = self._install_loader(install_id, account_id)
        install = dict(raw) if isinstance(raw, Mapping) else {}
        if not install or not bool(install.get("enabled")):
            raise ServiceNotFoundError(_MESSAGE_OAUTH_PROVIDER_NOT_FOUND)
        descriptor_value = install.get("compiled_manifest") or install.get("manifest")
        descriptor = dict(descriptor_value) if isinstance(descriptor_value, Mapping) else {}
        if not descriptor:
            raise ServiceNotFoundError(_MESSAGE_OAUTH_PROVIDER_NOT_FOUND)
        return install, descriptor

    @staticmethod
    def _provider(descriptor: dict[str, Any], provider_id: str) -> OAuthProviderContract:
        try:
            providers = compile_oauth_providers(descriptor)
        except ValueError as exc:
            raise ServiceValidationError(
                "Installed OAuth provider contract is invalid",
                payload={"error": "invalid_oauth_provider_contract"},
            ) from exc
        provider = next((row for row in providers if row.provider_id == provider_id), None)
        if provider is None:
            raise ServiceNotFoundError(_MESSAGE_OAUTH_PROVIDER_NOT_FOUND)
        return provider

    @staticmethod
    def _return_page_owns_intent(
        descriptor: dict[str, Any],
        *,
        page_id: str,
        provider_id: str,
        connection_type: str,
        intent_id: str,
    ) -> bool:
        ui_manifest = descriptor.get("ui_manifest")
        pages = ui_manifest.get("pages") if isinstance(ui_manifest, Mapping) else []
        matches = [
            page
            for page in (pages if isinstance(pages, list) else [])
            if isinstance(page, Mapping) and str(page.get("page_id") or "").strip() == page_id
        ]
        if len(matches) != 1:
            return False
        components = matches[0].get("components")
        for component in components if isinstance(components, list) else []:
            if not isinstance(component, Mapping) or component.get("type") != "connection_form":
                continue
            data = component.get("data")
            if not isinstance(data, Mapping):
                continue
            if (
                str(data.get("oauth_provider_id") or "").strip() == provider_id
                and str(data.get("connection_type") or "").strip() == connection_type
                and str(data.get("oauth_intent") or "").strip() == intent_id
            ):
                return True
        return False

    @classmethod
    def _requested_scopes(
        cls,
        descriptor: dict[str, Any],
        *,
        connection_type: str,
        provider_id: str,
    ) -> list[str]:
        # Accepts a section declared inline or resolved from a bundle file; a
        # referenced one leaves a filename here and used to read as "no types".
        rows = contract_section(descriptor, "connection_types")
        matches = [
            row
            for row in rows
            if isinstance(row, dict)
            and str(row.get("connection_type_id") or "").strip() == connection_type
        ]
        if len(matches) != 1:
            raise ServiceValidationError(
                "Unknown OAuth connection type",
                payload={"error": "unknown_oauth_connection_type", "field": "connection_type"},
            )
        auth = matches[0].get("auth")
        if not isinstance(auth, dict) or str(auth.get("type") or "").lower() != "oauth2":
            raise ServiceValidationError(
                "Connection type does not use OAuth",
                payload={"error": "invalid_oauth_connection_type", "field": "connection_type"},
            )
        if str(auth.get("provider_id") or "").strip() != provider_id:
            raise ServiceValidationError(
                "Connection type does not use this OAuth provider",
                payload={"error": "invalid_oauth_connection_type", "field": "connection_type"},
            )
        scopes = auth.get("scopes")
        if not isinstance(scopes, dict):
            raise ServiceValidationError(
                _MESSAGE_INSTALLED_OAUTH_CONNECTION_SCOPE_CONTRACT_IS_INVALID,
                payload={"error": "invalid_oauth_provider_contract"},
            )
        result: list[str] = []
        for group in ("required", "optional"):
            values = scopes.get(group)
            if not isinstance(values, list):
                raise ServiceValidationError(
                    _MESSAGE_INSTALLED_OAUTH_CONNECTION_SCOPE_CONTRACT_IS_INVALID,
                    payload={"error": "invalid_oauth_provider_contract"},
                )
            for value in values:
                normalized = str(value or "").strip()
                if not normalized or normalized in result:
                    raise ServiceValidationError(
                        _MESSAGE_INSTALLED_OAUTH_CONNECTION_SCOPE_CONTRACT_IS_INVALID,
                        payload={"error": "invalid_oauth_provider_contract"},
                    )
                result.append(normalized)
        return result

    @staticmethod
    def _authorization_origin(
        provider: OAuthProviderContract,
        *,
        intent_id: str,
        intent_inputs: Mapping[str, Any],
    ) -> str:
        """The canonical origin the flow is bound to, or "" for a fixed provider."""
        try:
            return resolve_authorization_origin(
                provider,
                intent_id=intent_id,
                values=dict(intent_inputs),
            )
        except ValueError as exc:
            code, _, field = str(exc).partition(":")
            safe_code = (
                code
                if code in {"unknown_oauth_intent", "invalid_oauth_intent_input_value"}
                else "invalid_oauth_intent_input_value"
            )
            payload: dict[str, Any] = {"error": safe_code}
            if field:
                payload["field"] = field
            raise ServiceValidationError(
                "Invalid OAuth authorization input", payload=payload
            ) from exc

    @staticmethod
    def _mapped_inputs(
        provider: OAuthProviderContract,
        *,
        intent_id: str,
        intent_inputs: Mapping[str, Any],
    ) -> dict[str, str]:
        try:
            return validate_authorization_intent_inputs(
                provider,
                intent_id=intent_id,
                values=dict(intent_inputs),
            )
        except ValueError as exc:
            code, _, field = str(exc).partition(":")
            safe_code = (
                code
                if code
                in {
                    "unknown_oauth_intent",
                    "unknown_oauth_intent_input",
                    "invalid_oauth_intent_input_value",
                }
                else "invalid_oauth_intent_input_value"
            )
            payload = {"error": safe_code, "field": field or "intent_id"}
            raise ServiceValidationError(
                "Invalid OAuth authorization intent", payload=payload
            ) from exc

    def _reconnect_intent_inputs(
        self,
        *,
        provider: OAuthProviderContract,
        command: OAuthAuthorizationStart,
        connection: Mapping[str, Any],
    ) -> dict[str, Any]:
        values: dict[str, Any] = dict(command.intent_inputs)
        binding = provider.origin_binding
        if binding is None:
            return values

        connection_config = connection.get("connection_config")
        oauth_config = (
            connection_config.get("oauth") if isinstance(connection_config, Mapping) else None
        )
        raw_saved_subject = (
            oauth_config.get("subject") if isinstance(oauth_config, Mapping) else None
        )
        saved_subject = raw_saved_subject if isinstance(raw_saved_subject, str) else ""
        saved_origin = binding.normalize(saved_subject)
        if not saved_origin or saved_origin != saved_subject:
            raise ServicePreconditionError(
                "OAuth connection cannot be safely reauthorized",
                payload={"error": "oauth_reconnect_subject_unavailable"},
            )

        supplied = values.get(binding.input_id)
        if supplied is not None:
            supplied_values = {binding.input_id: supplied}
            self._mapped_inputs(
                provider,
                intent_id=command.intent_id,
                intent_inputs=supplied_values,
            )
            supplied_origin = self._authorization_origin(
                provider,
                intent_id=command.intent_id,
                intent_inputs=supplied_values,
            )
            if supplied_origin != saved_origin:
                raise ServiceValidationError(
                    "OAuth reconnect must preserve the verified subject",
                    payload={
                        "error": "oauth_reconnect_subject_mismatch",
                        "field": binding.input_id,
                    },
                )

        values[binding.input_id] = saved_origin[: -len(binding.host_suffix)]
        return values

    def _register_redirect_uri(
        self, *, project_id: str, extension_id: str, callback_id: str
    ) -> str:
        try:
            redirect_uri = str(
                self._register_callback(
                    project_id=project_id,
                    extension_id=extension_id,
                    callback_id=callback_id,
                )
                or ""
            ).strip()
        except OAuthCallbackRegistrationPreconditionError as exc:
            raise ServicePreconditionError(
                "OAuth callback registration is unavailable",
                payload={"error": "oauth_callback_registration_unavailable"},
            ) from exc
        except OAuthCallbackRegistrationStorageError as exc:
            raise ServiceUnavailableError(
                "OAuth callback registration storage is unavailable",
                payload={"error": "oauth_callback_registration_storage_unavailable"},
            ) from exc
        except OAuthCallbackPublicBaseURLUnavailable as exc:
            raise ServiceUnavailableError(
                "OAuth public callback URL is unavailable",
                payload={"error": "oauth_public_base_url_unavailable", "reason": exc.reason},
            ) from exc
        if not redirect_uri:
            raise ServicePreconditionError(
                "OAuth callback registration is unavailable",
                payload={"error": "oauth_callback_registration_unavailable"},
            )
        return redirect_uri

    def start_authorization(
        self,
        scope: ScopeContext,
        install_id: str,
        provider_id: str,
        command: OAuthAuthorizationStart,
    ) -> OAuthAuthorizationStartResult:
        self._require_operator(scope)
        scope_values = self._scope_values(scope)
        normalized_install_id = str(install_id or "").strip()
        normalized_provider_id = str(provider_id or "").strip()
        initial_install, initial_descriptor = self._load_install(
            install_id=normalized_install_id,
            account_id=scope_values["account_id"],
        )
        extension_id = str(initial_install.get("extension_id") or "").strip()
        if not extension_id:
            raise ServiceNotFoundError(_MESSAGE_OAUTH_PROVIDER_NOT_FOUND)
        self._provider(initial_descriptor, normalized_provider_id)
        repository_scope = {
            **scope_values,
            "extension_install_id": normalized_install_id,
            "extension_id": extension_id,
            "provider_id": normalized_provider_id,
            "configuration_id": str(command.configuration_id),
        }

        with self._transaction_factory():
            configuration = self._repository.get_configuration_for_scope_for_update(
                **repository_scope
            )
            if not configuration:
                raise ServiceNotFoundError("Configuration not found")

            install, descriptor = self._load_install(
                install_id=normalized_install_id,
                account_id=scope_values["account_id"],
            )
            if str(install.get("extension_id") or "").strip() != extension_id:
                raise ServiceNotFoundError(_MESSAGE_OAUTH_PROVIDER_NOT_FOUND)
            provider = self._provider(descriptor, normalized_provider_id)
            if (
                OAUTH_ID_RE.fullmatch(command.return_page_id) is None
                or command.return_page_id not in declared_ui_page_ids(descriptor)
                or not self._return_page_owns_intent(
                    descriptor,
                    page_id=command.return_page_id,
                    provider_id=normalized_provider_id,
                    connection_type=command.connection_type,
                    intent_id=command.intent_id,
                )
            ):
                raise ServiceValidationError(
                    "Invalid OAuth return page",
                    payload={"error": "invalid_oauth_return_page", "field": "return_page_id"},
                )
            release_id = str(install.get("release_id") or "").strip()
            expected_fingerprint = provider_security_fingerprint(provider)
            if (
                not release_id
                or str(configuration.get("extension_release_id") or "").strip() != release_id
                or str(configuration.get("provider_security_fingerprint") or "").strip()
                != expected_fingerprint
            ):
                raise ServicePreconditionError(
                    "OAuth configuration requires release reconciliation",
                    payload={"error": "oauth_release_transition_required"},
                )
            if str(configuration.get("status") or "").strip() not in {
                "ready_for_authorization",
                "verified",
            }:
                raise ServicePreconditionError(
                    "OAuth configuration is not ready",
                    payload={"error": "oauth_configuration_not_ready"},
                )
            non_secret_config = configuration.get("non_secret_config")
            non_secret_config = non_secret_config if isinstance(non_secret_config, Mapping) else {}
            secret_refs = configuration.get("secret_refs")
            secret_refs = secret_refs if isinstance(secret_refs, Mapping) else {}
            nested_refs = secret_refs.get("refs")
            secret_refs = nested_refs if isinstance(nested_refs, Mapping) else {}
            for field in provider.credential_fields:
                if not field.required:
                    continue
                if field.secret:
                    ref_key = str(secret_refs.get(field.field_id) or "").strip()
                    expected_ref = configuration_secret_ref_key(
                        str(command.configuration_id), field.field_id
                    )
                    available = bool(
                        ref_key == expected_ref
                        and self._secret_present(
                            account_id=scope_values["account_id"], ref_key=ref_key
                        )
                    )
                else:
                    available = bool(str(non_secret_config.get(field.field_id) or "").strip())
                if not available:
                    raise ServicePreconditionError(
                        "OAuth configuration is not ready",
                        payload={"error": "oauth_configuration_not_ready"},
                    )
            client_id = str(non_secret_config.get("client_id") or "").strip()

            requested_scopes = self._requested_scopes(
                descriptor,
                connection_type=command.connection_type,
                provider_id=normalized_provider_id,
            )
            existing_connection_id = str(command.existing_connection_id or "").strip() or None
            connection_credential_revision: int | None = None
            effective_intent_inputs: dict[str, Any] = dict(command.intent_inputs)
            if existing_connection_id:
                connection = self._repository.lock_connection(
                    connection_id=existing_connection_id,
                    **repository_scope,
                    connection_type=command.connection_type,
                )
                if not connection:
                    raise ServiceNotFoundError(_MESSAGE_CONNECTION_NOT_FOUND)
                expected_connection_scope = {
                    "connection_id": existing_connection_id,
                    "tenant_id": scope_values["tenant_id"],
                    "account_id": scope_values["account_id"],
                    "project_id": scope_values["project_id"],
                    "extension_install_id": normalized_install_id,
                    "extension_id": extension_id,
                    "oauth_provider_id": normalized_provider_id,
                    "oauth_configuration_id": str(command.configuration_id),
                    "oauth_extension_release_id": release_id,
                    "oauth_provider_security_fingerprint": expected_fingerprint,
                    "connection_type": command.connection_type,
                }
                if any(
                    str(connection.get(key) or "").strip() != expected
                    for key, expected in expected_connection_scope.items()
                ):
                    raise ServiceNotFoundError(_MESSAGE_CONNECTION_NOT_FOUND)
                connection_credential_revision = int(connection.get("credential_revision") or 0)
                if connection_credential_revision < 1:
                    raise ServiceNotFoundError(_MESSAGE_CONNECTION_NOT_FOUND)
                connection_config = connection.get("connection_config")
                oauth_config = (
                    connection_config.get("oauth")
                    if isinstance(connection_config, Mapping)
                    else None
                )
                lifecycle_status = str(
                    oauth_config.get("lifecycle_status")
                    if isinstance(oauth_config, Mapping)
                    else ""
                ).strip()
                if connection.get("enabled") is not True or lifecycle_status == "disconnecting":
                    raise ServicePreconditionError(
                        "OAuth connection disconnect is in progress",
                        payload={"error": "oauth_connection_disconnect_in_progress"},
                    )
                effective_intent_inputs = self._reconnect_intent_inputs(
                    provider=provider,
                    command=command,
                    connection=connection,
                )

            mapped_inputs = self._mapped_inputs(
                provider,
                intent_id=command.intent_id,
                intent_inputs=effective_intent_inputs,
            )
            authorization_origin = self._authorization_origin(
                provider,
                intent_id=command.intent_id,
                intent_inputs=effective_intent_inputs,
            )

            redirect_uri = self._register_redirect_uri(
                project_id=scope_values["project_id"],
                extension_id=extension_id,
                callback_id=provider.callback_id,
            )
            state = self._state_token_factory()
            verifier = self._verifier_factory()
            if len(state) < 32 or len(state) > 256 or len(verifier) < 43 or len(verifier) > 128:
                raise RuntimeError("Secure OAuth state generation failed")
            expires_at = self._now() + _AUTHORIZATION_TTL
            intent = provider.intents.get(command.intent_id)
            if intent is None:
                raise ServiceValidationError(
                    "Invalid OAuth authorization intent",
                    payload={"error": "unknown_oauth_intent", "field": "intent_id"},
                )
            parameters = {
                str(key): _scalar_parameter(value)
                for key, value in intent.static_parameters.items()
            }
            parameters.update(mapped_inputs)
            parameters.update(
                {
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "response_type": "code",
                    "scope": provider.join_scopes(requested_scopes),
                    "state": state,
                }
            )
            if provider.uses_pkce:
                parameters.update(
                    {
                        "code_challenge": _code_challenge(verifier),
                        "code_challenge_method": "S256",
                    }
                )
            # The origin is resolved before the URL is built and stored with the
            # state, so the callback has something to compare against that the
            # caller cannot influence later.
            authorization_base = provider.authorization_endpoint.resolve(authorization_origin)
            if not authorization_base:
                raise ServiceValidationError(
                    "OAuth authorization endpoint is unavailable",
                    payload={"error": "oauth_authorization_endpoint_unavailable"},
                )
            authorization_url = f"{authorization_base}?{urlencode(parameters)}"
            if len(authorization_url) > _MAX_AUTHORIZATION_URL_LENGTH:
                raise ServiceValidationError(
                    "OAuth authorization request is too large",
                    payload={"error": "oauth_authorization_url_too_large"},
                )
            intent_payload: dict[str, Any] = {
                "authorization_parameters": dict(mapped_inputs),
                "return_page_id": command.return_page_id,
            }
            if authorization_origin:
                intent_payload["authorization_origin"] = authorization_origin
            if connection_credential_revision is not None:
                intent_payload["connection_credential_revision"] = connection_credential_revision
            created = self._repository.create_state(
                state_hash=hashlib.sha256(state.encode("utf-8")).hexdigest(),
                **repository_scope,
                extension_release_id=release_id,
                callback_id=provider.callback_id,
                configuration_revision=int(configuration.get("configuration_revision") or 0),
                authorization_revision=int(configuration.get("authorization_revision") or 0),
                connection_type=command.connection_type,
                intent_id=command.intent_id,
                existing_connection_id=existing_connection_id,
                display_name=command.display_name,
                intent_payload=intent_payload,
                requested_scopes=requested_scopes,
                redirect_uri=redirect_uri,
                # A provider that does not use PKCE stores no verifier at all
                # rather than an unused one nobody will ever check.
                pkce_verifier_encrypted=(
                    self._encrypt_verifier(verifier) if provider.uses_pkce else None
                ),
                provider_security_fingerprint=expected_fingerprint,
                expires_at=expires_at,
            )
            if not created:
                raise RuntimeError("OAuth authorization state storage is unavailable")

        return OAuthAuthorizationStartResult(
            authorization_url=authorization_url,
            expires_at=expires_at,
            redirect_uri=redirect_uri,
        )

    def ensure_redirect_uri(
        self,
        scope: ScopeContext,
        install_id: str,
        provider_id: str,
    ) -> OAuthRedirectURIResult:
        """Ensure and return the stable core-owned provider callback URI."""
        self._require_operator(scope)
        scope_values = self._scope_values(scope)
        normalized_install_id = str(install_id or "").strip()
        normalized_provider_id = str(provider_id or "").strip()
        install, descriptor = self._load_install(
            install_id=normalized_install_id,
            account_id=scope_values["account_id"],
        )
        extension_id = str(install.get("extension_id") or "").strip()
        if not extension_id:
            raise ServiceNotFoundError(_MESSAGE_OAUTH_PROVIDER_NOT_FOUND)
        provider = self._provider(descriptor, normalized_provider_id)
        with self._transaction_factory():
            current_install, current_descriptor = self._load_install(
                install_id=normalized_install_id,
                account_id=scope_values["account_id"],
            )
            if (
                str(current_install.get("extension_id") or "").strip() != extension_id
                or str(current_install.get("release_id") or "").strip()
                != str(install.get("release_id") or "").strip()
            ):
                raise ServicePreconditionError(
                    "OAuth extension release changed",
                    payload={"error": "oauth_release_transition_required"},
                )
            current_provider = self._provider(current_descriptor, normalized_provider_id)
            if current_provider.callback_id != provider.callback_id:
                raise ServicePreconditionError(
                    "OAuth extension release changed",
                    payload={"error": "oauth_release_transition_required"},
                )
            redirect_uri = self._register_redirect_uri(
                project_id=scope_values["project_id"],
                extension_id=extension_id,
                callback_id=current_provider.callback_id,
            )
        return OAuthRedirectURIResult(redirect_uri=redirect_uri)

    def provider_setup_status(
        self,
        scope: ScopeContext,
        install_id: str,
        provider_id: str,
    ) -> OAuthProviderSetupStatus:
        """Return viewer-safe readiness without creating callback state."""
        scope_values = self._scope_values(scope)
        install, descriptor = self._load_install(
            install_id=str(install_id or "").strip(),
            account_id=scope_values["account_id"],
        )
        extension_id = str(install.get("extension_id") or "").strip()
        provider = self._provider(descriptor, str(provider_id or "").strip())
        ready = False
        try:
            ready = bool(
                self._callback_registration_ready(
                    project_id=scope_values["project_id"],
                    extension_id=extension_id,
                    callback_id=provider.callback_id,
                )
            )
        except Exception:
            ready = False
        return OAuthProviderSetupStatus(callback_registration_ready=ready)


__all__ = [
    "OAuthAuthorizationService",
    "OAuthAuthorizationStart",
    "OAuthAuthorizationStartResult",
    "OAuthProviderSetupStatus",
    "OAuthRedirectURIResult",
]
