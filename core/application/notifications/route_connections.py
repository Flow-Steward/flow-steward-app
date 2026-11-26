"""Check a notification route's connection against its provider's contract.

Run when a route is saved and again before each delivery. A provider whose
connection types declare a ``notification`` block is held to it:

- a route of a mixed provider, or of one with a connection-bound channel type,
  must name its connection (``notification_connection_required``); the platform
  never falls back to the project's runtime connection for such a provider;
- a named connection must exist, be enabled, belong to the route's project and to
  the provider's install, be of a type that declares ``notification``, and the
  route's address fields must fit that type
  (``notification_connection_incompatible``).

A provider that declares no block keeps its old behaviour: the connection stays
optional and is not checked here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.application.extensions.notification_capability import (
    NOTIFICATION_CONNECTION_INCOMPATIBLE,
    NOTIFICATION_CONNECTION_REQUIRED,
    NotificationCapability,
    connection_capability,
    declared_capabilities,
    provider_requires_route_connection,
    route_fields_error,
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


@dataclass(frozen=True)
class RouteConnectionDecision:
    """The outcome of checking one route; ``error`` is empty when it may proceed."""

    error: str = ""
    detail: str = ""
    capability: NotificationCapability | None = None
    #: True when the provider declares per-type notification blocks.
    governed: bool = False

    @property
    def ok(self) -> bool:
        return not self.error


_REQUIRED_DETAIL = (
    "This notification provider needs the route to name its connection: the "
    "connection decides whether the route addresses a recipient or a channel."
)


def _incompatible(detail: str) -> RouteConnectionDecision:
    return RouteConnectionDecision(
        error=NOTIFICATION_CONNECTION_INCOMPATIBLE, detail=detail, governed=True
    )


def check_route_connection(
    *,
    extension: dict[str, Any],
    extension_key: str,
    provider_install_id: str,
    project_id: str,
    route_config: dict[str, Any],
    connection: dict[str, Any] | None,
) -> RouteConnectionDecision:
    """Check one route of a provider; *connection* is the route's connection row.

    *provider_install_id* is the provider's unique visible install when there is
    one, else ``""`` (several installs: the connection's own install decides).
    """
    if not declared_capabilities(extension):
        return RouteConnectionDecision()
    config = _as_dict(route_config)
    connection_id = _text(config.get("connection_id"))
    if not connection_id:
        if provider_requires_route_connection(extension):
            return RouteConnectionDecision(
                error=NOTIFICATION_CONNECTION_REQUIRED, detail=_REQUIRED_DETAIL, governed=True
            )
        return RouteConnectionDecision(governed=True)
    row = _as_dict(connection)
    if not row or _text(row.get("connection_id")) != connection_id:
        return _incompatible(f"Connection '{connection_id}' was not found.")
    if row.get("enabled") is False:
        return _incompatible(f"Connection '{connection_id}' is disabled.")
    if _text(row.get("project_id")) != _text(project_id):
        return _incompatible(f"Connection '{connection_id}' belongs to another project.")
    if _text(row.get("extension_id")).lower() != _text(extension_key).lower():
        return _incompatible(f"Connection '{connection_id}' belongs to another extension.")
    connection_install_id = _text(row.get("extension_install_id"))
    extension_install_id = _text(
        extension.get("extension_install_id") or extension.get("install_id")
    )
    if not connection_install_id or connection_install_id != extension_install_id:
        return _incompatible(f"Connection '{connection_id}' belongs to another install.")
    if provider_install_id and connection_install_id != provider_install_id:
        return _incompatible(f"Connection '{connection_id}' belongs to another install.")
    connection_type = _text(row.get("connection_type"))
    capability = connection_capability(extension, connection_type)
    if capability is None:
        return _incompatible(
            f"Connection type '{connection_type}' cannot carry notification routes."
        )
    if route_fields_error(capability, config):
        if capability.connection_bound:
            detail = (
                "This connection is bound to one channel; the route must not name a "
                "channel or recipients."
            )
        elif capability.target_kind == "recipient":
            detail = "This connection addresses recipients; the route needs 'to' and no channel."
        else:
            detail = (
                "This connection addresses a channel; the route needs 'channel_id' and no 'to'."
            )
        return RouteConnectionDecision(
            error=NOTIFICATION_CONNECTION_INCOMPATIBLE,
            detail=detail,
            capability=capability,
            governed=True,
        )
    return RouteConnectionDecision(capability=capability, governed=True)


def provider_install_id(bl: Any, *, extension_key: str, account_id: str) -> str:
    """The provider's unique visible install, or ``""`` when there is none or several."""
    from core.application.extensions.notification_install_binding import (
        select_notification_install,
    )

    try:
        selected = select_notification_install(
            getattr(bl, "conn", None), extension_id=extension_key, account_id=account_id
        )
    except (RuntimeError, ValueError):
        return ""
    return _text(selected.install_id)


def load_route_connection(bl: Any, connection_id: str) -> dict[str, Any]:
    """The route's connection row as stored, enabled or not."""
    normalized = _text(connection_id)
    if not normalized or bl is None:
        return {}
    getter = getattr(bl, "get_project_connection", None)
    if callable(getter):
        try:
            row = getter(connection_id=normalized, enabled_only=False)
        except TypeError:
            row = None
        if isinstance(row, dict):
            return dict(row)
    if callable(getattr(bl, "cursor", None)):
        from core.infrastructure.db import project_connection_store

        return _as_dict(project_connection_store.get_project_connection_row(bl, normalized))
    return {}


def validate_routes_for_save(
    bl: Any, *, project_id: str, account_id: str, routes: list[dict[str, Any]]
) -> list[tuple[int, RouteConnectionDecision]]:
    """Every route that may not be saved, with its index and the reason."""
    from core.application.notifications.delivery import (
        _notification_extension_key,
        _resolve_notification_extension,
    )

    refused: list[tuple[int, RouteConnectionDecision]] = []
    extensions: dict[tuple[str, str], dict[str, Any] | None] = {}
    for idx, route in enumerate(routes):
        provider_key = _text(route.get("provider_key"))
        if not provider_key:
            continue
        provider = _as_dict(bl.get_provider(provider_key)) if hasattr(bl, "get_provider") else {}
        provider = provider or {"provider_key": provider_key}
        try:
            extension_key = _notification_extension_key(
                {**provider, "provider_config": _as_dict(provider.get("config"))}
            )
        except ValueError:
            continue
        config = _as_dict(route.get("route_config"))
        connection = load_route_connection(bl, _text(config.get("connection_id")))
        provider_ref = {
            "provider_key": provider_key,
            "provider_config": {"extension_key": extension_key},
        }
        extension = None
        # The connection's own install first (several installs of one extension
        # are told apart by it); then the provider's unique install, so that a
        # connection of another extension is still judged by this provider.
        for install_id in dict.fromkeys([_text(connection.get("extension_install_id")), ""]):
            cache_key = (extension_key, install_id)
            if cache_key not in extensions:
                try:
                    extensions[cache_key] = _resolve_notification_extension(
                        bl, provider_ref, extension_install_id=install_id, account_id=account_id
                    )
                except Exception:
                    extensions[cache_key] = None
            extension = extensions[cache_key]
            if extension is not None:
                break
        if extension is None or not declared_capabilities(extension):
            # Not an installed notification extension, or one that declares no
            # per-type blocks: such routes are saved the way they always were.
            continue
        decision = check_route_connection(
            extension=extension,
            extension_key=extension_key,
            provider_install_id=provider_install_id(
                bl, extension_key=extension_key, account_id=account_id
            ),
            project_id=project_id,
            route_config=config,
            connection=connection,
        )
        if not decision.ok:
            refused.append((idx, decision))
    return refused


__all__ = [
    "RouteConnectionDecision",
    "check_route_connection",
    "load_route_connection",
    "provider_install_id",
    "validate_routes_for_save",
]
