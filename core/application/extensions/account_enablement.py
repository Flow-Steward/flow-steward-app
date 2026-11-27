"""Per-account enablement of an extension installed once per installation (ADR 0020, C6).

The extension's code is installed once, in the installation account
(:data:`core.application.auth.installation_admin.INSTALLATION_ACCOUNT_ID`), by an
installation administrator. Every other account uses that install only after its own
administrator enabled it there. The row also keeps that account's non-secret settings;
credentials stay in the account's secret references and connections.

The installation account needs no row: it owns the install, so its enablement is the
install's own state. An account that did not enable an extension resolves it as absent.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from core.application.auth.installation_admin import (
    INSTALLATION_ACCOUNT_ID,
    is_installation_account,
)
from core.application.management.errors import (
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.application.management.scope import ScopeContext, normalize_scope

from .lifecycle_ports import require_role

ENABLED = "enabled"
DISABLED = "disabled"
#: An account that has no row for an installed extension yet.
NOT_ENABLED = "not_enabled"
#: The row of the installation account: it owns the install.
INSTALLATION = "installation"

MAX_SETTINGS_BYTES = 64 * 1024
_SECRET_KEY_MARKERS = ("password", "secret", "token", "api_key", "apikey", "credential")


@dataclass(frozen=True)
class ExtensionAccountEnablement:
    account_id: str
    extension_id: str
    install_id: str
    state: str = ENABLED
    settings: dict[str, Any] = field(default_factory=dict)
    enabled_at: str = ""
    enabled_by: str = ""
    updated_at: str = ""
    updated_by: str = ""

    @property
    def enabled(self) -> bool:
        return self.state == ENABLED

    def view(self) -> dict[str, Any]:
        return {
            "extension_id": self.extension_id,
            "install_id": self.install_id,
            "state": self.state,
            "enabled": self.enabled,
            "settings": dict(self.settings),
            "enabled_at": self.enabled_at,
            "enabled_by": self.enabled_by,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


def _text(value: Any) -> str:
    return str(value or "").strip()


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return _text(value)


def _settings(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _row_to_enablement(row: Mapping[str, Any]) -> ExtensionAccountEnablement:
    return ExtensionAccountEnablement(
        account_id=_text(row.get("account_id")),
        extension_id=_text(row.get("extension_id")),
        install_id=_text(row.get("install_id")),
        state=_text(row.get("state")) or ENABLED,
        settings=_settings(row.get("settings_jsonb")),
        enabled_at=_iso(row.get("enabled_at")),
        enabled_by=_text(row.get("enabled_by")),
        updated_at=_iso(row.get("updated_at")),
        updated_by=_text(row.get("updated_by")),
    )


_COLUMNS = (
    "account_id::text AS account_id, extension_id, install_id, state, settings_jsonb, "
    "enabled_at, enabled_by, updated_at, updated_by"
)


class DBExtensionAccountEnablementStore:
    """PostgreSQL store of ``extension_account_enablements`` (owned by ``db/schema``)."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def _rows(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            names = [column[0] for column in cur.description or ()]
            return [dict(zip(names, row, strict=False)) for row in cur.fetchall()]

    def get(self, *, account_id: str, extension_id: str) -> ExtensionAccountEnablement | None:
        rows = self._rows(
            f"SELECT {_COLUMNS} FROM extension_account_enablements"  # nosec B608 - constant column list
            " WHERE account_id = %s AND extension_id = %s",
            (account_id, extension_id),
        )
        return _row_to_enablement(rows[0]) if rows else None

    def list_for_account(self, account_id: str) -> list[ExtensionAccountEnablement]:
        rows = self._rows(
            f"SELECT {_COLUMNS} FROM extension_account_enablements"  # nosec B608 - constant column list
            " WHERE account_id = %s ORDER BY extension_id",
            (account_id,),
        )
        return [_row_to_enablement(row) for row in rows]

    def accounts_for_install(self, install_id: str) -> list[str]:
        """Every account that has a row for the install, enabled or not."""
        rows = self._rows(
            "SELECT account_id::text AS account_id FROM extension_account_enablements"
            " WHERE install_id = %s ORDER BY account_id",
            (install_id,),
        )
        return [_text(row.get("account_id")) for row in rows]

    def save(self, row: ExtensionAccountEnablement) -> ExtensionAccountEnablement:
        rows = self._rows(
            f"""
            INSERT INTO extension_account_enablements (
                account_id, extension_id, install_id, state, settings_jsonb,
                enabled_at, enabled_by, updated_at, updated_by
            ) VALUES (
                %s, %s, %s, %s, %s::jsonb,
                CASE WHEN %s = 'enabled' THEN now() END, %s, now(), %s
            )
            ON CONFLICT (account_id, extension_id) DO UPDATE SET
                install_id = EXCLUDED.install_id,
                state = EXCLUDED.state,
                settings_jsonb = EXCLUDED.settings_jsonb,
                enabled_at = CASE
                    WHEN EXCLUDED.state = 'enabled'
                     AND extension_account_enablements.state <> 'enabled'
                    THEN now()
                    ELSE extension_account_enablements.enabled_at
                END,
                enabled_by = CASE
                    WHEN EXCLUDED.state = 'enabled'
                     AND extension_account_enablements.state <> 'enabled'
                    THEN EXCLUDED.enabled_by
                    ELSE extension_account_enablements.enabled_by
                END,
                updated_at = now(),
                updated_by = EXCLUDED.updated_by
            RETURNING {_COLUMNS}
            """,  # nosec B608 - constant column list; values are bound parameters
            (
                row.account_id,
                row.extension_id,
                row.install_id,
                row.state,
                json.dumps(row.settings),
                row.state,
                row.enabled_by,
                row.updated_by,
            ),
        )
        return _row_to_enablement(rows[0])


