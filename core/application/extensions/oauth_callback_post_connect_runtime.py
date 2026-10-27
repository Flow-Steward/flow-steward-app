from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import Any

from core.application.extensions.oauth_contract import (
    provider_revocation_fingerprint,
    provider_security_fingerprint,
)
from core.application.extensions.oauth_post_connect import (
    OAuthPostConnectResult,
)
from core.application.extensions.secret_reflection import value_exposes_secret

from .oauth_callback_connection_checks import _locked_install_provider
from .oauth_callback_models import OAuthCallbackRepository
from .oauth_callback_policy import (
    _MESSAGE_STALE_CONFIGURATION,
    _MESSAGE_STALE_CONNECTION,
    _MESSAGE_STALE_INSTALL,
    _configuration_matches,
    _connection_matches,
    _mapping,
    _operational_state,
    _post_connect_config,
    _repository_scope,
    _required_connection_scopes,
    _text,
)


def _apply_post_connect(
    *,
    state: dict[str, Any],
    connection_id: str,
    expected_revision: int,
    result: OAuthPostConnectResult,
    repository: OAuthCallbackRepository,
    transaction_factory: Callable[[], AbstractContextManager[Any]],
    install_loader: Callable[[str, str], Mapping[str, Any] | None],
) -> bool:
    try:
        with transaction_factory():
            current = _locked_install_provider(
                state, repository=repository, install_loader=install_loader
            )
            if current is None:
                raise ValueError(_MESSAGE_STALE_INSTALL)
            _, provider = current
            scope = _repository_scope(state)
            configuration = _mapping(repository.get_configuration_for_scope_for_update(**scope))
            if not _configuration_matches(configuration, state, provider):
                raise ValueError(_MESSAGE_STALE_CONFIGURATION)
            connection = _mapping(
                repository.lock_connection(
                    connection_id=connection_id,
                    **scope,
                    connection_type=_text(state.get("connection_type")),
                )
            )
            connection_state = {**state, "existing_connection_id": connection_id}
            if not _connection_matches(
                connection,
                connection_state,
                expected_revision=expected_revision,
            ):
                raise ValueError(_MESSAGE_STALE_CONNECTION)
            connection_config, display_name = _post_connect_config(connection, result)
            saved = repository.update_oauth_post_connect(
                connection_id=connection_id,
                account_id=_text(state.get("account_id")),
                project_id=_text(state.get("project_id")),
                extension_install_id=_text(state.get("extension_install_id")),
                extension_id=_text(state.get("extension_id")),
                oauth_configuration_id=_text(state.get("configuration_id")),
                oauth_provider_id=_text(state.get("provider_id")),
                connection_type=_text(state.get("connection_type")),
                expected_credential_revision=expected_revision,
                display_name=display_name,
                connection_config=connection_config,
                **_operational_state(
                    ready=result.health == "ok",
                    error_code=result.error_code,
                ),
            )
            if not saved:
                raise ValueError("stale post-connect")
    except Exception:
        return False
    return True


def _dispatch_post_connect(
    *,
    state: dict[str, Any],
    connection_id: str,
    access_token: str,
    refresh_state: bool = False,
    repository: OAuthCallbackRepository,
    transaction_factory: Callable[[], AbstractContextManager[Any]],
    install_loader: Callable[[str, str], Mapping[str, Any] | None],
    invoke_post_connect: Callable[..., Mapping[str, Any]] | None,
) -> tuple[dict[str, Any], int, dict[str, Any]] | None:
    if invoke_post_connect is None or not access_token:
        return None
    try:
        with transaction_factory():
            current = _locked_install_provider(
                state, repository=repository, install_loader=install_loader
            )
            if current is None:
                raise ValueError(_MESSAGE_STALE_INSTALL)
            install, provider = current
            if not provider.post_connect_action_id:
                raise ValueError("post-connect unavailable")
            scope = _repository_scope(state)
            configuration = _mapping(repository.get_configuration_for_scope_for_update(**scope))
            effective_state = (
                {
                    **state,
                    "configuration_revision": configuration.get("configuration_revision"),
                    "authorization_revision": configuration.get("authorization_revision"),
                    "provider_security_fingerprint": provider_security_fingerprint(provider),
                    "callback_id": provider.callback_id,
                }
                if refresh_state
                else state
            )
            if not _configuration_matches(configuration, effective_state, provider):
                raise ValueError(_MESSAGE_STALE_CONFIGURATION)
            connection_state = {**effective_state, "existing_connection_id": connection_id}
            connection = _mapping(
                repository.lock_connection(
                    connection_id=connection_id,
                    **scope,
                    connection_type=_text(state.get("connection_type")),
                )
            )
            expected_revision = int(connection.get("credential_revision") or 0)
            if not _connection_matches(
                connection,
                connection_state,
                expected_revision=expected_revision,
            ):
                raise ValueError(_MESSAGE_STALE_CONNECTION)
            if int(connection.get("oauth_authorization_revision") or 0) != int(
                configuration.get("authorization_revision") or 0
            ):
                raise ValueError("stale authorization")
            if _text(connection.get("oauth_provider_revocation_fingerprint")) != (
                provider_revocation_fingerprint(provider)
            ):
                raise ValueError("stale provider")
            descriptor = _mapping(install.get("compiled_manifest") or install.get("manifest"))
            required_scopes = _required_connection_scopes(
                descriptor,
                connection_type=_text(state.get("connection_type")),
                provider_id=_text(state.get("provider_id")),
            )
            raw_granted = connection.get("oauth_granted_scopes")
            granted_scopes = {
                _text(item)
                for item in (raw_granted if isinstance(raw_granted, list) else [])
                if isinstance(item, str) and _text(item)
            }
            if required_scopes is None or not required_scopes.issubset(granted_scopes):
                raise ValueError("insufficient scope")
            try:
                invocation = invoke_post_connect(
                    extension=install,
                    action_id=provider.post_connect_action_id,
                    target={
                        "connection_id": connection_id,
                        "connection": {
                            "connection_id": connection_id,
                            "secrets": {"access_token": access_token},
                        },
                    },
                    context={
                        "account_id": _text(state.get("account_id")),
                        "project_id": _text(state.get("project_id")),
                        "connection_id": connection_id,
                        "connection_type": _text(state.get("connection_type")),
                        "granted_scopes": sorted(granted_scopes),
                    },
                )
            except Exception:
                invocation = {}
            invocation = _mapping(invocation)
            if value_exposes_secret(invocation, frozenset({access_token})):
                invocation = {
                    "status": "failed",
                    "response": {
                        "ok": False,
                        "error_code": "extension_secret_exposure_blocked",
                    },
                }
            return invocation, expected_revision, effective_state
    except Exception:
        return None
