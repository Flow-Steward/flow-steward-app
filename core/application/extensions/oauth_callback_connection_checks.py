from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import Any

from core.application.extensions.oauth_contract import (
    OAuthProviderContract,
    provider_security_fingerprint,
)

from .oauth_callback_models import OAuthCallbackRepository
from .oauth_callback_policy import _mapping, _provider, _repository_scope, _text


def _current_install_provider(
    state: dict[str, Any], *, install_loader: Callable[[str, str], Mapping[str, Any] | None]
) -> tuple[dict[str, Any], OAuthProviderContract] | None:
    raw = install_loader(
        _text(state.get("extension_install_id")),
        _text(state.get("account_id")),
    )
    install = _mapping(raw)
    if (
        not install
        or not bool(install.get("enabled"))
        or _text(install.get("extension_id")) != _text(state.get("extension_id"))
        or _text(install.get("release_id")) != _text(state.get("extension_release_id"))
    ):
        return None
    descriptor = _mapping(install.get("compiled_manifest") or install.get("manifest"))
    provider = _provider(descriptor, _text(state.get("provider_id")))
    if provider is None:
        return None
    if provider.callback_id != _text(state.get("callback_id")):
        return None
    if provider_security_fingerprint(provider) != _text(state.get("provider_security_fingerprint")):
        return None
    return install, provider


def _locked_install_provider(
    state: dict[str, Any],
    *,
    repository: OAuthCallbackRepository,
    install_loader: Callable[[str, str], Mapping[str, Any] | None],
) -> tuple[dict[str, Any], OAuthProviderContract] | None:
    scope = _repository_scope(state)
    locked = _mapping(repository.lock_oauth_install_for_update(**scope))
    if (
        _text(locked.get("install_id")) != _text(state.get("extension_install_id"))
        or _text(locked.get("account_id")) != _text(state.get("account_id"))
        or _text(locked.get("extension_id")) != _text(state.get("extension_id"))
        or _text(locked.get("release_id")) != _text(state.get("extension_release_id"))
        or _text(locked.get("state")) != "active"
    ):
        return None
    current = _current_install_provider(state, install_loader=install_loader)
    if current is None or _text(current[0].get("release_id")) != _text(locked.get("release_id")):
        return None
    return current


def _reconnect_subject_mismatch(
    *,
    provider: OAuthProviderContract,
    state: Mapping[str, Any],
    subject: str,
    repository: OAuthCallbackRepository,
    transaction_factory: Callable[[], AbstractContextManager[Any]],
) -> bool:
    """Whether a reconnect is trying to move a connection to another subject.

    Reconnecting replaces the grant on a connection that already exists, and
    that connection's identity is the subject it was bound to. Letting a
    reconnect land a different subject would silently repoint it: workflows
    naming that connection would keep running and start addressing a different
    subject, which is the failure that would go unnoticed longest.

    Changing subject is a new connection, not a reconnection.
    """
    if provider.callback_verification is None:
        return False
    existing_connection_id = _text(state.get("existing_connection_id"))
    if not existing_connection_id:
        return False
    try:
        with transaction_factory():
            rows = repository.list_connections_for_configuration(**_repository_scope(dict(state)))
    except Exception:
        # Unable to confirm the connection's current subject. Refusing costs a
        # restart; proceeding could repoint a live connection.
        return True
    connection_type = _text(state.get("connection_type"))
    for row in rows or []:
        candidate = _mapping(row)
        if _text(candidate.get("connection_id")) != existing_connection_id:
            continue
        if _text(candidate.get("connection_type")) != connection_type:
            return True
        oauth = _mapping(_mapping(candidate.get("connection_config")).get("oauth"))
        bound = _text(oauth.get("subject"))
        # A connection that has never completed a grant has no subject yet;
        # the first successful authorization is what gives it one.
        return bool(bound) and bound != subject
    # The state names a connection this configuration does not hold.
    return True


def _duplicate_subject_connection_id(
    *,
    provider: OAuthProviderContract,
    state: Mapping[str, Any],
    subject: str,
    repository: OAuthCallbackRepository,
    transaction_factory: Callable[[], AbstractContextManager[Any]],
) -> str:
    """An existing active connection this new grant would displace, if any.

    Only for providers that ask for it, and only for a new connection:
    reconnecting an existing one is exactly how a grant is meant to be
    replaced. Two different configurations may hold the same subject, because
    they can be two different applications at the provider.
    """
    if not provider.unique_subject_per_configuration or not subject:
        return ""
    if _text(state.get("existing_connection_id")):
        return ""
    try:
        with transaction_factory():
            rows = repository.list_connections_for_configuration(**_repository_scope(dict(state)))
    except Exception:
        # Failing to answer is not the same as answering "no duplicate". A
        # grant that might retire a working token is not worth the risk.
        return "unknown"
    for row in rows or []:
        candidate = _mapping(row)
        if candidate.get("enabled") is not True:
            continue
        # Deliberately not filtered by connection type. The provider issues one
        # token per application and subject regardless of which connection type
        # asked for it, so a grant for one type would retire the token held by
        # a connection of another. The policy is named for the configuration
        # because that is the scope the provider actually enforces.
        oauth = _mapping(_mapping(candidate.get("connection_config")).get("oauth"))
        if _text(oauth.get("lifecycle_status")) == "disconnecting":
            continue
        if _text(oauth.get("subject")) == subject:
            return _text(candidate.get("connection_id")) or "unknown"
    return ""
