"""The explicitly chosen connection that sends the platform's own email.

A platform administrator picks one connection in Settings -> Email. Candidates
are enabled connections of an enabled install of a notification provider whose
connection type addresses a recipient. The setting stores the connection, its
install and type, and a *confirmation*: the connection's ``sender_revision`` and
the OAuth configuration's ``configuration_revision`` at the moment the
administrator confirmed. Both counters only grow (database triggers move them
on every change of who sends), so the confirmation stays valid exactly as long
as nothing changed. Disabling and re-enabling the connection, editing its
settings or secrets, reconnecting another account, moving it to another
install, or editing the OAuth configuration all revoke it until an
administrator confirms again. There is no fallback to another sender.

A send reads the connection once, checks it, builds ``target.connection`` from
that same read, and at the admission point -- after everything is built and
before the external-effect claim and the extension call -- re-reads the setting
and both counters. Any difference fails the send with ``system_sender_changed``.

An install that never wrote the setting is pinned once to what the earlier rule
picked (the oldest enabled connection of a recipient notification provider),
with an audit entry; after that only the explicit setting applies.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

SETTING_KEY = "system_email_connection_id"
SYSTEM_SENDER_CHANGED = "system_sender_changed"
SYSTEM_SENDER_LOCKED = "system_sender_locked"

SOURCE_ADMIN = "admin"
SOURCE_UPGRADE_PIN = "upgrade_pin"

REASON_NOT_SELECTED = "not_selected"
REASON_CONNECTION_MISSING = "connection_missing"
REASON_CONNECTION_DISABLED = "connection_disabled"
REASON_INSTALL_UNAVAILABLE = "install_unavailable"
REASON_NOT_RECIPIENT = "not_recipient"
REASON_IDENTITY_CHANGED = "identity_changed"
REASON_CONFIRMATION_REVOKED = "confirmation_revoked"

#: Test-only hooks, keyed by point name; production code never sets one.
FAULT_POINTS: dict[str, Callable[[], None]] = {}


def fault_point(name: str) -> None:
    hook = FAULT_POINTS.get(name)
    if hook is not None:
        hook()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class SenderSetting:
    """The stored selection; an empty ``connection_id`` means no system email."""

    connection_id: str = ""
    extension_install_id: str = ""
    connection_type: str = ""
    sender_revision: int = 0
    configuration_id: str = ""
    configuration_revision: int = 0
    source: str = SOURCE_ADMIN
    confirmed_by: str = ""
    confirmed_at: str = ""

    @classmethod
    def from_value(cls, value: Any) -> SenderSetting:
        payload = _as_dict(value)
        return cls(
            connection_id=_text(payload.get("connection_id")),
            extension_install_id=_text(payload.get("extension_install_id")),
            connection_type=_text(payload.get("connection_type")),
            sender_revision=_int(payload.get("sender_revision")),
            configuration_id=_text(payload.get("configuration_id")),
            configuration_revision=_int(payload.get("configuration_revision")),
            source=_text(payload.get("source")) or SOURCE_ADMIN,
            confirmed_by=_text(payload.get("confirmed_by")),
            confirmed_at=_text(payload.get("confirmed_at")),
        )

    def as_value(self) -> dict[str, Any]:
        return {
            "connection_id": self.connection_id or None,
            "extension_install_id": self.extension_install_id or None,
            "connection_type": self.connection_type or None,
            "sender_revision": self.sender_revision,
            "configuration_id": self.configuration_id or None,
            "configuration_revision": self.configuration_revision,
            "source": self.source,
            "confirmed_by": self.confirmed_by or None,
            "confirmed_at": self.confirmed_at or None,
        }

    def same_selection(self, other: SenderSetting | None) -> bool:
        return other is not None and (
            self.connection_id,
            self.extension_install_id,
            self.connection_type,
        ) == (other.connection_id, other.extension_install_id, other.connection_type)


@dataclass(frozen=True)
class SenderStatus:
    """Whether system email can be sent now, and through what."""

    available: bool
    reason: str = ""
    setting: SenderSetting | None = None
    connection: dict[str, Any] = field(default_factory=dict)
    extension: dict[str, Any] = field(default_factory=dict)
    capability: Any = None
    configuration_revision: int = 0


def _conn(bl: Any) -> Any:
    conn = getattr(bl, "conn", None)
    if conn is None:
        raise RuntimeError("system email sender storage is not available")
    return conn


def read_setting(bl: Any) -> tuple[bool, SenderSetting | None]:
    """``(written, setting)``; ``written`` is False for an install that never wrote it."""
    from core.infrastructure.db.system_settings_store import get_system_setting

    row = get_system_setting(conn=_conn(bl), setting_key=SETTING_KEY)
    if not isinstance(row, dict):
        return False, None
    return True, SenderSetting.from_value(row.get("setting_value_jsonb"))


def _save_setting(bl: Any, setting: SenderSetting, *, actor: str) -> None:
    from core.infrastructure.db.system_settings_store import save_system_setting

    save_system_setting(
        conn=_conn(bl), setting_key=SETTING_KEY, value=setting.as_value(), updated_by=actor
    )
    clear_availability_cache()


def _insert_setting_if_absent(bl: Any, setting: SenderSetting) -> bool:
    import json

    from core.infrastructure.db.system_settings_store import normalize_account_id

    with _conn(bl).cursor() as cur:
        cur.execute(
            """
            INSERT INTO system_settings (account_id, setting_key, setting_value_jsonb, updated_by)
            VALUES (%s, %s, %s::jsonb, 'system')
            ON CONFLICT (account_id, setting_key) DO NOTHING
            """,
            (normalize_account_id(None), SETTING_KEY, json.dumps(setting.as_value())),
        )
        return int(getattr(cur, "rowcount", 0) or 0) == 1


_IDENTITY_SQL = """
    SELECT connection.connection_id, connection.project_id, connection.extension_id,
           connection.account_id, connection.display_name, connection.connection_kind,
           connection.connection_type, connection.environment,
           connection.credential_revision, connection.extension_install_id,
           connection.oauth_configuration_id, connection.oauth_provider_id,
           connection.oauth_extension_release_id, connection.oauth_authorization_revision,
           connection.oauth_granted_scopes, connection.enabled, connection.connection_config,
           connection.secret_refs, connection.connection_routes, connection.ingress_config,
           connection.workflow_mappings, connection.sender_revision,
           connection.credentials_configuration_id::text AS credentials_configuration_id,
           COALESCE(connection.oauth_configuration_id,
                    connection.credentials_configuration_id)::text AS signing_configuration_id,
           configuration.configuration_revision
      FROM project_connections AS connection
      LEFT JOIN extension_oauth_configurations AS configuration
        ON configuration.configuration_id = COALESCE(
               connection.oauth_configuration_id, connection.credentials_configuration_id)
       AND configuration.account_id = connection.account_id
     WHERE connection.connection_id = %s