class InMemoryExtensionAccountEnablementStore:
    """The same contract in memory, for tests and offline tools."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], ExtensionAccountEnablement] = {}

    def get(self, *, account_id: str, extension_id: str) -> ExtensionAccountEnablement | None:
        return self._rows.get((account_id, extension_id))

    def list_for_account(self, account_id: str) -> list[ExtensionAccountEnablement]:
        return sorted(
            (row for (account, _), row in self._rows.items() if account == account_id),
            key=lambda row: row.extension_id,
        )

    def accounts_for_install(self, install_id: str) -> list[str]:
        return sorted(row.account_id for row in self._rows.values() if row.install_id == install_id)

    def save(self, row: ExtensionAccountEnablement) -> ExtensionAccountEnablement:
        now = datetime.now(UTC).isoformat()
        previous = self._rows.get((row.account_id, row.extension_id))
        newly_enabled = row.state == ENABLED and (previous is None or not previous.enabled)
        saved = replace(
            row,
            enabled_at=now if newly_enabled else (previous.enabled_at if previous else ""),
            enabled_by=row.enabled_by
            if newly_enabled
            else (previous.enabled_by if previous else ""),
            updated_at=now,
        )
        self._rows[(row.account_id, row.extension_id)] = saved
        return saved


def validate_account_settings(value: Any) -> dict[str, Any]:
    """Settings are a JSON object of non-secret values, at most 64 KiB."""
    if not isinstance(value, Mapping):
        raise ServiceValidationError("Extension settings must be an object")
    settings = dict(value)
    try:
        encoded = json.dumps(settings, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ServiceValidationError("Extension settings must be plain JSON values") from exc
    if len(encoded.encode("utf-8")) > MAX_SETTINGS_BYTES:
        raise ServiceValidationError("Extension settings are larger than 64 KiB")
    for path in _setting_key_paths(settings):
        lowered = path[-1].lower()
        if any(marker in lowered for marker in _SECRET_KEY_MARKERS):
            raise ServiceValidationError(
                f"Setting '{'.'.join(path)}' looks like a secret: keep it in a secret"
                " reference of a connection instead"
            )
    return settings


def _setting_key_paths(value: Any, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Every key of *value*, nested objects and objects inside lists included."""
    paths: list[tuple[str, ...]] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            path = (*prefix, str(key))
            paths.append(path)
            paths.extend(_setting_key_paths(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.extend(_setting_key_paths(child, (*prefix, f"[{index}]")))
    return paths


def install_reaches_account(install: Any, account_id: str, enablements: Any) -> bool:
    """Whether ``account_id`` may use ``install``: it owns it, the install is shared
    with every account (``visibility = 'global'``), or the account enabled it."""
    normalized_account = _text(account_id)
    if not normalized_account or install is None:
        return False
    if _text(getattr(install, "account_id", "")) == normalized_account:
        return True
    visibility = getattr(install, "visibility", "")
    visibility = _text(getattr(visibility, "value", visibility)).lower()
    state = getattr(install, "state", "")
    state = _text(getattr(state, "value", state)).lower()
    if visibility == "global":
        return True
    if state != "active" or enablements is None:
        return False
    row = enablements.get(
        account_id=normalized_account, extension_id=_text(getattr(install, "extension_id", ""))
    )
    return bool(
        row is not None
        and row.enabled
        and row.install_id == _text(getattr(install, "install_id", ""))
    )


def account_enablement_state(install: Any, account_id: str, enablements: Any) -> str:
    """``enabled``, ``disabled`` or ``not_enabled`` of an installation-owned install for
    another account; empty for the installation account and for any other install."""
    normalized_account = _text(account_id)
    if (
        install is None
        or not normalized_account
        or is_installation_account(normalized_account)
        or _text(getattr(install, "account_id", "")) != INSTALLATION_ACCOUNT_ID
        or not isinstance(
            enablements,
            (DBExtensionAccountEnablementStore, InMemoryExtensionAccountEnablementStore),
        )
    ):
        return ""
    row = enablements.get(
        account_id=normalized_account, extension_id=_text(getattr(install, "extension_id", ""))
    )
    if row is None or row.install_id != _text(getattr(install, "install_id", "")):
        return NOT_ENABLED
    return row.state


def install_serves_other_accounts(install: Any, enablements: Any) -> bool:
    """Whether uninstalling ``install`` touches accounts other than its owner."""
    visibility = getattr(install, "visibility", "")
    if _text(getattr(visibility, "value", visibility)).lower() == "global":
        return True
    install_id = _text(getattr(install, "install_id", ""))
    if not install_id or enablements is None:
        return False
    owner = _text(getattr(install, "account_id", ""))
    return any(account != owner for account in enablements.accounts_for_install(install_id))


class ExtensionAccountEnablementService:
    """Enable, disable and configure an installed extension for the caller's account."""

    def __init__(
        self,
        *,
        get_install_store: Callable[[], Any],
        get_enablement_store: Callable[[], Any],
    ) -> None:
        self._get_install_store = get_install_store
        self._get_enablement_store = get_enablement_store

    @staticmethod
    def _admin_scope(scope: ScopeContext | None) -> ScopeContext:
        resolved = normalize_scope(scope)
        require_role(resolved, "admin")
        account_id = _text(resolved.account_id)
        if not account_id:
            raise ServiceForbiddenError("Authentication required")
        if is_installation_account(account_id):
            raise ServiceValidationError(
                "The installation account turns an extension on or off on its install:"
                " use Enable or Disable"
            )
        return resolved

    def _installation_install(self, extension_id: str) -> Any:
        install = self._get_install_store().get(
            extension_id=extension_id, account_id=INSTALLATION_ACCOUNT_ID
        )
        if install is None:
            raise ServiceNotFoundError(f"Extension '{extension_id}' not found")
        return install

    def _own_install(self, extension_id: str, account_id: str) -> Any:
        return self._get_install_store().get(extension_id=extension_id, account_id=account_id)

    def get(self, extension_id: str, *, scope: ScopeContext | None = None) -> dict[str, Any]:
        resolved = self._admin_scope(scope)
        normalized_id = _text(extension_id)
        install = self._installation_install(normalized_id)
        row = self._get_enablement_store().get(
            account_id=resolved.account_id, extension_id=normalized_id
        )
        if row is None:
            return {
                "extension_id": normalized_id,
                "install_id": _text(install.install_id),
                "state": NOT_ENABLED,
                "enabled": False,
                "settings": {},
            }
        return row.view()

    def _save(
        self,
        extension_id: str,
        *,
        scope: ScopeContext | None,
        state: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved = self._admin_scope(scope)
        account_id = _text(resolved.account_id)
        normalized_id = _text(extension_id)
        if self._own_install(normalized_id, account_id) is not None:
            raise ServiceValidationError(
                "This account has its own install of the extension; it is turned on or off"
                " on that install"
            )
        install = self._installation_install(normalized_id)
        install_state = getattr(install, "state", "")
        install_state = _text(getattr(install_state, "value", install_state)).lower()
        store = self._get_enablement_store()
        previous = store.get(account_id=account_id, extension_id=normalized_id)
        next_state = state or (previous.state if previous is not None else DISABLED)
        if next_state == ENABLED and install_state != "active":
            raise ServiceValidationError(
                "The installation administrator has turned this extension off; it cannot be"
                " enabled for this company now"
            )
        actor = _text(resolved.actor_id) or _text(resolved.user_id)
        saved = store.save(
            ExtensionAccountEnablement(
                account_id=account_id,
                extension_id=normalized_id,
                install_id=_text(install.install_id),
                state=next_state,
                settings=(
                    settings
                    if settings is not None
                    else (dict(previous.settings) if previous is not None else {})
                ),
                enabled_by=actor,
                updated_by=actor,
            )
        )
        return saved.view()

    def enable(self, extension_id: str, *, scope: ScopeContext | None = None) -> dict[str, Any]:
        return self._save(extension_id, scope=scope, state=ENABLED)

    def disable(self, extension_id: str, *, scope: ScopeContext | None = None) -> dict[str, Any]:
        return self._save(extension_id, scope=scope, state=DISABLED)

    def update_settings(
        self,
        extension_id: str,
        settings: Any,
        *,
        scope: ScopeContext | None = None,
    ) -> dict[str, Any]:
        return self._save(extension_id, scope=scope, settings=validate_account_settings(settings))


def default_enablement_store(get_logger: Callable[[], Any]) -> DBExtensionAccountEnablementStore:
    """The database store on the logger's connection; refused without a database."""
    try:
        bl = get_logger()
    except Exception as exc:
        raise ServiceUnavailableError("Extension enablement store is unavailable") from exc
    conn = getattr(bl, "conn", None) if bl is not None else None
    if conn is None:
        raise ServiceUnavailableError("Extension enablement store is unavailable")
    return DBExtensionAccountEnablementStore(conn)


__all__ = [
    "DISABLED",
    "ENABLED",
    "INSTALLATION",
    "NOT_ENABLED",
    "DBExtensionAccountEnablementStore",
    "ExtensionAccountEnablement",
    "ExtensionAccountEnablementService",
    "InMemoryExtensionAccountEnablementStore",
    "account_enablement_state",
    "default_enablement_store",
    "install_reaches_account",
    "install_serves_other_accounts",
    "validate_account_settings",
]
