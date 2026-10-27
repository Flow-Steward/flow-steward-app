"""Provider-neutral OAuth release upgrade impact classification."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from core.application.management.errors import ServicePreconditionError


@dataclass(frozen=True)
class OAuthReleaseImpact:
    permission_approval_required: bool
    invalidate_pending_state_ids: tuple[str, ...]
    reauthorize_connection_ids: tuple[str, ...]


@dataclass(frozen=True)
class PreparedOAuthReleaseTransition:
    account_id: str
    install_id: str
    extension_id: str
    old_release_id: str
    new_release_id: str
    impact: OAuthReleaseImpact
    provider_fingerprints: dict[str, tuple[str, str]]
    old_contract: Mapping[str, Any]
    new_contract: Mapping[str, Any]


class OAuthReleaseUpgradeRepository(Protocol):
    def read_oauth_release_inventory(
        self, *, account_id: str, install_id: str, extension_id: str, old_release_id: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]: ...

    def lock_oauth_release_inventory(
        self, *, account_id: str, install_id: str, extension_id: str, old_release_id: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]: ...

    def apply_oauth_release_impact(self, **values: Any) -> None: ...


def _text(value: Any) -> str:
    return str(value or "").strip()


def _texts(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    return {_text(item) for item in value if _text(item)}


def _rows(contract: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = contract.get(key)
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _providers(contract: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        provider_id: row
        for row in _rows(contract, "oauth_providers")
        if (provider_id := _text(row.get("provider_id")))
    }


def _connection_types(
    contract: Mapping[str, Any],
) -> dict[tuple[str, str], tuple[set[str], set[str]]]:
    result: dict[tuple[str, str], tuple[set[str], set[str]]] = {}
    for row in _rows(contract, "connection_types"):
        connection_type = _text(row.get("connection_type_id"))
        auth = row.get("auth")
        if not connection_type or not isinstance(auth, Mapping):
            continue
        if _text(auth.get("type")).lower() != "oauth2":
            continue
        provider_id = _text(auth.get("provider_id"))
        scopes = auth.get("scopes")
        if not provider_id or not isinstance(scopes, Mapping):
            continue
        result[(provider_id, connection_type)] = (
            _texts(scopes.get("required")),
            _texts(scopes.get("optional")),
        )
    return result


def _provider_configuration_ids(
    configurations: Iterable[Mapping[str, Any]], provider_id: str
) -> set[str]:
    return {
        _text(row.get("configuration_id"))
        for row in configurations
        if _text(row.get("provider_id")) == provider_id and _text(row.get("configuration_id"))
    }


def _pending_state_ids(
    configurations: Iterable[Mapping[str, Any]],
    *,
    provider_id: str,
    connection_types: set[str] | None,
) -> set[str]:
    result: set[str] = set()
    for configuration in configurations:
        if _text(configuration.get("provider_id")) != provider_id:
            continue
        pending_states = configuration.get("pending_states")
        if not isinstance(pending_states, list):
            continue
        for state in pending_states:
            if not isinstance(state, Mapping):
                continue
            state_connection_type = _text(
                state.get("connection_type") or state.get("connection_type_id")
            )
            if connection_types is not None and state_connection_type not in connection_types:
                continue
            state_id = _text(state.get("state_id") or state.get("state_hash"))
            if state_id:
                result.add(state_id)
    return result


def _provider_connections(
    connections: Iterable[Mapping[str, Any]],
    configurations: Iterable[Mapping[str, Any]],
    provider_id: str,
) -> list[Mapping[str, Any]]:
    configuration_ids = _provider_configuration_ids(configurations, provider_id)
    return [
        row
        for row in connections
        if _text(row.get("provider_id")) == provider_id
        and _text(row.get("configuration_id")) in configuration_ids
    ]


def _scope_impact(
    *,
    provider_id: str,
    old_scopes: dict[tuple[str, str], tuple[set[str], set[str]]],
    new_scopes: dict[tuple[str, str], tuple[set[str], set[str]]],
    connections: list[Mapping[str, Any]],
) -> tuple[bool, set[str], set[str]]:
    approval = False
    invalidated_types: set[str] = set()
    reauthorize_ids: set[str] = set()
    connection_types = {
        connection_type
        for known_provider, connection_type in set(old_scopes) | set(new_scopes)
        if known_provider == provider_id
    }
    for connection_type in connection_types:
        old_required, old_optional = old_scopes.get((provider_id, connection_type), (set(), set()))
        new_required, new_optional = new_scopes.get((provider_id, connection_type), (set(), set()))
        if (old_required, old_optional) == (new_required, new_optional):
            continue
        approval = True
        added_required = new_required - old_required
        removed = (old_required | old_optional) - (new_required | new_optional)
        if old_required != new_required or removed:
            invalidated_types.add(connection_type)
        for connection in connections:
            if (
                _text(connection.get("connection_type") or connection.get("connection_type_id"))
                != connection_type
            ):
                continue
            granted = _texts(connection.get("granted_scopes"))
            if not added_required.issubset(granted) or bool(removed & granted):
                connection_id = _text(connection.get("connection_id"))
                if connection_id:
                    reauthorize_ids.add(connection_id)
    return approval, invalidated_types, reauthorize_ids


def evaluate_oauth_release_impact(
    old_contract: Mapping[str, Any],
    new_contract: Mapping[str, Any],
    configurations: Iterable[Mapping[str, Any]],
    connections: Iterable[Mapping[str, Any]],
) -> OAuthReleaseImpact:
    """Return the exact stored OAuth resources affected by a release change."""
    configuration_rows = list(configurations)
    connection_rows = list(connections)
    old_providers = _providers(old_contract)
    new_providers = _providers(new_contract)
    old_scopes = _connection_types(old_contract)
    new_scopes = _connection_types(new_contract)
    approval = set(old_providers) != set(new_providers)
    invalidate_ids: set[str] = set()
    reauthorize_ids: set[str] = set()

    for provider_id in sorted(set(old_providers) | set(new_providers)):
        old_provider = old_providers.get(provider_id)
        new_provider = new_providers.get(provider_id)
        provider_connections = _provider_connections(
            connection_rows, configuration_rows, provider_id
        )
        security_changed = (
            old_provider is None
            or new_provider is None
            or _text(old_provider.get("security_fingerprint"))
            != _text(new_provider.get("security_fingerprint"))
        )
        revocation_changed = (
            old_provider is not None
            and new_provider is not None
            and _text(old_provider.get("revocation_fingerprint"))
            != _text(new_provider.get("revocation_fingerprint"))
        )
        if security_changed:
            approval = True
            invalidate_ids.update(
                _pending_state_ids(
                    configuration_rows, provider_id=provider_id, connection_types=None
                )
            )
            reauthorize_ids.update(
                _text(row.get("connection_id"))
                for row in provider_connections
                if _text(row.get("connection_id"))
            )
            continue
        approval = approval or revocation_changed
        scope_approval, invalidated_types, scope_connection_ids = _scope_impact(
            provider_id=provider_id,
            old_scopes=old_scopes,
            new_scopes=new_scopes,
            connections=provider_connections,
        )
        approval = approval or scope_approval
        invalidate_ids.update(
            _pending_state_ids(
                configuration_rows,
                provider_id=provider_id,
                connection_types=invalidated_types,
            )
        )
        reauthorize_ids.update(scope_connection_ids)

    return OAuthReleaseImpact(
        permission_approval_required=approval,
        invalidate_pending_state_ids=tuple(sorted(invalidate_ids)),
        reauthorize_connection_ids=tuple(sorted(reauthorize_ids)),
    )


def oauth_release_impact_hash(
    *,
    account_id: str,
    install_id: str,
    extension_id: str,
    old_release_id: str,
    new_release_id: str,
    impact: OAuthReleaseImpact,
    candidate_contract_fingerprint: str = "",
) -> str:
    """Bind an approval to exact releases and a canonical impact set."""
    payload = {
        "account_id": _text(account_id),
        "install_id": _text(install_id),
        "extension_id": _text(extension_id),
        "old_release_id": _text(old_release_id),
        "new_release_id": _text(new_release_id),
        "candidate_contract_fingerprint": _text(candidate_contract_fingerprint),
        "permission_approval_required": impact.permission_approval_required,
        "invalidate_pending_state_ids": sorted(impact.invalidate_pending_state_ids),
        "reauthorize_connection_ids": sorted(impact.reauthorize_connection_ids),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def resolve_oauth_release_contract(descriptor: Mapping[str, Any]) -> Mapping[str, Any]:
    """Resolve only canonical persisted contracts or fingerprinted inline fallback."""
    runtime = descriptor.get("runtime")
    runtime = runtime if isinstance(runtime, Mapping) else {}
    compiled = descriptor.get("compiled_manifest")
    compiled = compiled if isinstance(compiled, Mapping) else {}
    compiled_runtime = compiled.get("runtime")
    compiled_runtime = compiled_runtime if isinstance(compiled_runtime, Mapping) else {}
    for owner in (descriptor, runtime, compiled, compiled_runtime):
        contract = owner.get("extension_contract_v2_normalized")
        if isinstance(contract, Mapping):
            return contract
    for owner in (descriptor, compiled):
        providers = _rows(owner, "oauth_providers")
        if providers and all(
            _text(row.get("security_fingerprint")) and _text(row.get("revocation_fingerprint"))
            for row in providers
        ):
            return {
                "oauth_providers": providers,
                "connection_types": _rows(owner, "connection_types"),
            }
    for owner in (runtime, descriptor):
        contract = owner.get("extension_contract_v2")
        if not isinstance(contract, Mapping):
            continue
        providers = _rows(contract, "oauth_providers")
        if providers and all(
            _text(row.get("security_fingerprint")) and _text(row.get("revocation_fingerprint"))
            for row in providers
        ):
            return contract
    return {}


def _provider_fingerprints(contract: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    return {
        provider_id: (
            _text(row.get("security_fingerprint")),
            _text(row.get("revocation_fingerprint")),
        )
        for row in _rows(contract, "oauth_providers")
        if (provider_id := _text(row.get("provider_id")))
    }


def oauth_contract_fingerprint(contract: Mapping[str, Any]) -> str:
    """Fingerprint the normalized provider and OAuth connection permission catalog."""
    payload = {
        "providers": [dict(row) for row in _rows(contract, "oauth_providers")],
        "connection_types": [
            dict(row)
            for row in _rows(contract, "connection_types")
            if isinstance(row.get("auth"), Mapping)
            and _text(row["auth"].get("type")).lower() == "oauth2"
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _impact_payload(impact: OAuthReleaseImpact) -> dict[str, Any]:
    return {
        "permission_approval_required": impact.permission_approval_required,
        "invalidate_pending_state_count": len(impact.invalidate_pending_state_ids),
        "reauthorize_connection_ids": list(impact.reauthorize_connection_ids),
    }


class OAuthReleaseUpgradeService:
    """Prepare and apply one approval-bound OAuth release transition."""

    def __init__(self, repository: OAuthReleaseUpgradeRepository) -> None:
        self._repository = repository

    def prepare(
        self,
        *,
        account_id: str,
        install_id: str,
        extension_id: str,
        old_release_id: str,
        new_release_id: str,
        old_descriptor: Mapping[str, Any],
        new_descriptor: Mapping[str, Any],
        approved_impact_hash: str,
    ) -> PreparedOAuthReleaseTransition:
        configurations, connections = self._repository.read_oauth_release_inventory(
            account_id=account_id,
            install_id=install_id,
            extension_id=extension_id,
            old_release_id=old_release_id,
        )
        old_contract = resolve_oauth_release_contract(old_descriptor)
        new_contract = resolve_oauth_release_contract(new_descriptor)
        impact = evaluate_oauth_release_impact(
            old_contract,
            new_contract,
            configurations,
            connections,
        )
        impact_hash = oauth_release_impact_hash(
            account_id=account_id,
            install_id=install_id,
            extension_id=extension_id,
            old_release_id=old_release_id,
            new_release_id=new_release_id,
            impact=impact,
            candidate_contract_fingerprint=oauth_contract_fingerprint(new_contract),
        )
        if impact.permission_approval_required and approved_impact_hash != impact_hash:
            raise ServicePreconditionError(
                "OAuth release impact approval required",
                payload={
                    "error": "oauth_release_approval_required",
                    "impact_hash": impact_hash,
                    "impact": _impact_payload(impact),
                },
            )
        return PreparedOAuthReleaseTransition(
            account_id=account_id,
            install_id=install_id,
            extension_id=extension_id,
            old_release_id=old_release_id,
            new_release_id=new_release_id,
            impact=impact,
            provider_fingerprints=_provider_fingerprints(new_contract),
            old_contract=old_contract,
            new_contract=new_contract,
        )

    def apply(self, prepared: PreparedOAuthReleaseTransition) -> None:
        configurations, connections = self._repository.lock_oauth_release_inventory(
            account_id=prepared.account_id,
            install_id=prepared.install_id,
            extension_id=prepared.extension_id,
            old_release_id=prepared.old_release_id,
        )
        disconnecting_connection_ids = sorted(
            _text(row.get("connection_id"))
            for row in connections
            if _text(row.get("connection_id"))
            and _text(row.get("lifecycle_status")) == "disconnecting"
        )
        if disconnecting_connection_ids:
            raise ServicePreconditionError(
                "OAuth release cannot be applied while disconnects are in progress",
                payload={
                    "error": "oauth_release_disconnect_in_progress",
                    "connection_ids": disconnecting_connection_ids,
                },
            )
        locked_impact = evaluate_oauth_release_impact(
            prepared.old_contract,
            prepared.new_contract,
            configurations,
            connections,
        )
        if locked_impact != prepared.impact:
            raise RuntimeError("oauth_release_impact_drift")
        self._repository.apply_oauth_release_impact(
            account_id=prepared.account_id,
            install_id=prepared.install_id,
            extension_id=prepared.extension_id,
            old_release_id=prepared.old_release_id,
            new_release_id=prepared.new_release_id,
            invalidate_pending_state_ids=prepared.impact.invalidate_pending_state_ids,
            reauthorize_connection_ids=prepared.impact.reauthorize_connection_ids,
            provider_fingerprints=prepared.provider_fingerprints,
        )