"""


def read_connection_identity(bl: Any, connection_id: str) -> dict[str, Any]:
    """The connection row a send is built from, with its OAuth configuration revision."""
    if not _text(connection_id):
        return {}
    with _conn(bl).cursor() as cur:
        cur.execute(_IDENTITY_SQL, (_text(connection_id),))
        row = cur.fetchone()
        if not row:
            return {}
        if isinstance(row, dict):
            return dict(row)
        names = [column[0] for column in cur.description]
        return dict(zip(names, row, strict=False))


def _extension_for_install(bl: Any, install_id: str, account_id: str) -> dict[str, Any]:
    from core.infrastructure.extension_runtime.extension_runtime_helpers import (
        resolve_extension_runtime_record_by_install_id,
    )

    if not _text(install_id):
        return {}
    try:
        extension, _ = resolve_extension_runtime_record_by_install_id(
            _text(install_id), get_logger_only=lambda: bl, account_id=_text(account_id) or None
        )
    except Exception:
        return {}
    return _as_dict(extension)


def _eligibility(bl: Any, row: dict[str, Any]) -> tuple[str, dict[str, Any], Any]:
    """Why a connection cannot send system email (``""`` when it can)."""
    from core.application.extensions.notification_capability import (
        TARGET_KIND_RECIPIENT,
        connection_capability,
    )

    if not row:
        return REASON_CONNECTION_MISSING, {}, None
    if row.get("enabled") is not True:
        return REASON_CONNECTION_DISABLED, {}, None
    install_id = _text(row.get("extension_install_id"))
    extension = _extension_for_install(bl, install_id, _text(row.get("account_id")))
    if (
        not extension
        or not bool(extension.get("enabled"))
        or _text(extension.get("kind")) != "notification_provider"
        or _text(extension.get("extension_install_id") or extension.get("install_id")) != install_id
    ):
        return REASON_INSTALL_UNAVAILABLE, {}, None
    capability = connection_capability(extension, _text(row.get("connection_type")))
    if capability is None or capability.target_kind != TARGET_KIND_RECIPIENT:
        return REASON_NOT_RECIPIENT, extension, None
    return "", extension, capability


def _confirmation_for(row: dict[str, Any], *, source: str, actor: str) -> SenderSetting:
    from datetime import UTC, datetime

    return SenderSetting(
        connection_id=_text(row.get("connection_id")),
        extension_install_id=_text(row.get("extension_install_id")),
        connection_type=_text(row.get("connection_type")),
        sender_revision=_int(row.get("sender_revision")),
        # The OAuth configuration it signs in with: its own grant's, or the one
        # it takes its credentials from (credentials_from).
        configuration_id=_text(row.get("signing_configuration_id")),
        configuration_revision=_int(row.get("configuration_revision")),
        source=source,
        confirmed_by=actor,
        confirmed_at=datetime.now(UTC).isoformat(),
    )


def legacy_sender_choice(bl: Any) -> dict[str, Any] | None:
    """What the rule before explicit selection picked: the oldest enabled
    connection of an enabled recipient notification provider."""
    from core.application.extensions.external_effects import notification_target_kind

    if bl is None or not hasattr(bl, "list_enabled_extension_connections"):
        return None
    extensions: dict[str, dict[str, Any] | None] = {}
    for connection in bl.list_enabled_extension_connections() or []:
        extension_key = _text(connection.get("extension_id"))
        if not extension_key:
            continue
        if extension_key not in extensions:
            try:
                extensions[extension_key] = bl.get_extension(extension_key)
            except Exception:
                extensions[extension_key] = None
        extension = extensions[extension_key] or {}
        if (
            bool(extension.get("enabled"))
            and _text(extension.get("kind")) == "notification_provider"
            and notification_target_kind(extension) == "recipient"
        ):
            return dict(connection)
    return None


def _audit(bl: Any, *, action: str, actor: str, detail: dict[str, Any]) -> None:
    log_action = getattr(bl, "log_action", None)
    if not callable(log_action):
        return
    log_action(
        project_id=None,
        job_id=None,
        task_id="system_email",
        task_summary=f"System email sender {action}",
        agent=_text(actor) or "system",
        action_type=f"system_email_sender_{action}",
        status="ok",
        payload={},
        result=detail,
        meta={"audit_category": "settings"},
    )


def ensure_upgrade_pin(bl: Any) -> None:
    """Pin an install that never wrote the setting to the earlier rule's pick, once."""
    written, _ = read_setting(bl)
    if written:
        return
    chosen = legacy_sender_choice(bl)
    row = read_connection_identity(bl, _text((chosen or {}).get("connection_id")))
    setting = (
        _confirmation_for(row, source=SOURCE_UPGRADE_PIN, actor="system")
        if row
        else SenderSetting(source=SOURCE_UPGRADE_PIN)
    )
    if _insert_setting_if_absent(bl, setting):
        clear_availability_cache()
        _audit(
            bl,
            action="pinned",
            actor="system",
            detail={
                "connection_id": setting.connection_id or None,
                "extension_install_id": setting.extension_install_id or None,
                "connection_type": setting.connection_type or None,
                "reason": "upgrade_pin",
            },
        )


