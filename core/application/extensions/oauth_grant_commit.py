"""Commit one issued OAuth grant and run its post-connect action, for every flow.

The authorization-code callback and the refresh-token import (ADR 0058) differ only
in how the first token set is obtained. From the moment a provider has answered,
both go through this one path: the same locks, the same lineage checks, the same
token write, the same guarded connection create or update and the same
post-connect action. A second copy of this path would be a second set of rules.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any

from core.application.extensions.oauth_configuration_secrets import (
    configuration_secret_ref_key,
)
from core.application.extensions.oauth_contract import (
    OAuthProviderContract,
    provider_revocation_fingerprint,
    provider_security_fingerprint,
)
from core.application.extensions.oauth_post_connect import (
    oauth_post_connect_result_from_invocation,
)

from .oauth_callback_connection_checks import _locked_install_provider
from .oauth_callback_models import (
    OAuthCallbackRepository,
    OAuthCallbackResult,
    OAuthCallbackTokenStore,
)
from .oauth_callback_policy import (
    _MESSAGE_STALE_CONFIGURATION,
    _MESSAGE_STALE_CONNECTION,
    _MESSAGE_STALE_INSTALL,
    _REPLACED_SUBJECT,
    _VERIFIED_IDENTITY_FIELDS,
    _configuration_matches,
    _connection_matches,
    _connection_owned_refs,
    _mapping,
    _operational_state,
    _post_connect_outcome,
    _repository_scope,
    _state_origin,
    _text,
)
from .oauth_callback_post_connect_runtime import _apply_post_connect, _dispatch_post_connect


def oauth_client_credentials(
    configuration: Mapping[str, Any],
    *,
    provider: OAuthProviderContract,
    configuration_id: str,
    account_id: str,
    configuration_secret_value: Callable[..., str],
) -> tuple[str, str] | None:
    """The configuration's client ID and secret, or None when either is unusable.

    The secret is read only through the configuration's own deterministic ref, so
    a configuration cannot borrow another one's credentials.
    """
    client_id = _text(_mapping(configuration.get("non_secret_config")).get("client_id"))
    if not client_id:
        return None
    if provider.token_endpoint.client_auth_method == "none":
        return client_id, ""
    secret_refs = _mapping(_mapping(configuration.get("secret_refs")).get("refs"))
    expected_ref = configuration_secret_ref_key(configuration_id, "client_secret")
    if _text(secret_refs.get("client_secret")) != expected_ref:
        return None
    client_secret = _text(configuration_secret_value(account_id=account_id, ref_key=expected_ref))
    if not client_secret:
        return None
    return client_id, client_secret


@dataclass(frozen=True)
class OAuthGrant:
    """A token set a provider has just issued, ready to become a connection."""

    #: Carries the tokens; kept out of every repr so no log line or traceback
    #: built from a grant can show them.
    token_response: Mapping[str, Any] = field(repr=False)
    requested_scopes: list[str]
    granted_scopes: list[str]
    connection_id: str
    #: Stored when the response carries no refresh token. None keeps the refresh
    #: token the connection being replaced already holds.
    fallback_refresh_token: str | None = field(default=None, repr=False)
    #: New values for the settings named in `replaced_setting_keys`. None keeps
    #: every existing setting.
    connection_settings: Mapping[str, Any] | None = None
    #: The settings the connection type declares. Only these are replaced; a key
    #: the declaration does not name is left as it is.
    replaced_setting_keys: frozenset[str] = frozenset()
    #: Keep the verified identity while a replacement waits for post-connect.
    #: The redirect flow clears it, as it always has; an import keeps it.
    keep_identity_until_verified: bool = False


class OAuthGrantCommitter:
    def __init__(
        self,
        *,
        repository: OAuthCallbackRepository,
        token_store: OAuthCallbackTokenStore,
        install_loader: Callable[[str, str], Mapping[str, Any] | None],
        transaction_factory: Callable[[], AbstractContextManager[Any]],
        invoke_post_connect: Callable[..., Mapping[str, Any]] | None,
    ) -> None:
        self._repository = repository
        self._token_store = token_store
        self._install_loader = install_loader
        self._transaction_factory = transaction_factory
        self._invoke_post_connect = invoke_post_connect

    def complete(
        self,
        *,
        state: dict[str, Any],
        grant: OAuthGrant,
        return_page_id: str | None,
    ) -> OAuthCallbackResult:
        """Commit the grant, then run the post-connect action when one is declared."""
        try:
            access_token, post_connect_pending = self._commit(state=state, grant=grant)
        except Exception:
            return OAuthCallbackResult(outcome="restart_required", return_page_id=return_page_id)
        if not post_connect_pending:
            return OAuthCallbackResult(
                outcome="connected",
                connection_id=grant.connection_id,
                return_page_id=return_page_id,
            )
        return self._post_connect(
            state=state,
            connection_id=grant.connection_id,
            access_token=access_token,
            return_page_id=return_page_id,
        )

    def _locked_lineage(
        self, state: dict[str, Any]
    ) -> tuple[OAuthProviderContract, dict[str, str], dict[str, Any]]:
        """The provider, scope and configuration, once install and lineage check out."""
        current = _locked_install_provider(
            state, repository=self._repository, install_loader=self._install_loader
        )
        if current is None:
            raise ValueError(_MESSAGE_STALE_INSTALL)
        _, locked_provider = current
        scope = _repository_scope(state)
        configuration = _mapping(self._repository.get_configuration_for_scope_for_update(**scope))
        if not _configuration_matches(configuration, state, locked_provider):
            raise ValueError(_MESSAGE_STALE_CONFIGURATION)
        return locked_provider, scope, configuration

    def lineage_is_current(self, state: dict[str, Any]) -> bool:
        """Whether a commit of this state would pass its lineage checks right now.

        Run immediately before a provider call that may rotate a token, so a
        state that could never commit does not spend one. The commit repeats
        every check under its own lock; this only narrows the window.
        """
        try:
            with self._transaction_factory():
                self._locked_lineage(state)
                existing_connection_id = _text(state.get("existing_connection_id"))
                if existing_connection_id:
                    connection = _mapping(
                        self._repository.lock_connection(
                            connection_id=existing_connection_id,
                            **_repository_scope(state),
                            connection_type=_text(state.get("connection_type")),
                        )
                    )
                    expected = int(
                        _mapping(state.get("intent_payload")).get("connection_credential_revision")
                        or 0
                    )
                    if not _connection_matches(connection, state, expected_revision=expected):
                        raise ValueError(_MESSAGE_STALE_CONNECTION)
        except Exception:
            return False
        return True

    def mark_invalid_client(self, state: dict[str, Any]) -> bool:
        """Record that the provider refused this configuration's client credentials."""
        try:
            with self._transaction_factory():
                _, scope, _ = self._locked_lineage(state)
                updated = self._repository.mark_configuration_invalid_client(
                    **scope,
                    expected_configuration_revision=int(state.get("configuration_revision") or 0),
                )
                if updated is None:
                    raise ValueError(_MESSAGE_STALE_CONFIGURATION)
        except Exception:
            return False
        return True

    def _commit(self, *, state: dict[str, Any], grant: OAuthGrant) -> tuple[str, bool]:
        connection_id = grant.connection_id
        existing_connection_id = _text(state.get("existing_connection_id"))
        with self._transaction_factory():
            locked_provider, scope, _ = self._locked_lineage(state)

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
                if grant.fallback_refresh_token is None:
                    refresh_ref = existing_refs.get("_oauth_refresh_token")
                    refresh_refs = (
                        {"refs": {"_oauth_refresh_token": refresh_ref}} if refresh_ref else {}
                    )
                    existing_refresh_token = self._token_store.resolve_refresh_token(
                        account_id=_text(state.get("account_id")),
                        secret_refs=refresh_refs,
                    )
            if grant.fallback_refresh_token is not None:
                existing_refresh_token = grant.fallback_refresh_token

            normalized_token_response = dict(grant.token_response)
            normalized_token_response["scope"] = " ".join(grant.granted_scopes)
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
            post_connect_pending = bool(
                self._invoke_post_connect is not None and locked_provider.post_connect_action_id
            )
            # A replacement remembers the subject the connection is bound to, so
            # a post-connect result for a different account is refused rather
            # than adopted. A provider that signs its callback is already held
            # to its subject before the exchange, so it needs no reminder.
            replaced_subject = _text(existing_oauth.get("subject")) or _text(
                existing_oauth.get(_REPLACED_SUBJECT)
            )
            pending_oauth.pop(_REPLACED_SUBJECT, None)
            if post_connect_pending:
                if not grant.keep_identity_until_verified:
                    for identity_field in _VERIFIED_IDENTITY_FIELDS:
                        pending_oauth.pop(identity_field, None)
                if (
                    existing_connection_id
                    and replaced_subject
                    and locked_provider.callback_verification is None
                ):
                    pending_oauth[_REPLACED_SUBJECT] = replaced_subject
            oauth_config: dict[str, Any] = {
                **pending_oauth,
                "lifecycle_status": (
                    "post_connect_pending" if post_connect_pending else "connected"
                ),
                "token_type": "Bearer",
            }
            # An imported grant has no authorization intent; recording an empty
            # one would read as a declared intent named "".
            intent_id = _text(state.get("intent_id"))
            if intent_id:
                oauth_config["authorization_intent_id"] = intent_id
            # A provider that signs its callback has already told us, provably,
            # which subject the grant belongs to. That is a better identity than
            # one asked for afterwards, so it is recorded here and no follow-up
            # request is made to discover it.
            verified_subject = _state_origin(state)
            if locked_provider.callback_verification is not None and verified_subject:
                oauth_config["subject"] = verified_subject
            if grant.connection_settings is None:
                settings = existing_config
            else:
                settings = {
                    key: value
                    for key, value in existing_config.items()
                    if key != "oauth" and key not in grant.replaced_setting_keys
                }
                settings.update(grant.connection_settings)
            connection_config = {**settings, "oauth": oauth_config}
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
                "oauth_requested_scopes": grant.requested_scopes,
                "oauth_granted_scopes": grant.granted_scopes,
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
                raise ValueError("stale grant")
        return _text(normalized_token_response.get("access_token")), post_connect_pending

    def _post_connect(
        self,
        *,
        state: dict[str, Any],
        connection_id: str,
        access_token: str,
        return_page_id: str | None,
    ) -> OAuthCallbackResult:
        dispatched = _dispatch_post_connect(
            state=state,
            connection_id=connection_id,
            access_token=access_token,
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
        recorded = _apply_post_connect(
            state=state,
            connection_id=connection_id,
            expected_revision=committed_revision,
            result=oauth_post_connect_result_from_invocation(invocation),
            repository=self._repository,
            transaction_factory=self._transaction_factory,
            install_loader=self._install_loader,
        )
        if recorded is None:
            return OAuthCallbackResult(
                outcome="restart_required",
                connection_id=connection_id,
                return_page_id=return_page_id,
            )
        return OAuthCallbackResult(
            outcome=_post_connect_outcome(recorded),
            connection_id=connection_id,
            return_page_id=return_page_id,
        )


__all__ = ["OAuthGrant", "OAuthGrantCommitter", "oauth_client_credentials"]
