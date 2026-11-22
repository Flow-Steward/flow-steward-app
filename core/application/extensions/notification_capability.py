"""What one notification connection addresses, read from the extension's contract.

A notification provider delivers through its project connections. By default
every connection addresses the same kind of target, declared once on the
provider's ``deliver_notification`` external effect (``channel`` and
``observability.target_kind``). A connection type may override that with its own
block::

    notification:
      target_kind: recipient       # or channel
      ledger_channel: email        # external-effect ledger channel for this type
      channel_source: route        # channel types only: route (default) or connection

Once one type of an extension declares a block, only the types that declare one
can carry routes or send system email. An extension whose declared types address
more than one kind of target is *mixed*: each route must then name its
connection, because the connection decides what the route addresses. A
``channel_source: connection`` type is bound to one channel by its own
configuration, so its routes carry no ``channel_id`` and the editor shows the
display-only ``channel_label`` from the connection config instead of a picker.

The platform never knows which extension sends mail and which posts to a chat:
everything here is read from the extension's own typed declaration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.application.extensions.external_effect_contracts import (
    DEFAULT_NOTIFICATION_TARGET_KIND,
    ExternalEffectDeclarationInvalidError,
    external_effect_declaration_for_operation,
    notification_target_kind,
)
from core.application.extensions.oauth_contract import contract_section

TARGET_KIND_RECIPIENT = "recipient"
TARGET_KIND_CHANNEL = "channel"
TARGET_KINDS = frozenset({TARGET_KIND_RECIPIENT, TARGET_KIND_CHANNEL})
#: The provider-level target kind of an extension whose types disagree.
TARGET_KIND_MIXED = "mixed"

CHANNEL_SOURCE_ROUTE = "route"
CHANNEL_SOURCE_CONNECTION = "connection"
CHANNEL_SOURCES = frozenset({CHANNEL_SOURCE_ROUTE, CHANNEL_SOURCE_CONNECTION})

NOTIFICATION_BLOCK_KEYS = frozenset({"target_kind", "ledger_channel", "channel_source"})

#: A route of a provider that needs one names no connection.
NOTIFICATION_CONNECTION_REQUIRED = "notification_connection_required"
#: The route's connection cannot carry this route.
NOTIFICATION_CONNECTION_INCOMPATIBLE = "notification_connection_incompatible"

#: The display-only label of a connection bound to one channel.
CHANNEL_LABEL_CONFIG_KEY = "channel_label"

_DELIVERY_OPERATION_ID = "deliver_notification"
_DEFAULT_LEDGER_CHANNEL = "generic"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


@dataclass(frozen=True)
class NotificationCapability:
    """What deliveries through one connection type address."""

    target_kind: str
    ledger_channel: str
    channel_source: str = CHANNEL_SOURCE_ROUTE
    #: True when a connection type's own ``notification`` block declared it.
    declared: bool = False

    @property
    def connection_bound(self) -> bool:
        return (
            self.target_kind == TARGET_KIND_CHANNEL
            and self.channel_source == CHANNEL_SOURCE_CONNECTION
        )

    def as_view(self) -> dict[str, str]:
        return {"target_kind": self.target_kind, "channel_source": self.channel_source}


def connection_type_rows(extension: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The extension's connection types, wherever its descriptor carries them."""
    payload = _as_dict(extension)
    for descriptor in (payload, _as_dict(payload.get("compiled_manifest"))):
        rows = contract_section(descriptor, "connection_types")
        if rows:
            return rows
    return []


def _delivery_effect(extension: dict[str, Any] | None) -> dict[str, Any]:
    try:
        return external_effect_declaration_for_operation(extension, _DELIVERY_OPERATION_ID)
    except ExternalEffectDeclarationInvalidError:
        return {}


def default_capability(extension: dict[str, Any] | None) -> NotificationCapability:
    """What the provider's single delivery effect declares for every connection."""
    effect = _delivery_effect(extension)
    target_kind = _text(_as_dict(effect.get("observability")).get("target_kind"))
    if target_kind not in TARGET_KINDS:
        target_kind = notification_target_kind(extension) or DEFAULT_NOTIFICATION_TARGET_KIND
    return NotificationCapability(
        target_kind=target_kind,
        ledger_channel=_text(effect.get("channel")) or _DEFAULT_LEDGER_CHANNEL,
    )


def _capability_from_block(
    block: dict[str, Any], *, default: NotificationCapability
) -> NotificationCapability | None:
    target_kind = _text(block.get("target_kind"))
    if target_kind not in TARGET_KINDS:
        return None
    channel_source = _text(block.get("channel_source")) or CHANNEL_SOURCE_ROUTE
    if channel_source not in CHANNEL_SOURCES:
        return None
    if target_kind != TARGET_KIND_CHANNEL:
        channel_source = CHANNEL_SOURCE_ROUTE
    return NotificationCapability(
        target_kind=target_kind,
        ledger_channel=_text(block.get("ledger_channel")) or default.ledger_channel,
        channel_source=channel_source,
        declared=True,
    )


def declared_capabilities(extension: dict[str, Any] | None) -> dict[str, NotificationCapability]:
    """Every connection type that declares a ``notification`` block, by type id."""
    default = default_capability(extension)
    declared: dict[str, NotificationCapability] = {}
    for row in connection_type_rows(extension):
        type_id = _text(row.get("connection_type_id"))
        block = row.get("notification")
        if not type_id or not isinstance(block, dict):
            continue
        capability = _capability_from_block(block, default=default)
        if capability is not None:
            declared[type_id] = capability
    return declared