def sender_status(bl: Any) -> SenderStatus:
    """Read the setting and the selected connection once, and judge them."""
    ensure_upgrade_pin(bl)
    _, setting = read_setting(bl)
    if setting is None or not setting.connection_id:
        return SenderStatus(available=False, reason=REASON_NOT_SELECTED, setting=setting)
    row = read_connection_identity(bl, setting.connection_id)
    reason, extension, capability = _eligibility(bl, row)
    if reason:
        return SenderStatus(available=False, reason=reason, setting=setting, connection=row)
    if (
        _text(row.get("extension_install_id")) != setting.extension_install_id
        or _text(row.get("connection_type")) != setting.connection_type
    ):
        return SenderStatus(
            available=False, reason=REASON_IDENTITY_CHANGED, setting=setting, connection=row
        )
    configuration_revision = _int(row.get("configuration_revision"))
    if (
        _int(row.get("sender_revision")) != setting.sender_revision
        or _text(row.get("signing_configuration_id")) != setting.configuration_id
        or configuration_revision != setting.configuration_revision
    ):
        return SenderStatus(
            available=False, reason=REASON_CONFIRMATION_REVOKED, setting=setting, connection=row
        )
    return SenderStatus(
        available=True,
        setting=setting,
        connection=row,
        extension=extension,
        capability=capability,
        configuration_revision=configuration_revision,
    )


