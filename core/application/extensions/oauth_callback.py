"""Consume extension OAuth callbacks and commit grants atomically."""

from __future__ import annotations

import hashlib
import logging
import secrets
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from core.application.extensions.oauth_configuration_secrets import (
    configuration_secret_ref_key,
)
from core.application.extensions.oauth_contract import (
    OAuthProviderContract,
    oauth_error_help_target,
    provider_revocation_fingerprint,
    provider_security_fingerprint,
)
from core.application.extensions.oauth_post_connect import (
    oauth_post_connect_result_from_invocation,
)
from core.infrastructure.extension_runtime.oauth_transport import (
    OAuthErrorCode,
    OAuthProviderError,
)

from .oauth_callback_connection_checks import (
    _current_install_provider,
    _duplicate_subject_connection_id,
    _locked_install_provider,
    _reconnect_subject_mismatch,
)
from .oauth_callback_models import OAuthCallbackOutcome as OAuthCallbackOutcome
from .oauth_callback_models import OAuthCallbackRepository as OAuthCallbackRepository
from .oauth_callback_models import OAuthCallbackResult as OAuthCallbackResult
from .oauth_callback_models import OAuthCallbackTokenStore as OAuthCallbackTokenStore
from .oauth_callback_models import OAuthGrantClaimUnavailable as OAuthGrantClaimUnavailable
from .oauth_callback_policy import (
    _MESSAGE_STALE_CONFIGURATION,
    _MESSAGE_STALE_CONNECTION,
    _MESSAGE_STALE_INSTALL,
    _VERIFIED_IDENTITY_FIELDS,
    _callback_signature_ok,
    _callback_url_matches,
    _configuration_matches,
    _connection_matches,
    _connection_owned_refs,
    _mapping,
    _operational_state,
    _post_connect_outcome,
    _provider,
    _provider_error_outcome,
    _repository_scope,
    _required_connection_scopes,
    _return_page_id,
    _scopes,
    _state_origin,
    _text,
)
from .oauth_callback_post_connect_runtime import _apply_post_connect, _dispatch_post_connect

#: Error codes that mean the provider answered and refused *the exchange*. Nothing
#: was granted, so a connection being reconnected is genuinely untouched.
#:
#: These only carry that meaning while the exchange is still what failed. Once it
#: has succeeded a new token set exists, and the same code raised for a different
#: reason — too few scopes granted, say — says nothing about the old credentials:
#: the provider may already have revoked them. That is why certainty is decided by
#: the phase first and the code second.
#:
#: Every other failure happens after the request was dispatched — a timeout, an
#: unreadable answer, an unexpected fault — and the provider may already have
#: accepted the grant and revoked what the old connection was using. The old
#: access token can keep working until it expires, which turns the lie into a
#: delayed failure rather than an immediate one. So preservation is not claimed.
_DEFINITE_REFUSAL_CODES = frozenset(
    {
        OAuthErrorCode.AUTHORIZATION_DENIED,
        OAuthErrorCode.AUTHORIZATION_REQUIRED,
        OAuthErrorCode.INVALID_CLIENT,
        OAuthErrorCode.INVALID_GRANT,
        OAuthErrorCode.RATE_LIMITED,
    }
)


_logger = logging.getLogger(__name__)