def connection_capability(
    extension: dict[str, Any] | None, connection_type: str
) -> NotificationCapability | None:
    """What deliveries through a connection of this type address, or None.

    None means the type cannot carry notification routes: the extension declares
    ``notification`` on some of its types and not on this one.
    """
    declared = declared_capabilities(extension)
    if declared:
        return declared.get(_text(connection_type))
    return default_capability(extension)


def provider_target_kind(extension: dict[str, Any] | None) -> str:
    """``recipient``, ``channel``, or ``mixed`` when the declared types disagree."""
    declared = declared_capabilities(extension)
    kinds = {capability.target_kind for capability in declared.values()}
    if not kinds:
        return default_capability(extension).target_kind
    if len(kinds) == 1:
        return kinds.pop()
    return TARGET_KIND_MIXED


def provider_requires_route_connection(extension: dict[str, Any] | None) -> bool:
    """Whether every route of this provider must name its connection.

    True for a mixed provider and for one with a connection-bound channel type:
    without a named connection the platform would fall back to the project's
    runtime connection, and a connection-bound route could reach another channel.
    """
    declared = declared_capabilities(extension)
    if len({capability.target_kind for capability in declared.values()}) > 1:
        return True
    return any(capability.connection_bound for capability in declared.values())


def _has_value(value: Any) -> bool:
    if isinstance(value, list | tuple):
        return any(_text(item) for item in value)
    return bool(_text(value))


#: A route's channel field; ``channel`` is accepted as an alias.
ROUTE_CHANNEL_FIELDS = ("channel_id", "channel")


def route_has_channel(route_config: dict[str, Any] | None) -> bool:
    config = _as_dict(route_config)
    return any(_has_value(config.get(field_name)) for field_name in ROUTE_CHANNEL_FIELDS)


def route_fields_error(capability: NotificationCapability, route_config: dict[str, Any]) -> str:
    """Why a route's address fields do not fit its connection type, or ``""``.

    Only types that declare a ``notification`` block are held to their fields;
    a provider that declares none keeps the behaviour it always had.
    """
    if not capability.declared:
        return ""
    config = _as_dict(route_config)
    has_to = _has_value(config.get("to"))
    has_channel = route_has_channel(config)
    if capability.target_kind == TARGET_KIND_RECIPIENT:
        if not has_to or has_channel:
            return NOTIFICATION_CONNECTION_INCOMPATIBLE
        return ""
    if has_to:
        return NOTIFICATION_CONNECTION_INCOMPATIBLE
    if capability.connection_bound:
        return NOTIFICATION_CONNECTION_INCOMPATIBLE if has_channel else ""
    return "" if has_channel else NOTIFICATION_CONNECTION_INCOMPATIBLE


def channel_label(connection: dict[str, Any] | None) -> str:
    config = _as_dict(_as_dict(connection).get("connection_config")) or _as_dict(
        _as_dict(connection).get("config")
    )
    return _text(config.get(CHANNEL_LABEL_CONFIG_KEY))


def notification_connection_view(
    extension: dict[str, Any] | None, connection: dict[str, Any] | None
) -> dict[str, Any]:
    """What a provider listing shows about one notification connection.

    ``notification_target_kind`` is the provider-level kind (``mixed`` for a
    mixed provider); the other fields are this connection's own.
    """
    row = _as_dict(connection)
    capability = connection_capability(extension, _text(row.get("connection_type")))
    return {
        "notification_target_kind": provider_target_kind(extension),
        "requires_connection": provider_requires_route_connection(extension),
        "connection_type": _text(row.get("connection_type")),
        "connection_display_name": _text(row.get("display_name")),
        "notification_eligible": capability is not None,
        "target_kind": capability.target_kind if capability else "",
        "channel_source": capability.channel_source if capability else "",
        "channel_label": channel_label(row) if capability and capability.connection_bound else "",
    }


def group_provider_connections(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give every notification-provider entry the eligible connections of its provider."""
    by_provider: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        if entry.get("notification_eligible") is not True:
            continue
        by_provider.setdefault(_text(entry.get("provider_key")), []).append(
            {
                "connection_id": _text(entry.get("connection_id")),
                "display_name": _text(entry.get("connection_display_name")),
                "connection_type": _text(entry.get("connection_type")),
                "target_kind": _text(entry.get("target_kind")),
                "channel_source": _text(entry.get("channel_source")),
                "channel_label": _text(entry.get("channel_label")),
                "enabled": bool(entry.get("enabled", True)),
            }
        )
    for entry in entries:
        if "notification_eligible" in entry:
            entry["connections"] = list(by_provider.get(_text(entry.get("provider_key")), []))
    return entries


__all__ = [
    "CHANNEL_LABEL_CONFIG_KEY",
    "CHANNEL_SOURCES",
    "CHANNEL_SOURCE_CONNECTION",
    "CHANNEL_SOURCE_ROUTE",
    "NOTIFICATION_BLOCK_KEYS",
    "NOTIFICATION_CONNECTION_INCOMPATIBLE",
    "NOTIFICATION_CONNECTION_REQUIRED",
    "ROUTE_CHANNEL_FIELDS",
    "TARGET_KINDS",
    "TARGET_KIND_CHANNEL",
    "TARGET_KIND_MIXED",
    "TARGET_KIND_RECIPIENT",
    "NotificationCapability",
    "channel_label",
    "connection_capability",
    "connection_type_rows",
    "declared_capabilities",
    "default_capability",
    "group_provider_connections",
    "notification_connection_view",
    "provider_requires_route_connection",
    "provider_target_kind",
    "route_fields_error",
    "route_has_channel",
]