def admission_check(bl: Any, status: SenderStatus) -> str:
    """The admission point: the setting and both counters as the send started."""
    fault_point("before_admission")
    _, current = read_setting(bl)
    started = status.setting
    if started is None or not started.same_selection(current):
        return SYSTEM_SENDER_CHANGED
    row = read_connection_identity(bl, started.connection_id)
    if (
        not row
        or row.get("enabled") is not True
        or _int(row.get("sender_revision")) != _int(status.connection.get("sender_revision"))
        or _text(row.get("signing_configuration_id"))
        != _text(status.connection.get("signing_configuration_id"))
        or _int(row.get("configuration_revision")) != status.configuration_revision
    ):
        return SYSTEM_SENDER_CHANGED
    return ""


def list_candidates(bl: Any) -> list[dict[str, Any]]:
    """Every connection a platform administrator may choose, oldest first."""
    candidates: list[dict[str, Any]] = []
    projects: dict[str, dict[str, Any]] = {}
    for connection in getattr(bl, "list_enabled_extension_connections", list)() or []:
        row = read_connection_identity(bl, _text(connection.get("connection_id")))
        reason, extension, _capability = _eligibility(bl, row)
        if reason:
            continue
        project_id = _text(row.get("project_id"))
        if project_id not in projects:
            try:
                projects[project_id] = _as_dict(bl.get_project(project_id))
            except Exception:
                projects[project_id] = {}
        candidates.append(
            {
                "connection_id": _text(row.get("connection_id")),
                "display_name": _text(row.get("display_name")),
                "connection_type": _text(row.get("connection_type")),
                "project_id": project_id,
                "project_name": _text(projects[project_id].get("name")),
                "extension_key": _text(
                    extension.get("extension_key") or extension.get("extension_id")
                ),
                "extension_display_name": _text(
                    extension.get("display_name")
                    or extension.get("extension_key")
                    or extension.get("extension_id")
                ),
                "extension_install_id": _text(row.get("extension_install_id")),
            }
        )
    return candidates


def is_platform_admin(scope: Any) -> bool:
    """An installation administrator: the admin role in the installation account.

    The admin role, ``admin:all`` for a token, and the installation account as the
    acting account; an administrator of another account is refused. The rule lives
    in :func:`core.application.auth.installation_admin.is_installation_admin`.
    """
    from core.application.auth.installation_admin import is_installation_admin

    return is_installation_admin(scope)


def _require_platform_admin(scope: Any) -> None:
    from core.application.management.errors import ServiceForbiddenError

    if not is_platform_admin(scope):
        raise ServiceForbiddenError(
            "Only an installation administrator can choose the system email sender",
            payload={"error": "platform_admin_required", "code": "platform_admin_required"},
        )


def select_sender(bl: Any, scope: Any, connection_id: str) -> SenderStatus:
    """Choose and confirm one connection (or clear the choice with ``""``)."""
    from core.application.management.errors import ServiceValidationError

    _require_platform_admin(scope)
    actor = _text(getattr(scope, "actor_name", "")) or "admin"
    if not _text(connection_id):
        _save_setting(bl, SenderSetting(confirmed_by=actor), actor=actor)
        _audit(bl, action="cleared", actor=actor, detail={"connection_id": None})
        return sender_status(bl)
    row = read_connection_identity(bl, connection_id)
    reason, _extension, _capability = _eligibility(bl, row)
    if reason:
        raise ServiceValidationError(
            "This connection cannot send system email",
            payload={
                "error": "system_sender_not_eligible",
                "code": "system_sender_not_eligible",
                "reason": reason,
            },
        )
    setting = _confirmation_for(row, source=SOURCE_ADMIN, actor=actor)
    _save_setting(bl, setting, actor=actor)
    _audit(
        bl,
        action="selected",
        actor=actor,
        detail={
            "connection_id": setting.connection_id,
            "extension_install_id": setting.extension_install_id,
            "connection_type": setting.connection_type,
            "sender_revision": setting.sender_revision,
            "configuration_revision": setting.configuration_revision,
        },
    )
    return sender_status(bl)