class OAuthCallbackService:
    def __init__(
        self,
        *,
        repository: OAuthCallbackRepository,
        token_store: OAuthCallbackTokenStore,
        install_loader: Callable[[str, str], Mapping[str, Any] | None],
        configuration_secret_value: Callable[..., str],
        decrypt_verifier: Callable[[str], str],
        exchange_code: Callable[..., dict[str, Any]],
        transaction_factory: Callable[[], AbstractContextManager[Any]],
        public_base_url: str,
        invoke_post_connect: Callable[..., Mapping[str, Any]] | None = None,
        resolve_runtime_access_token: Callable[..., Mapping[str, Any]] | None = None,
        connection_id_factory: Callable[[], str] = lambda: f"conn_{secrets.token_hex(8)}",
    ) -> None:
        self._repository = repository
        self._token_store = token_store
        self._install_loader = install_loader
        self._configuration_secret_value = configuration_secret_value
        self._decrypt_verifier = decrypt_verifier
        self._exchange_code = exchange_code
        self._transaction_factory = transaction_factory
        self._public_base_url = _text(public_base_url)
        self._invoke_post_connect = invoke_post_connect
        self._resolve_runtime_access_token = resolve_runtime_access_token
        self._connection_id_factory = connection_id_factory

    def _exchange_error_result(
        self,
        *,
        state: dict[str, Any],
        provider: OAuthProviderContract,
        code: OAuthErrorCode,
        return_page_id: str | None = None,
        granted: bool = False,
    ) -> OAuthCallbackResult:
        """A failed reconnect, told apart by whether a grant was actually issued.

        ``granted`` says the exchange completed and a new token set exists. From
        that point nothing about the previous authorization is knowable from here,
        whatever the code that follows happens to be.
        """
        preserved_connection_id = self._validated_preserved_connection_id(state)
        outcome: OAuthCallbackOutcome
        if code is OAuthErrorCode.INVALID_CLIENT:
            outcome = "needs_attention"
        elif code in {
            OAuthErrorCode.INVALID_GRANT,
            OAuthErrorCode.AUTHORIZATION_REQUIRED,
        }:
            outcome = "authorization_required"
        elif code is OAuthErrorCode.AUTHORIZATION_DENIED:
            outcome = "authorization_denied"
        else:
            outcome = "provider_unavailable"
        if code is OAuthErrorCode.INVALID_CLIENT:
            try:
                with self._transaction_factory():
                    current = _locked_install_provider(
                        state, repository=self._repository, install_loader=self._install_loader
                    )
                    if current is None:
                        raise ValueError(_MESSAGE_STALE_INSTALL)
                    _, locked_provider = current
                    scope = _repository_scope(state)
                    configuration = _mapping(
                        self._repository.get_configuration_for_scope_for_update(**scope)
                    )
                    if not _configuration_matches(configuration, state, locked_provider):
                        raise ValueError(_MESSAGE_STALE_CONFIGURATION)
                    updated = self._repository.mark_configuration_invalid_client(
                        **scope,
                        expected_configuration_revision=int(
                            state.get("configuration_revision") or 0
                        ),
                    )
                    if updated is None:
                        raise ValueError(_MESSAGE_STALE_CONFIGURATION)
            except Exception:
                return OAuthCallbackResult(
                    outcome="restart_required", return_page_id=return_page_id
                )
        definite = code in _DEFINITE_REFUSAL_CODES and not granted
        if preserved_connection_id and not definite:
            # The grant may have landed. The connection still exists and is still
            # named, but whether its credentials survived is not knowable from
            # here, so it is flagged for attention instead of called intact.
            outcome = "needs_attention"
        return OAuthCallbackResult(
            outcome=outcome,
            connection_id=preserved_connection_id,
            connection_preserved=True if preserved_connection_id and definite else None,
            help_target=oauth_error_help_target(provider, code.value),
            return_page_id=return_page_id,
        )

    def _validated_preserved_connection_id(self, state: dict[str, Any]) -> str | None:
        """Return a reconnect target only after repeating every scoped lock check."""
        connection_id = _text(state.get("existing_connection_id"))
        intent_payload = _mapping(state.get("intent_payload"))
        try:
            expected_revision = int(intent_payload.get("connection_credential_revision") or 0)
        except (TypeError, ValueError):
            return None
        if not connection_id or expected_revision <= 0:
            return None
        try:
            with self._transaction_factory():
                current = _locked_install_provider(
                    state, repository=self._repository, install_loader=self._install_loader
                )
                if current is None:
                    raise ValueError(_MESSAGE_STALE_INSTALL)
                _, provider = current
                scope = _repository_scope(state)
                configuration = _mapping(
                    self._repository.get_configuration_for_scope_for_update(**scope)
                )
                if not _configuration_matches(configuration, state, provider):
                    raise ValueError(_MESSAGE_STALE_CONFIGURATION)
                connection = _mapping(
                    self._repository.lock_connection(
                        connection_id=connection_id,
                        **scope,
                        connection_type=_text(state.get("connection_type")),
                    )
                )
                if not _connection_matches(
                    connection,
                    state,
                    expected_revision=expected_revision,
                ):
                    raise ValueError(_MESSAGE_STALE_CONNECTION)
        except Exception:
            return None
        return connection_id

    def retry_post_connect(
        self,
        *,
        tenant_id: str,
        account_id: str,
        project_id: str,
        extension_install_id: str,
        extension_id: str,
        provider_id: str,
        configuration_id: Any,
        connection_id: str,
        connection_type: str,
    ) -> OAuthCallbackResult:
        if self._invoke_post_connect is None:
            return OAuthCallbackResult(outcome="restart_required")
        try:
            with self._transaction_factory():
                install = _mapping(self._install_loader(extension_install_id, account_id))
                if (
                    not install
                    or not bool(install.get("enabled"))
                    or _text(install.get("extension_id")) != extension_id
                ):
                    raise ValueError(_MESSAGE_STALE_INSTALL)
                descriptor = _mapping(install.get("compiled_manifest") or install.get("manifest"))
                provider = _provider(descriptor, provider_id)
                if provider is None or not provider.post_connect_action_id:
                    raise ValueError("post-connect unavailable")
                scope = {
                    "tenant_id": tenant_id,
                    "account_id": account_id,
                    "project_id": project_id,
                    "extension_install_id": extension_install_id,
                    "extension_id": extension_id,
                    "provider_id": provider_id,
                    "configuration_id": configuration_id,
                }
                locked = _mapping(self._repository.lock_oauth_install_for_update(**scope))
                if (
                    _text(locked.get("install_id")) != extension_install_id
                    or _text(locked.get("account_id")) != account_id
                    or _text(locked.get("extension_id")) != extension_id
                    or _text(locked.get("release_id")) != _text(install.get("release_id"))
                    or _text(locked.get("state")) != "active"
                ):
                    raise ValueError(_MESSAGE_STALE_INSTALL)
                configuration = _mapping(
                    self._repository.get_configuration_for_scope_for_update(**scope)
                )
                state = {
                    **scope,
                    "extension_release_id": _text(install.get("release_id")),
                    "configuration_revision": configuration.get("configuration_revision"),
                    "authorization_revision": configuration.get("authorization_revision"),
                    "provider_security_fingerprint": provider_security_fingerprint(provider),
                    "callback_id": provider.callback_id,
                    "connection_type": connection_type,
                    "existing_connection_id": connection_id,
                }
                if not _configuration_matches(configuration, state, provider):
                    raise ValueError(_MESSAGE_STALE_CONFIGURATION)
                connection = _mapping(
                    self._repository.lock_connection(
                        connection_id=connection_id,
                        **scope,
                        connection_type=connection_type,
                    )
                )
                expected_revision = int(connection.get("credential_revision") or 0)
                if not _connection_matches(connection, state, expected_revision=expected_revision):
                    raise ValueError(_MESSAGE_STALE_CONNECTION)
                oauth = _mapping(_mapping(connection.get("connection_config")).get("oauth"))
                lifecycle = _text(oauth.get("lifecycle_status"))
                post_connect_failure = lifecycle == "needs_attention" and _text(
                    oauth.get("last_error_code")
                ) in {"internal_failure", "malformed_response", "provider_unavailable"}
                if lifecycle != "post_connect_pending" and not post_connect_failure:
                    raise ValueError("retry not available")
                if int(connection.get("oauth_authorization_revision") or 0) != int(
                    configuration.get("authorization_revision") or 0
                ):
                    raise ValueError("stale authorization")
                if _text(connection.get("oauth_provider_revocation_fingerprint")) != (
                    provider_revocation_fingerprint(provider)
                ):
                    raise ValueError("stale provider")
                required_scopes = _required_connection_scopes(
                    descriptor,
                    connection_type=connection_type,
                    provider_id=provider_id,
                )
                raw_granted = connection.get("oauth_granted_scopes")
                granted_scopes = {
                    _text(item)
                    for item in (raw_granted if isinstance(raw_granted, list) else [])
                    if isinstance(item, str) and _text(item)
                }
                if required_scopes is None or not required_scopes.issubset(granted_scopes):
                    raise ValueError("insufficient scope")
        except Exception:
            return OAuthCallbackResult(outcome="restart_required")
        if self._resolve_runtime_access_token is None:
            return OAuthCallbackResult(outcome="restart_required")
        try:
            resolved_token = _mapping(
                self._resolve_runtime_access_token(
                    tenant_id=tenant_id,
                    account_id=account_id,
                    project_id=project_id,
                    extension_install_id=extension_install_id,
                    extension_id=extension_id,
                    provider_id=provider_id,
                    configuration_id=configuration_id,
                    connection_id=connection_id,
                    connection_type=connection_type,
                    recovery_purpose="post_connect_retry",
                )
            )
            access_token = _text(resolved_token.get("access_token"))
            if not access_token:
                raise ValueError("access token unavailable")
        except Exception as exc:
            code = _text(getattr(exc, "code", ""))
            if code == "oauth_authorization_required":
                return OAuthCallbackResult(
                    outcome="authorization_required",
                    connection_id=connection_id,
                    help_target=oauth_error_help_target(provider, "authorization_required"),
                )
            if code == "oauth_needs_attention":
                return OAuthCallbackResult(
                    outcome="needs_attention",
                    connection_id=connection_id,
                    help_target=oauth_error_help_target(provider, "invalid_client"),
                )
            if code == "oauth_provider_unavailable":
                return OAuthCallbackResult(
                    outcome="needs_attention",
                    connection_id=connection_id,
                    help_target=oauth_error_help_target(provider, "provider_unavailable"),
                )
            return OAuthCallbackResult(outcome="restart_required")
        dispatched = _dispatch_post_connect(
            state=state,
            connection_id=connection_id,
            access_token=access_token,
            refresh_state=True,
            repository=self._repository,
            transaction_factory=self._transaction_factory,
            install_loader=self._install_loader,
            invoke_post_connect=self._invoke_post_connect,
        )
        if dispatched is None:
            return OAuthCallbackResult(outcome="restart_required")
        invocation, expected_revision, state = dispatched
        result = oauth_post_connect_result_from_invocation(invocation)
        if not _apply_post_connect(
            state=state,
            connection_id=connection_id,
            expected_revision=expected_revision,
            result=result,
            repository=self._repository,
            transaction_factory=self._transaction_factory,
            install_loader=self._install_loader,
        ):
            return OAuthCallbackResult(outcome="restart_required")
        return OAuthCallbackResult(
            outcome=_post_connect_outcome(result), connection_id=connection_id
        )

    def _pre_exchange(
        self, state: dict[str, Any]
    ) -> tuple[dict[str, Any], OAuthProviderContract, dict[str, Any], str, str, str] | None:
        current = _current_install_provider(state, install_loader=self._install_loader)
        if current is None:
            return None
        install, provider = current
        intent_id = _text(state.get("intent_id"))
        if intent_id not in provider.intents:
            return None
        descriptor = _mapping(install.get("compiled_manifest") or install.get("manifest"))
        configuration = self._repository.get_configuration_for_scope(**_repository_scope(state))
        configuration = _mapping(configuration)
        if not _configuration_matches(configuration, state, provider):
            return None
        non_secret = _mapping(configuration.get("non_secret_config"))
        client_id = _text(non_secret.get("client_id"))
        if not client_id:
            return None
        secret_refs = _mapping(_mapping(configuration.get("secret_refs")).get("refs"))
        client_secret = ""
        if provider.token_endpoint.client_auth_method != "none":
            expected_ref = configuration_secret_ref_key(
                _text(state.get("configuration_id")), "client_secret"
            )
            if _text(secret_refs.get("client_secret")) != expected_ref:
                return None
            client_secret = _text(
                self._configuration_secret_value(
                    account_id=_text(state.get("account_id")), ref_key=expected_ref
                )
            )
            if not client_secret:
                return None
        # A provider without PKCE stored no verifier, so requiring one here would
        # reject every one of its callbacks.
        verifier = ""
        if provider.uses_pkce:
            try:
                verifier = self._decrypt_verifier(_text(state.get("pkce_verifier_encrypted")))
            except Exception:
                return None
            if not verifier:
                return None
        return descriptor, provider, configuration, client_id, client_secret, verifier

    def handle_authorization_callback(
        self,
        *,
        extension_id: str,
        callback_id: str,
        registration: Mapping[str, Any],
        callback_path: str,
        opaque_state: str,
        code: str | None,
        provider_error: str | None,
        query_pairs: tuple[tuple[str, str], ...] = (),
    ) -> OAuthCallbackResult:
        normalized_state = _text(opaque_state)
        if not normalized_state or len(normalized_state) > 256:
            return OAuthCallbackResult(outcome="restart_required")
        consume_scope = {
            "state_hash": hashlib.sha256(normalized_state.encode("utf-8")).hexdigest(),
            "account_id": _text(registration.get("account_id")),
            "project_id": _text(registration.get("project_id")),
            "extension_id": _text(extension_id),
            "callback_id": _text(callback_id),
        }
        if not all(consume_scope.values()):
            return OAuthCallbackResult(outcome="restart_required")
        # Read the state without claiming it. Everything that can reject this
        # callback is checked first, so a forged signature or a mismatched subject
        # cannot burn the state a legitimate callback is still going to need.
        try:
            with self._transaction_factory():
                state = self._repository.load_state_for_callback(**consume_scope)
        except Exception:
            return OAuthCallbackResult(outcome="restart_required")
        state = _mapping(state)
        if not state:
            return OAuthCallbackResult(outcome="restart_required")

        try:
            with self._transaction_factory():
                prepared = self._pre_exchange(state)
        except Exception:
            prepared = None
        if prepared is None:
            return OAuthCallbackResult(outcome="restart_required")

        # A provider that signs its callbacks is verified before anything is
        # spent, and an error callback is verified exactly like a successful one:
        # an unsigned "the merchant said no" is not evidence of anything.
        if not _callback_signature_ok(
            provider=prepared[1],
            client_secret=prepared[4],
            state=state,
            query_pairs=query_pairs,
        ):
            return OAuthCallbackResult(outcome="restart_required")

        # Everything from here on is one grant: the duplicate check, the exchange
        # and the write that follows it. A provider that keeps one token per
        # configuration and subject cannot have two of these interleaved, or both
        # callbacks pass the duplicate check, both exchange, and one of the two
        # tokens is retired behind the connection still holding it.
        subject = _state_origin(state)
        if _reconnect_subject_mismatch(
            provider=prepared[1],
            state=state,
            subject=subject,
            repository=self._repository,
            transaction_factory=self._transaction_factory,
        ):
            return OAuthCallbackResult(
                outcome="needs_attention",
                connection_id=_text(state.get("existing_connection_id")) or None,
                connection_preserved=True,
                return_page_id=_return_page_id(state, prepared[0]),
            )
        try:
            with self._grant_claim(
                provider=prepared[1],
                configuration_id=_text(state.get("configuration_id")),
                subject=subject,
            ):
                return self._complete_authorization(
                    consume_scope=consume_scope,
                    state=state,
                    prepared=prepared,
                    callback_path=callback_path,
                    provider_error=provider_error,
                    code=code,
                    subject=subject,
                )
        except (TimeoutError, OAuthGrantClaimUnavailable):
            # Either another token operation for this subject is in flight, or the
            # lock that would make this safe is unavailable. Nothing was spent
            # either way, so the flow is restarted rather than risked.
            return OAuthCallbackResult(
                outcome="restart_required",
                return_page_id=_return_page_id(state, prepared[0]),
            )

    @contextmanager
    def _grant_claim(
        self, *, provider: OAuthProviderContract, configuration_id: str, subject: str
    ) -> Iterator[None]:
        """Serialize this grant against every other token operation for the subject.

        Providers that do not declare the uniqueness rule take no lock, exactly as
        before. Those that do cannot proceed without one: running unserialized is
        the failure the lock exists to prevent, so a repository that cannot supply
        it fails closed rather than quietly doing the dangerous thing.
        """
        if not provider.unique_subject_per_configuration:
            yield
            return
        claim = getattr(self._repository, "oauth_provider_token_claim", None)
        if claim is None or not subject or not configuration_id:
            raise OAuthGrantClaimUnavailable(
                "a serialized provider token claim is required for this provider"
            )
        with claim(configuration_id=configuration_id, subject=subject):
            yield

    def _complete_authorization(
        self,
        *,
        consume_scope: dict[str, str],
        state: dict[str, Any],
        prepared: tuple[Any, ...],
        callback_path: str,
        provider_error: str | None,
        code: str | None,
        subject: str,
    ) -> OAuthCallbackResult:
        """The part of a callback that must not interleave with another grant."""
        # A second grant for a subject this configuration already holds would be
        # worse than useless: a provider that keeps one token per application and
        # subject retires the first as soon as the second is issued, silently
        # breaking the connection that was already working. The code is therefore
        # never exchanged — it expires unused, and the operator is pointed at the
        # connection they already have.
        # Both checks run again here, under the claim. The first pass answered
        # before the lock existed, so between then and now another callback could
        # have created the connection or repointed it.
        if _reconnect_subject_mismatch(
            provider=prepared[1],
            state=state,
            subject=subject,
            repository=self._repository,
            transaction_factory=self._transaction_factory,
        ):
            return OAuthCallbackResult(
                outcome="needs_attention",
                connection_id=_text(state.get("existing_connection_id")) or None,
                connection_preserved=True,
                return_page_id=_return_page_id(state, prepared[0]),
            )
        duplicate_connection_id = _duplicate_subject_connection_id(
            provider=prepared[1],
            state=state,
            subject=subject,
            repository=self._repository,
            transaction_factory=self._transaction_factory,
        )
        if duplicate_connection_id:
            existing = duplicate_connection_id if duplicate_connection_id != "unknown" else None
            return OAuthCallbackResult(
                outcome="needs_attention",
                connection_id=existing,
                connection_preserved=True if existing else None,
                return_page_id=_return_page_id(state, prepared[0]),
            )

        # Only now is the state spent, and spending it is the atomic step that
        # decides which of two concurrent valid callbacks proceeds.
        try:
            with self._transaction_factory():
                claimed = self._repository.consume_state_for_callback(**consume_scope)
        except Exception:
            return OAuthCallbackResult(outcome="restart_required")
        if not _mapping(claimed):
            return OAuthCallbackResult(outcome="restart_required")

        # Deliberately after the consume. A callback that names a live state but
        # arrives at the wrong URL burns it, which is the existing behaviour and
        # is not what the signature check is protecting against.
        if not _callback_url_matches(
            state.get("redirect_uri"),
            public_base_url=self._public_base_url,
            callback_path=callback_path,
        ):
            return OAuthCallbackResult(outcome="restart_required")

        if provider_error is not None:
            error_outcome = _provider_error_outcome(provider_error)
            try:
                with self._transaction_factory():
                    current = _current_install_provider(state, install_loader=self._install_loader)
            except Exception:
                current = None
            descriptor = (
                _mapping(current[0].get("compiled_manifest") or current[0].get("manifest"))
                if current is not None
                else {}
            )
            return_page_id = _return_page_id(state, descriptor)
            help_target = (
                oauth_error_help_target(current[1], error_outcome) if current is not None else None
            )
            preserved_connection_id = self._validated_preserved_connection_id(state)
            return OAuthCallbackResult(
                outcome=error_outcome,
                connection_id=preserved_connection_id,
                connection_preserved=True if preserved_connection_id else None,
                help_target=help_target,
                return_page_id=return_page_id,
            )
        normalized_code = _text(code)
        if not normalized_code:
            return OAuthCallbackResult(outcome="restart_required")

        descriptor, provider, _, client_id, client_secret, verifier = prepared
        return_page_id = _return_page_id(state, descriptor)
        try:
            token_response = self._exchange_code(
                endpoint=provider.token_endpoint.bind(_state_origin(state)),
                code=normalized_code,
                redirect_uri=_text(state.get("redirect_uri")),
                code_verifier=verifier if provider.uses_pkce else "",
                client_id=client_id,
                client_secret=client_secret,
                include_grant_type=provider.code_exchange.include_grant_type,
                include_redirect_uri=provider.code_exchange.include_redirect_uri,
                static_parameters=provider.code_exchange.static_parameters,
                implicit_token_type=provider.code_exchange.implicit_token_type,
                required_response_fields=provider.code_exchange.required_response_fields,
            )
            requested_scopes = [
                _text(value) for value in state.get("requested_scopes", []) if _text(value)
            ]
            granted_scopes = _scopes(
                token_response.get("scope"), fallback=requested_scopes, provider=provider
            )
        except OAuthProviderError as exc:
            # The closed code only: no provider message, no response body, no query.
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- `code` here is a closed OAuthProviderError enum value, not an authorization code or token.
            _logger.warning("OAuth token exchange failed: code=%s", exc.code.value)
            return self._exchange_error_result(
                state=state,
                provider=provider,
                code=exc.code,
                return_page_id=return_page_id,
            )
        except Exception:
            # The exchange was dispatched and this side cannot say what came of
            # it, so nothing is promised about the connection it would replace.
            preserved_connection_id = self._validated_preserved_connection_id(state)
            return OAuthCallbackResult(
                outcome="needs_attention" if preserved_connection_id else "provider_unavailable",
                connection_id=preserved_connection_id,
                return_page_id=return_page_id,
            )

        required_scopes = _required_connection_scopes(
            descriptor,
            connection_type=_text(state.get("connection_type")),
            provider_id=_text(state.get("provider_id")),
        )
        if required_scopes is None or not required_scopes.issubset(granted_scopes):
            # Scope names are neither secret nor provider copy, and without them an
            # operator sees only "authorization required" with no way to tell that
            # the released app version asked for less than the connection needs.
            _logger.warning(
                "OAuth grant is short of the scopes this connection type requires: "
                "missing=%s granted_count=%d connection_type=%s",
                sorted((required_scopes or set()) - set(granted_scopes)),
                len(granted_scopes),
                _text(state.get("connection_type")),
            )
            # The exchange succeeded and a token set was issued; it simply does not
            # carry everything this connection type needs. The provider may have
            # retired the previous grant to issue this one, so the old connection
            # is not reported as preserved even though the refusal is definite.
            return self._exchange_error_result(
                state=state,
                provider=provider,
                code=OAuthErrorCode.AUTHORIZATION_REQUIRED,
                return_page_id=return_page_id,
                granted=True,
            )

        existing_connection_id = _text(state.get("existing_connection_id"))
        connection_id = existing_connection_id or _text(self._connection_id_factory())
        if not connection_id:
            return OAuthCallbackResult(outcome="restart_required", return_page_id=return_page_id)
        try:
            with self._transaction_factory():
                current = _locked_install_provider(
                    state, repository=self._repository, install_loader=self._install_loader
                )
                if current is None:
                    raise ValueError(_MESSAGE_STALE_INSTALL)
                _, locked_provider = current
                scope = _repository_scope(state)
                configuration = _mapping(
                    self._repository.get_configuration_for_scope_for_update(**scope)
                )
                if not _configuration_matches(configuration, state, locked_provider):
                    raise ValueError(_MESSAGE_STALE_CONFIGURATION)

                connection: dict[str, Any] = {}
                expected_connection_revision = 0
                existing_refresh_token = ""
                existing_refs: dict[str, str] = {}
                if existing_connection_id:
                    intent_payload = _mapping(state.get("intent_payload"))
                    expected_connection_revision = int(
                        intent_payload.get("connection_credential_revision") or 0
                    )
                    connection = _mapping(
                        self._repository.lock_connection(
                            connection_id=existing_connection_id,
                            **scope,
                            connection_type=_text(state.get("connection_type")),
                        )
                    )
                    if not _connection_matches(
                        connection,
                        state,
                        expected_revision=expected_connection_revision,
                    ):
                        raise ValueError(_MESSAGE_STALE_CONNECTION)
                    validated_refs = _connection_owned_refs(
                        existing_connection_id,
                        connection.get("secret_refs"),
                    )
                    if validated_refs is None:
                        raise ValueError("invalid connection secret refs")
                    existing_refs = validated_refs
                    refresh_ref = existing_refs.get("_oauth_refresh_token")
                    refresh_refs = (
                        {"refs": {"_oauth_refresh_token": refresh_ref}} if refresh_ref else {}
                    )
                    existing_refresh_token = self._token_store.resolve_refresh_token(
                        account_id=_text(state.get("account_id")),
                        secret_refs=refresh_refs,
                    )

                normalized_token_response = dict(token_response)
                normalized_token_response["scope"] = " ".join(granted_scopes)
                secret_refs = self._token_store.write_tokens(
                    account_id=_text(state.get("account_id")),
                    connection_id=connection_id,
                    token_response=normalized_token_response,
                    existing_refresh_token=existing_refresh_token,
                )
                if connection:
                    written_refs = _mapping(_mapping(secret_refs).get("refs"))
                    if "expires_in" not in normalized_token_response:
                        existing_refs.pop("_oauth_expires_at", None)
                    secret_refs = {"refs": {**existing_refs, **written_refs}}
                existing_config = _mapping(connection.get("connection_config"))
                existing_oauth = _mapping(existing_config.get("oauth"))
                pending_oauth = dict(existing_oauth)
                if self._invoke_post_connect is not None and locked_provider.post_connect_action_id:
                    for field in _VERIFIED_IDENTITY_FIELDS:
                        pending_oauth.pop(field, None)
                post_connect_pending = bool(
                    self._invoke_post_connect is not None and locked_provider.post_connect_action_id
                )
                oauth_config: dict[str, Any] = {
                    **pending_oauth,
                    "lifecycle_status": (
                        "post_connect_pending" if post_connect_pending else "connected"
                    ),
                    "token_type": "Bearer",
                    "authorization_intent_id": _text(state.get("intent_id")),
                }
                # A provider that signs its callback has already told us, provably,
                # which subject the grant belongs to. That is a better identity than
                # one asked for afterwards, so it is recorded here and no follow-up
                # request is made to discover it.
                verified_subject = _state_origin(state)
                if locked_provider.callback_verification is not None and verified_subject:
                    oauth_config["subject"] = verified_subject
                connection_config = {**existing_config, "oauth": oauth_config}
                connection_values = {
                    "connection_id": connection_id,
                    "tenant_id": _text(state.get("tenant_id")),
                    "account_id": _text(state.get("account_id")),
                    "project_id": _text(state.get("project_id")),
                    "extension_install_id": _text(state.get("extension_install_id")),
                    "extension_id": _text(state.get("extension_id")),
                    "display_name": _text(state.get("display_name")),
                    "connection_kind": "extension",
                    "connection_type": _text(state.get("connection_type")),
                    "oauth_configuration_id": _text(state.get("configuration_id")),
                    "oauth_provider_id": _text(state.get("provider_id")),
                    "oauth_extension_release_id": _text(state.get("extension_release_id")),
                    "oauth_authorization_revision": int(state.get("authorization_revision") or 0),
                    "oauth_requested_scopes": requested_scopes,
                    "oauth_granted_scopes": granted_scopes,
                    "oauth_provider_security_fingerprint": provider_security_fingerprint(
                        locked_provider
                    ),
                    "oauth_provider_revocation_fingerprint": provider_revocation_fingerprint(
                        locked_provider
                    ),
                    "secret_refs": secret_refs,
                    "connection_config": connection_config,
                    **_operational_state(ready=not post_connect_pending),
                }
                if existing_connection_id:
                    saved = self._repository.update_oauth_connection(
                        **connection_values,
                        expected_credential_revision=expected_connection_revision,
                    )
                else:
                    saved = self._repository.create_oauth_connection(**connection_values)
                if not saved or not self._repository.mark_configuration_verified(**scope):
                    raise ValueError("stale callback")
        except Exception:
            return OAuthCallbackResult(outcome="restart_required", return_page_id=return_page_id)
        post_connect_action = provider.post_connect_action_id
        if self._invoke_post_connect is not None and post_connect_action:
            dispatched = _dispatch_post_connect(
                state=state,
                connection_id=connection_id,
                access_token=_text(normalized_token_response.get("access_token")),
                repository=self._repository,
                transaction_factory=self._transaction_factory,
                install_loader=self._install_loader,
                invoke_post_connect=self._invoke_post_connect,
            )
            if dispatched is None:
                return OAuthCallbackResult(
                    outcome="restart_required",
                    connection_id=connection_id,
                    return_page_id=return_page_id,
                )
            invocation, committed_revision, state = dispatched
            post_connect_result = oauth_post_connect_result_from_invocation(invocation)
            if not _apply_post_connect(
                state=state,
                connection_id=connection_id,
                expected_revision=committed_revision,
                result=post_connect_result,
                repository=self._repository,
                transaction_factory=self._transaction_factory,
                install_loader=self._install_loader,
            ):
                return OAuthCallbackResult(
                    outcome="restart_required",
                    connection_id=connection_id,
                    return_page_id=return_page_id,
                )
            return OAuthCallbackResult(
                outcome=_post_connect_outcome(post_connect_result),
                connection_id=connection_id,
                return_page_id=return_page_id,
            )
        return OAuthCallbackResult(
            outcome="connected",
            connection_id=connection_id,
            return_page_id=return_page_id,
        )


__all__ = ["OAuthCallbackResult", "OAuthCallbackService"]
