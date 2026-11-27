"""Deployment-operator allowlist of secret backend configurations per account.

ADR 0066 amendment (2026-11-27). A secret backend configuration is saved by an
account administrator, but the backend's credentials come from the deployment and
are shared by every account of the installation. Without a limit an administrator
of one account could point a configuration at a vault path, key vault or project
that holds another account's secrets, and adopt or read them. The installation's
operator therefore states, outside every account's reach, which configuration
values each account may use.

The policy is an installation setting, never account-editable:

* ``FS_SECRET_BACKEND_POLICY``: JSON, or
* ``FS_SECRET_BACKEND_POLICY_FILE``: a path to a file holding the same JSON (the
  variable wins when both are set).

Shape::

    {
      "<account id>": {
        "<backend kind>": {"allowed_values": {"<config field>": ["value", ...]}}
      }
    }

Rules (fail closed):

* An account listed in the policy may use only the kinds listed under it; each field
  named in ``allowed_values`` must be present in the configuration and every value
  of it (a list is checked item by item) must be one of the listed values. Fields
  not named are left to the backend.
* An account not listed: the installation account is unrestricted (a single-account
  installation is unaffected); any other account may use no external backend.
* A policy that cannot be read or parsed refuses every external configuration.

The per-kind entry is also passed to the backend's ``validate_config(config,
policy)`` so it can apply rules Core cannot express (for example path prefixes).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID
from core.infrastructure.config.runtime_env import runtime_env

POLICY_ENV = "FS_SECRET_BACKEND_POLICY"
POLICY_FILE_ENV = "FS_SECRET_BACKEND_POLICY_FILE"
#: The installation account (``core.application.auth.installation_admin``).
INSTALLATION_ACCOUNT_ID = DEFAULT_ACCOUNT_ID
_MAX_POLICY_BYTES = 256 * 1024

ACCOUNT_NOT_ALLOWED_MESSAGE = (
    "The installation's operator has not allowed external secret storage for this account"
)
KIND_NOT_ALLOWED_MESSAGE = (
    "The installation's operator has not allowed this kind of secret storage for this account"
)
POLICY_UNREADABLE_MESSAGE = (
    "The installation's secret storage policy cannot be read, so external secret storage is refused"
)


class SecretBackendPolicyRefused(PermissionError):
    """The deployment policy does not allow this account, kind or configuration."""


_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {}
_UNSET: Any = object()


def _raw_policy() -> str | None:
    raw = str(runtime_env.get(POLICY_ENV) or "").strip()
    if raw:
        return raw
    path = str(runtime_env.get(POLICY_FILE_ENV) or "").strip()
    if not path:
        return None
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise SecretBackendPolicyRefused(POLICY_UNREADABLE_MESSAGE) from exc
    if len(data) > _MAX_POLICY_BYTES:
        raise SecretBackendPolicyRefused(POLICY_UNREADABLE_MESSAGE)
    return data.decode("utf-8", errors="replace").strip() or None


def _parsed(raw: str) -> dict[str, dict[str, dict[str, Any]]]:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise SecretBackendPolicyRefused(POLICY_UNREADABLE_MESSAGE) from exc
    if not isinstance(payload, dict):
        raise SecretBackendPolicyRefused(POLICY_UNREADABLE_MESSAGE)
    policy: dict[str, dict[str, dict[str, Any]]] = {}
    for account_id, kinds in payload.items():
        if not isinstance(kinds, dict):
            raise SecretBackendPolicyRefused(POLICY_UNREADABLE_MESSAGE)
        entries: dict[str, dict[str, Any]] = {}
        for kind, entry in kinds.items():
            if not isinstance(entry, dict):
                raise SecretBackendPolicyRefused(POLICY_UNREADABLE_MESSAGE)
            allowed = entry.get("allowed_values", {})
            if not isinstance(allowed, dict) or not all(
                isinstance(values, list) for values in allowed.values()
            ):
                raise SecretBackendPolicyRefused(POLICY_UNREADABLE_MESSAGE)
            entries[str(kind).strip()] = {
                **entry,
                "allowed_values": {str(k): list(v) for k, v in allowed.items()},
            }
        policy[str(account_id).strip()] = entries
    return policy


def load_policy() -> dict[str, dict[str, dict[str, Any]]] | None:
    """The deployment policy; ``None`` when none is configured. Raises when unreadable."""
    raw = _raw_policy()
    if raw is None:
        return None
    with _LOCK:
        if _CACHE.get("raw") == raw:
            return _CACHE["policy"]
    policy = _parsed(raw)
    with _LOCK:
        _CACHE.clear()
        _CACHE.update(raw=raw, policy=policy)
    return policy


def kind_policy(account_id: Any, kind: Any, *, policy: Any = _UNSET) -> dict[str, Any] | None:
    """The policy entry of ``kind`` for ``account_id``; ``None`` when unrestricted.

    Raises :class:`SecretBackendPolicyRefused` when the account may not use ``kind``.
    """
    account = str(account_id or "").strip()
    normalized_kind = str(kind or "").strip()
    loaded = load_policy() if policy is _UNSET else policy
    entries = (loaded or {}).get(account)
    if entries is None:
        if account and account == INSTALLATION_ACCOUNT_ID:
            return None
        raise SecretBackendPolicyRefused(ACCOUNT_NOT_ALLOWED_MESSAGE)
    entry = entries.get(normalized_kind)
    if entry is None:
        raise SecretBackendPolicyRefused(KIND_NOT_ALLOWED_MESSAGE)
    return dict(entry)


def check_config(config: Mapping[str, Any], entry: Mapping[str, Any] | None) -> None:
    """Refuse a configuration whose fields fall outside the entry's allowed values."""
    if entry is None:
        return
    allowed_values = entry.get("allowed_values") or {}
    for field, allowed in sorted(allowed_values.items()):
        if field not in config:
            raise SecretBackendPolicyRefused(
                f"config: '{field}' is required by the installation's secret storage policy"
            )
        value = config.get(field)
        values = value if isinstance(value, list) else [value]
        allowed_set = {json.dumps(item, sort_keys=True) for item in allowed}
        if not values or any(
            json.dumps(item, sort_keys=True) not in allowed_set for item in values
        ):
            raise SecretBackendPolicyRefused(
                f"config: this value of '{field}' is not allowed for this account by the"
                " installation's secret storage policy"
            )


def require_config_allowed(account_id: Any, kind: Any, config: Mapping[str, Any]) -> None:
    """:func:`kind_policy` then :func:`check_config`; raises when refused."""
    check_config(config, kind_policy(account_id, kind))


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


__all__ = [
    "ACCOUNT_NOT_ALLOWED_MESSAGE",
    "INSTALLATION_ACCOUNT_ID",
    "KIND_NOT_ALLOWED_MESSAGE",
    "POLICY_ENV",
    "POLICY_FILE_ENV",
    "POLICY_UNREADABLE_MESSAGE",
    "SecretBackendPolicyRefused",
    "check_config",
    "clear_cache",
    "kind_policy",
    "load_policy",
    "require_config_allowed",
]