def confirm_sender(bl: Any, scope: Any) -> SenderStatus:
    """Confirm the current selection again at its current revisions."""
    from core.application.management.errors import ServiceValidationError

    _require_platform_admin(scope)
    _, setting = read_setting(bl)
    if setting is None or not setting.connection_id:
        raise ServiceValidationError(
            "No system email sender is selected",
            payload={"error": "system_sender_not_selected", "code": "system_sender_not_selected"},
        )
    return select_sender(bl, scope, setting.connection_id)


def require_sender_change_allowed(bl: Any, scope: Any, *, connection_id: str) -> None:
    """Refuse a change to the system sender's connection unless a platform admin makes it."""
    from core.application.management.errors import ServiceForbiddenError

    if is_platform_admin(scope) or not _text(connection_id):
        return
    try:
        _, setting = read_setting(bl)
    except RuntimeError:
        # No settings storage behind this caller (a bare test double).
        return
    if setting is None or setting.connection_id != _text(connection_id):
        return
    raise ServiceForbiddenError(
        "This connection sends the platform's system email. Only a platform "
        "administrator can change its settings, secrets, account or type, or "
        "disable or delete it.",
        payload={"error": SYSTEM_SENDER_LOCKED, "code": SYSTEM_SENDER_LOCKED},
    )


def require_configuration_change_allowed(bl: Any, scope: Any, *, configuration_id: str) -> None:
    """Refuse an edit of the OAuth configuration the system sender signs in with."""
    from core.application.management.errors import ServiceForbiddenError

    if is_platform_admin(scope) or not _text(configuration_id):
        return
    try:
        _, setting = read_setting(bl)
    except RuntimeError:
        # No settings storage behind this caller (a bare test double).
        return
    if setting is None or not setting.connection_id:
        return
    row = read_connection_identity(bl, setting.connection_id)
    if _text(row.get("signing_configuration_id")) != _text(configuration_id):
        return
    raise ServiceForbiddenError(
        "The system email sender signs in with this OAuth configuration. Only a "
        "platform administrator can edit it.",
        payload={"error": SYSTEM_SENDER_LOCKED, "code": SYSTEM_SENDER_LOCKED},
    )


def is_system_sender(bl: Any, connection_id: str) -> bool:
    try:
        _, setting = read_setting(bl)
    except Exception:
        return False
    return (
        setting is not None
        and bool(setting.connection_id)
        and (setting.connection_id == _text(connection_id))
    )


def settings_view(bl: Any) -> dict[str, Any]:
    """What Settings -> Email shows."""
    status = sender_status(bl)
    setting = status.setting
    selected: dict[str, Any] | None = None
    if setting is not None and setting.connection_id:
        row = status.connection or read_connection_identity(bl, setting.connection_id)
        selected = {
            "connection_id": setting.connection_id,
            "display_name": _text(row.get("display_name")),
            "connection_type": setting.connection_type,
            "project_id": _text(row.get("project_id")),
            "extension_install_id": setting.extension_install_id,
            "source": setting.source,
            "confirmed_by": setting.confirmed_by,
            "confirmed_at": setting.confirmed_at,
        }
    return {
        "available": status.available,
        "reason": status.reason,
        "selected": selected,
        "candidates": list_candidates(bl),
    }


_AVAILABILITY_TTL_SECONDS = 30.0
_availability_lock = threading.Lock()
_availability_cache: dict[str, float | bool] = {}


def clear_availability_cache() -> None:
    with _availability_lock:
        _availability_cache.clear()


def cached_availability(bl: Any, *, clock: Callable[[], float] = time.monotonic) -> bool:
    """Whether system email can be sent; asked on every app load, kept briefly."""
    now = clock()
    with _availability_lock:
        checked_at = _availability_cache.get("at")
        if isinstance(checked_at, float) and now - checked_at < _AVAILABILITY_TTL_SECONDS:
            return bool(_availability_cache.get("available"))
    available = sender_status(bl).available
    with _availability_lock:
        _availability_cache.update({"at": float(now), "available": available})
    return available


__all__ = [
    "FAULT_POINTS",
    "SETTING_KEY",
    "SYSTEM_SENDER_CHANGED",
    "SYSTEM_SENDER_LOCKED",
    "SenderSetting",
    "SenderStatus",
    "admission_check",
    "cached_availability",
    "clear_availability_cache",
    "confirm_sender",
    "ensure_upgrade_pin",
    "is_platform_admin",
    "is_system_sender",
    "legacy_sender_choice",
    "list_candidates",
    "read_connection_identity",
    "read_setting",
    "require_configuration_change_allowed",
    "require_sender_change_allowed",
    "select_sender",
    "sender_status",
    "settings_view",
]
