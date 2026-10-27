"""Fail-closed runtime projection for extension-owned OAuth access tokens."""

from __future__ import annotations

import math
import secrets
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Literal, Protocol

from core.application.extensions.oauth_callback_policy import (
    _configuration_lineage_matches,
    _connection_matches,
    _connection_owned_refs,
    _mapping,
    _required_connection_scopes,
    _scopes,
    _text,
)
from core.application.extensions.oauth_configuration_secrets import (
    configuration_secret_ref_key,
)
from core.application.extensions.oauth_contract import (
    OAuthProviderContract,
    compile_oauth_providers,
    provider_revocation_fingerprint,
    provider_security_fingerprint,
)
from core.infrastructure.extension_runtime.oauth_transport import OAuthProviderError


class OAuthRuntimeTokenError(RuntimeError):
    """Closed runtime denial whose code is safe to expose to first-party callers."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class OAuthRuntimeRepository(Protocol):
    def oauth_refresh_claim(self, **scope: Any) -> AbstractContextManager[Any]: ...

    def oauth_provider_token_claim(
        self, *, configuration_id: str, subject: str
    ) -> AbstractContextManager[None]: ...

    def lock_oauth_install_for_update(self, **scope: Any) -> dict[str, Any] | None: ...

    def get_configuration_for_scope_for_update(
        self,
        *,
        tenant_id: Any,
        account_id: Any,
        project_id: Any,
        extension_install_id: str,
        extension_id: str,
        provider_id: str,
        configuration_id: Any,
    ) -> dict[str, Any] | None: ...

    def update_oauth_refresh(self, **values: Any) -> dict[str, Any] | None: ...

    def update_oauth_refresh_failure(self, **values: Any) -> dict[str, Any] | None: ...

    def lock_connection(
        self,
        *,
        connection_id: str,
        tenant_id: Any,
        account_id: Any,
        project_id: Any,
        extension_install_id: str,
        extension_id: str,
        provider_id: str,
        configuration_id: Any,
        connection_type: str,
    ) -> dict[str, Any] | None: ...


class OAuthRuntimeTokenStore(Protocol):
    def resolve_access_token(self, *, account_id: str, secret_refs: Any) -> str: ...

    def resolve_token_snapshot(self, *, account_id: str, secret_refs: Any) -> dict[str, Any]: ...

    def write_tokens(
        self,
        *,
        account_id: str,
        connection_id: str,
        token_response: dict[str, Any],
        existing_refresh_token: str,
    ) -> dict[str, Any]: ...


class OAuthRuntimeTokenService:
    """Resolve one exact connection's token after revalidating its current grant."""

    def __init__(
        self,
        *,
        repository: OAuthRuntimeRepository,
        token_store: OAuthRuntimeTokenStore,
        install_loader: Callable[[str, str], Mapping[str, Any] | None],
        transaction_factory: Callable[[], AbstractContextManager[Any]],
        configuration_secret_value: Callable[..., str] | None = None,
        refresh_access_token: Callable[..., dict[str, Any]] | None = None,
        now: Callable[[], float] = time.time,
        refresh_skew_seconds: int = 60,
    ) -> None:
        self._repository = repository
        self._token_store = token_store
        self._install_loader = install_loader
        self._transaction_factory = transaction_factory
        self._configuration_secret_value = configuration_secret_value
        self._refresh_access_token = refresh_access_token
        self._now = now
        self._refresh_skew_seconds = max(0, min(int(refresh_skew_seconds), 300))

    @staticmethod
    def _deny(code: str = "oauth_runtime_blocked") -> None:
        raise OAuthRuntimeTokenError(code)

    def _validated_rows(
        self,
        *,
        scope: dict[str, str],
        connection_id: str,
        connection_type: str,
        expected_configuration_revision: int | None = None,
        expected_credential_revision: int | None = None,
        recovery_purpose: Literal["post_connect_retry"] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], OAuthProviderContract, dict[str, Any], int]:
        locked_install = _mapping(self._repository.lock_oauth_install_for_update(**scope))
        install = _mapping(self._install_loader(scope["extension_install_id"], scope["account_id"]))
        release_id = _text(install.get("release_id"))
        if (
            not locked_install
            or _text(locked_install.get("install_id")) != scope["extension_install_id"]
            or _text(locked_install.get("account_id")) != scope["account_id"]
            or _text(locked_install.get("extension_id")) != scope["extension_id"]
            or _text(locked_install.get("release_id")) != release_id
            or _text(locked_install.get("state")) != "active"
            or not install
            or not bool(install.get("enabled"))
            or _text(install.get("extension_id")) != scope["extension_id"]
            or not release_id
        ):
            self._deny()
        descriptor = _mapping(install.get("compiled_manifest") or install.get("manifest"))
        providers = compile_oauth_providers(descriptor)
        matches = [row for row in providers if row.provider_id == scope["provider_id"]]
        if len(matches) != 1:
            self._deny()
        provider = matches[0]
        configuration = _mapping(self._repository.get_configuration_for_scope_for_update(**scope))
        try:
            configuration_revision = int(configuration.get("configuration_revision") or 0)
        except (TypeError, ValueError):
            self._deny()
        state = {
            **scope,
            "extension_release_id": release_id,
            "configuration_revision": configuration_revision,
            "authorization_revision": configuration.get("authorization_revision"),
            "provider_security_fingerprint": provider_security_fingerprint(provider),
            "connection_type": connection_type,
            "existing_connection_id": connection_id,
        }
        if (
            (
                expected_configuration_revision is not None
                and configuration_revision != expected_configuration_revision
            )
            or _text(configuration.get("status"))
            not in {"verified", "ready_for_authorization", "needs_attention"}
            or not _configuration_lineage_matches(
                configuration,
                state,
                provider,
            )
        ):
            self._deny()
        connection = _mapping(
            self._repository.lock_connection(
                connection_id=connection_id,
                **scope,
                connection_type=connection_type,
            )
        )
        try:
            revision = int(connection.get("credential_revision") or 0)
        except (TypeError, ValueError):
            self._deny()
        if expected_credential_revision is not None and revision != expected_credential_revision:
            self._deny()
        oauth = _mapping(_mapping(connection.get("connection_config")).get("oauth"))
        lifecycle = _text(oauth.get("lifecycle_status"))
        if not bool(connection.get("enabled")):
            self._deny()
        if lifecycle == "authorization_required":
            self._deny("oauth_authorization_required")
        if (
            not _connection_matches(connection, state, expected_revision=revision)
            or int(connection.get("oauth_authorization_revision") or 0)
            != int(configuration.get("authorization_revision") or 0)
            or not secrets.compare_digest(
                _text(connection.get("oauth_provider_revocation_fingerprint")),
                provider_revocation_fingerprint(provider),
            )
        ):
            self._deny()
        configuration_status = _text(configuration.get("status"))
        post_connect_recovery = (
            recovery_purpose == "post_connect_retry"
            and bool(provider.post_connect_action_id)
            and (
                lifecycle == "post_connect_pending"
                or (
                    lifecycle == "needs_attention"
                    and _text(oauth.get("last_error_code"))
                    in {"internal_failure", "malformed_response", "provider_unavailable"}
                )
            )
        )
        invalid_client_recovery = (
            configuration_status in {"ready_for_authorization", "verified"}
            and lifecycle == "needs_attention"
            and _text(oauth.get("last_error_code")) == "invalid_client"
        )
        if configuration_status == "needs_attention" and lifecycle != "connected":
            self._deny("oauth_needs_attention")
        if lifecycle == "needs_attention" and not (
            invalid_client_recovery or post_connect_recovery
        ):
            self._deny("oauth_needs_attention")
        if lifecycle not in {"connected", "needs_attention"} and not post_connect_recovery:
            self._deny()
        required_scopes = _required_connection_scopes(
            descriptor,
            connection_type=connection_type,
            provider_id=scope["provider_id"],
        )
        raw_granted = connection.get("oauth_granted_scopes")
        granted = {
            _text(value)
            for value in (raw_granted if isinstance(raw_granted, list) else [])
            if isinstance(value, str) and _text(value)
        }
        if required_scopes is None or not required_scopes.issubset(granted):
            self._deny("oauth_scope_required")
        return descriptor, configuration, provider, connection, revision

    @contextmanager
    def _provider_token_claim(
        self,
        *,
        provider: OAuthProviderContract,
        configuration_id: str,
        connection: Mapping[str, Any],
    ) -> Iterator[None]:
        """Hold the shared token claim for the subject this connection is bound to.

        Only for providers that declare one token per configuration and subject.
        For them the claim is not optional: refreshing while a grant is in flight
        is exactly the collision it prevents, so a repository that cannot supply
        it makes the refresh fail rather than proceed unserialized.
        """
        if not provider.unique_subject_per_configuration:
            yield
            return
        subject = _text(
            _mapping(_mapping(connection.get("connection_config")).get("oauth")).get("subject")
        )
        claim = getattr(self._repository, "oauth_provider_token_claim", None)
        if claim is None or not subject or not configuration_id:
            self._deny("oauth_runtime_blocked")
        with claim(configuration_id=configuration_id, subject=subject):
            yield

    def _record_refresh_failure(
        self,
        *,
        scope: dict[str, str],
        connection_id: str,
        connection_type: str,
        configuration_revision: int,
        credential_revision: int,
        error_code: str,
        recovery_purpose: Literal["post_connect_retry"] | None = None,
    ) -> bool:
        try:
            with self._transaction_factory():
                _, _, _, connection, _ = self._validated_rows(
                    scope=scope,
                    connection_id=connection_id,
                    connection_type=connection_type,
                    expected_configuration_revision=configuration_revision,
                    expected_credential_revision=credential_revision,
                    recovery_purpose=recovery_purpose,
                )
                config = _mapping(connection.get("connection_config"))
                oauth = _mapping(config.get("oauth"))
                oauth.update(
                    lifecycle_status=(
                        "needs_attention"
                        if error_code == "invalid_client"
                        else "authorization_required"
                    ),
                    last_error_code=error_code,
                )
                saved = self._repository.update_oauth_refresh_failure(
                    **scope,
                    connection_id=connection_id,
                    connection_type=connection_type,
                    expected_credential_revision=credential_revision,
                    expected_configuration_revision=configuration_revision,
                    mark_configuration=error_code == "invalid_client",
                    error_code=error_code,
                    connection_config={**config, "oauth": oauth},
                )
                if not saved:
                    raise ValueError("stale refresh failure")
                return True
        except Exception:
            return False

    def _resolve_runtime_access_token(
        self,
        *,
        tenant_id: str,
        account_id: str,
        project_id: str,
        extension_install_id: str,
        extension_id: str,
        provider_id: str,
        configuration_id: str,
        connection_id: str,
        connection_type: str,
        refresh_claimed: bool,
        recovery_purpose: Literal["post_connect_retry"] | None,
    ) -> dict[str, str]:
        scope = {
            "tenant_id": tenant_id,
            "account_id": account_id,
            "project_id": project_id,
            "extension_install_id": extension_install_id,
            "extension_id": extension_id,
            "provider_id": provider_id,
            "configuration_id": configuration_id,
        }
        try:
            with self._transaction_factory():
                descriptor, configuration, provider, connection, revision = self._validated_rows(
                    scope=scope,
                    connection_id=connection_id,
                    connection_type=connection_type,
                    recovery_purpose=recovery_purpose,
                )
                owned_refs = _connection_owned_refs(connection_id, connection.get("secret_refs"))
                access_ref = (owned_refs or {}).get("_oauth_access_token")
                if owned_refs is None or not access_ref:
                    self._deny()
                snapshot_reader = getattr(self._token_store, "resolve_token_snapshot", None)
                if callable(snapshot_reader):
                    token_snapshot = _mapping(
                        snapshot_reader(
                            account_id=account_id,
                            secret_refs={"refs": owned_refs},
                        )
                    )
                    access_token = _text(token_snapshot.get("access_token"))
                else:
                    token_snapshot = {}
                    access_token = self._token_store.resolve_access_token(
                        account_id=account_id,
                        secret_refs={"refs": {"_oauth_access_token": access_ref}},
                    )
                if not access_token:
                    self._deny()
        except OAuthRuntimeTokenError:
            raise
        except Exception as exc:
            raise OAuthRuntimeTokenError("oauth_runtime_blocked") from exc
        lifecycle = _text(
            _mapping(_mapping(connection.get("connection_config")).get("oauth")).get(
                "lifecycle_status"
            )
        )
        configuration_revalidation = (
            _text(configuration.get("status")) == "ready_for_authorization"
            and (lifecycle == "connected" or recovery_purpose == "post_connect_retry")
        ) or _text(
            _mapping(_mapping(connection.get("connection_config")).get("oauth")).get(
                "last_error_code"
            )
        ) == "invalid_client"
        expires_at = token_snapshot.get("expires_at")
        numeric_expiry = (
            float(expires_at)
            if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool)
            else None
        )
        configuration_status = _text(configuration.get("status"))
        if numeric_expiry is not None and not math.isfinite(numeric_expiry):
            self._deny(
                "oauth_needs_attention"
                if configuration_status == "needs_attention"
                else "oauth_runtime_blocked"
            )
        if configuration_status == "needs_attention":
            if numeric_expiry is None or numeric_expiry <= self._now() + self._refresh_skew_seconds:
                self._deny("oauth_needs_attention")
            return {"access_token": access_token}
        if not configuration_revalidation and numeric_expiry is None:
            return {"access_token": access_token}
        if (
            not configuration_revalidation
            and numeric_expiry is not None
            and numeric_expiry > self._now() + self._refresh_skew_seconds
        ):
            return {"access_token": access_token}
        if not refresh_claimed:
            try:
                # Two claims, always in this order. The provider-token claim is
                # outermost because a grant for the same subject takes it too: a
                # refresh and a new authorization each revoke what the other
                # produced, so they must not overlap. The per-connection refresh
                # claim inside it keeps its existing meaning. Grants never take
                # the inner one, so there is no cycle to deadlock on.
                with (
                    self._provider_token_claim(
                        provider=provider,
                        configuration_id=configuration_id,
                        connection=connection,
                    ),
                    self._repository.oauth_refresh_claim(
                        **scope,
                        connection_id=connection_id,
                        connection_type=connection_type,
                    ),
                ):
                    return self._resolve_runtime_access_token(
                        tenant_id=tenant_id,
                        account_id=account_id,
                        project_id=project_id,
                        extension_install_id=extension_install_id,
                        extension_id=extension_id,
                        provider_id=provider_id,
                        configuration_id=configuration_id,
                        connection_id=connection_id,
                        connection_type=connection_type,
                        refresh_claimed=True,
                        recovery_purpose=recovery_purpose,
                    )
            except OAuthRuntimeTokenError:
                raise
            except Exception as exc:
                raise OAuthRuntimeTokenError("oauth_provider_unavailable") from exc
        refresh_value = _text(token_snapshot.get("refresh_token"))
        refresh_ref = (owned_refs or {}).get("_oauth_refresh_token")
        if (
            not refresh_value
            or not refresh_ref
            or self._refresh_access_token is None
            or self._configuration_secret_value is None
        ):
            self._deny("oauth_authorization_required")
        non_secret = _mapping(configuration.get("non_secret_config"))
        client_id = _text(non_secret.get("client_id"))
        if not client_id:
            self._deny()
        config_refs = _mapping(_mapping(configuration.get("secret_refs")).get("refs"))
        client_secret = ""
        if provider.token_endpoint.client_auth_method != "none":
            expected_ref = configuration_secret_ref_key(configuration_id, "client_secret")
            if _text(config_refs.get("client_secret")) != expected_ref:
                self._deny()
            client_secret = _text(
                self._configuration_secret_value(account_id=account_id, ref_key=expected_ref)
            )
            if not client_secret:
                self._deny()
        # The token endpoint for a bound provider comes from the subject recorded
        # when the callback was verified — never from anything this invocation was
        # handed. A connection whose subject is gone cannot be refreshed against a
        # guess, so it asks for reauthorization instead.
        verified_subject = _text(
            _mapping(_mapping(connection.get("connection_config")).get("oauth")).get("subject")
        )
        token_endpoint = provider.token_endpoint.bind(verified_subject)
        if not token_endpoint.url:
            self._deny("oauth_authorization_required")
        try:
            response = self._refresh_access_token(
                endpoint=token_endpoint,
                refresh_token=refresh_value,
                client_id=client_id,
                client_secret=client_secret,
                implicit_token_type=provider.code_exchange.implicit_token_type,
                required_response_fields=provider.code_exchange.required_response_fields,
            )
        except OAuthProviderError as exc:
            code = exc.code.value
            if code in {"invalid_client", "invalid_grant", "authorization_required"}:
                committed = self._record_refresh_failure(
                    scope=scope,
                    connection_id=connection_id,
                    connection_type=connection_type,
                    configuration_revision=int(configuration.get("configuration_revision") or 0),
                    credential_revision=revision,
                    error_code=code,
                    recovery_purpose=recovery_purpose,
                )
                if code == "invalid_client":
                    self._deny("oauth_needs_attention" if committed else "oauth_runtime_blocked")
                self._deny("oauth_authorization_required" if committed else "oauth_runtime_blocked")
            # A nominally successful response this side could not read leaves the
            # stored pair untouched and is worth trying again: the grant may be
            # perfectly good and the answer merely unreadable this once.
            self._deny(
                "oauth_provider_unavailable"
                if code in {"provider_unavailable", "rate_limited", "malformed_response"}
                else "oauth_runtime_blocked"
            )
        except Exception as exc:
            raise OAuthRuntimeTokenError("oauth_provider_unavailable") from exc
        try:
            granted_fallback = [
                _text(value)
                for value in connection.get("oauth_granted_scopes", [])
                if isinstance(value, str) and _text(value)
            ]
            granted_scopes = _scopes(
                response.get("scope"), fallback=granted_fallback, provider=provider
            )
            required_scopes = _required_connection_scopes(
                descriptor,
                connection_type=connection_type,
                provider_id=provider_id,
            )
            if required_scopes is None or not required_scopes.issubset(granted_scopes):
                committed = self._record_refresh_failure(
                    scope=scope,
                    connection_id=connection_id,
                    connection_type=connection_type,
                    configuration_revision=int(configuration.get("configuration_revision") or 0),
                    credential_revision=revision,
                    error_code="authorization_required",
                    recovery_purpose=recovery_purpose,
                )
                self._deny("oauth_authorization_required" if committed else "oauth_runtime_blocked")
            response = dict(response)
            response["scope"] = " ".join(granted_scopes)
            with self._transaction_factory():
                _, _, _, current_connection, _ = self._validated_rows(
                    scope=scope,
                    connection_id=connection_id,
                    connection_type=connection_type,
                    expected_configuration_revision=int(
                        configuration.get("configuration_revision") or 0
                    ),
                    expected_credential_revision=revision,
                    recovery_purpose=recovery_purpose,
                )
                current_refs = _connection_owned_refs(
                    connection_id, current_connection.get("secret_refs")
                )
                if current_refs != owned_refs:
                    self._deny()
                current_snapshot = _mapping(
                    self._token_store.resolve_token_snapshot(
                        account_id=account_id,
                        secret_refs={"refs": current_refs},
                    )
                )
                if not secrets.compare_digest(
                    _text(current_snapshot.get("refresh_token")), refresh_value
                ):
                    self._deny()
                written_secret_refs = self._token_store.write_tokens(
                    account_id=account_id,
                    connection_id=connection_id,
                    token_response=response,
                    existing_refresh_token=refresh_value,
                )
                written_refs = _mapping(_mapping(written_secret_refs).get("refs"))
                merged_refs = dict(current_refs or {})
                if "expires_in" not in response:
                    merged_refs.pop("_oauth_expires_at", None)
                secret_refs = {"refs": {**merged_refs, **written_refs}}
                config = _mapping(current_connection.get("connection_config"))
                oauth = _mapping(config.get("oauth"))
                oauth.update(lifecycle_status="connected", last_error_code=None)
                saved = self._repository.update_oauth_refresh(
                    **scope,
                    connection_id=connection_id,
                    connection_type=connection_type,
                    expected_credential_revision=revision,
                    expected_configuration_revision=int(
                        configuration.get("configuration_revision") or 0
                    ),
                    expected_refresh_ref=refresh_ref,
                    oauth_granted_scopes=granted_scopes,
                    secret_refs=secret_refs,
                    connection_config={**config, "oauth": oauth},
                )
                if not saved:
                    raise ValueError("stale refresh")
        except OAuthRuntimeTokenError:
            raise
        except Exception as exc:
            raise OAuthRuntimeTokenError("oauth_runtime_blocked") from exc
        refreshed_access = _text(response.get("access_token"))
        if not refreshed_access:
            self._deny()
        return {"access_token": refreshed_access}

    def resolve_runtime_access_token(
        self,
        *,
        tenant_id: str,
        account_id: str,
        project_id: str,
        extension_install_id: str,
        extension_id: str,
        provider_id: str,
        configuration_id: str,
        connection_id: str,
        connection_type: str,
        recovery_purpose: Literal["post_connect_retry"] | None = None,
    ) -> dict[str, str]:
        return self._resolve_runtime_access_token(
            tenant_id=tenant_id,
            account_id=account_id,
            project_id=project_id,
            extension_install_id=extension_install_id,
            extension_id=extension_id,
            provider_id=provider_id,
            configuration_id=configuration_id,
            connection_id=connection_id,
            connection_type=connection_type,
            refresh_claimed=False,
            recovery_purpose=recovery_purpose,
        )


__all__ = ["OAuthRuntimeTokenError", "OAuthRuntimeTokenService"]
