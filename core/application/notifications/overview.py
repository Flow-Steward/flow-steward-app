"""Read-only overview of the notifications a project sends on its own.

The overview is computed from the live settings every time it is asked for:
the Automation rules of the project's linked workflows whose action sends a
notification (workflow, group and step scope), and the project's own event
subscriptions (notification routes). It keeps no copy of either and writes
nothing. Message-sending steps inside a workflow are not automatic
notifications and never appear here; neither does a connected extension that no
rule or subscription uses.

Each entry says where it is configured so the UI can open that exact place.
A rule or subscription that cannot deliver as configured is reported as a setup
problem; an entry without one is only configured, never confirmed delivered.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from core.application.notifications.contracts import (
    _as_dict,
    _as_str,
    _event_type_matches,
    _severity_allows,
)
from core.application.workflows.reaction_runtime import normalize_reaction_route_keys
from core.application.workflows.reaction_scopes import (
    is_supported_reaction_scope,
    split_scoped_reaction_key,
)

ENTRY_AUTOMATION = "automation"
ENTRY_SUBSCRIPTION = "subscription"

STATE_ENABLED = "enabled"
STATE_DISABLED = "disabled"
STATE_SETUP_PROBLEM = "setup_problem"

#: What a notify rule addresses; mirrors the runtime in ``reaction_runtime``.
TARGET_MATCHING_SUBSCRIPTIONS = "matching_subscriptions"
TARGET_SUBSCRIPTIONS = "subscriptions"
TARGET_CHANNEL = "channel"
TARGET_RECIPIENTS = "recipients"

PROBLEM_EXTENSION_MISSING = "extension_missing"
PROBLEM_EXTENSION_DISABLED = "extension_disabled"
PROBLEM_CONNECTION_DISABLED = "connection_disabled"
PROBLEM_CONNECTION_MISSING = "connection_missing"
PROBLEM_DESTINATION_MISSING = "destination_missing"
PROBLEM_SUBSCRIPTION_MISSING = "subscription_missing"
PROBLEM_NO_MATCHING_SUBSCRIPTION = "no_matching_subscription"
PROBLEM_SCOPE_RETIRED = "scope_retired"

#: Problems an extension or project connection causes; the UI links them to
#: the extension's settings rather than to the rule.
CONNECTION_PROBLEMS = frozenset(
    {
        PROBLEM_EXTENSION_MISSING,
        PROBLEM_EXTENSION_DISABLED,
        PROBLEM_CONNECTION_DISABLED,
        PROBLEM_CONNECTION_MISSING,
    }
)

_DEFAULT_NOTIFY_EVENT = "workflow.automation.notify"
_TEMPLATE_MARKER = "{{"


def _rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(row) for row in value if isinstance(row, Mapping)]


def _recipients(value: Any) -> list[str]:
    """The addresses a rule names, read the way ``reaction_runtime`` reads them."""
    items = value if isinstance(value, list) else str(value or "").split(",")
    return [text for item in items if (text := str(item or "").strip())][:20]


def _decided_at_run_time(*values: str) -> bool:
    return any(_TEMPLATE_MARKER in value for value in values)


class _ExtensionAvailability:
    """Whether an extension and its project connections can deliver.

    An unknown inventory (a store without the listing) reports nothing rather
    than a problem it cannot prove.
    """

    def __init__(self, bl: Any, project_id: str) -> None:
        extensions = (
            bl.list_extensions(enabled_only=False) if hasattr(bl, "list_extensions") else None
        )
        self._extensions: dict[str, bool] | None = None
        if isinstance(extensions, list):
            self._extensions = {}
            for row in _rows(extensions):
                key = _as_str(row.get("extension_key") or row.get("extension_id"))
                if key:
                    self._extensions[key] = self._extensions.get(key, False) or bool(
                        row.get("enabled")
                    )
        connections = (
            bl.list_project_connections(project_id, enabled_only=False)
            if hasattr(bl, "list_project_connections")
            else None
        )
        self._connections: list[dict[str, Any]] | None = (
            _rows(connections) if isinstance(connections, list) else None
        )

    def problems(self, extension_id: str, connection_id: str = "") -> list[str]:
        if not extension_id:
            return []
        problems: list[str] = []
        if self._extensions is not None:
            if extension_id not in self._extensions:
                problems.append(PROBLEM_EXTENSION_MISSING)
            elif not self._extensions[extension_id]:
                problems.append(PROBLEM_EXTENSION_DISABLED)
        if self._connections is not None:
            if connection_id:
                selected = [
                    row
                    for row in self._connections
                    if _as_str(row.get("connection_id")) == connection_id
                ]
                if not selected:
                    problems.append(PROBLEM_CONNECTION_MISSING)
                elif not any(row.get("enabled") is not False for row in selected):
                    problems.append(PROBLEM_CONNECTION_DISABLED)
            else:
                own = [
                    row
                    for row in self._connections
                    if _as_str(row.get("extension_id")) == extension_id
                ]
                # Without a connection delivery uses the single visible install,
                # so only connections that exist and are all switched off count.
                if own and not any(row.get("enabled") is not False for row in own):
                    problems.append(PROBLEM_CONNECTION_DISABLED)
        return problems


def _route_extension_id(route: Mapping[str, Any]) -> str:
    provider_config = _as_dict(route.get("provider_config"))
    return _as_str(
        provider_config.get("extension_key")
        or route.get("integration_key")
        or route.get("provider_key")
    )


def _route_destination(route: Mapping[str, Any]) -> dict[str, Any]:
    config = _as_dict(route.get("route_config"))
    channel_id = _as_str(config.get("channel_id"))
    recipients = _recipients(config.get("to"))
    method = TARGET_RECIPIENTS if recipients and not channel_id else TARGET_CHANNEL
    return {
        "method": method,
        "provider_key": _as_str(route.get("provider_key")),
        "provider_name": _as_str(route.get("provider_display_name"))
        or _as_str(route.get("provider_key")),
        "channel_id": channel_id,
        "recipients": recipients,
        "subscriptions": [],
        "decided_at_run_time": _decided_at_run_time(channel_id, *recipients),
    }


def _route_name(route: Mapping[str, Any]) -> str:
    return _as_str(route.get("display_name")) or _as_str(route.get("route_key"))


def _route_problems(route: Mapping[str, Any], availability: _ExtensionAvailability) -> list[str]:
    config = _as_dict(route.get("route_config"))
    problems = availability.problems(
        _route_extension_id(route), _as_str(config.get("connection_id"))
    )
    if not _as_str(config.get("channel_id")) and not _recipients(config.get("to")):
        problems.append(PROBLEM_DESTINATION_MISSING)
    return problems


def _state(enabled: bool, problems: list[str]) -> str:
    if not enabled:
        return STATE_DISABLED
    return STATE_SETUP_PROBLEM if problems else STATE_ENABLED


def _connection_target(extension_id: str, problems: list[str]) -> dict[str, str] | None:
    if extension_id and any(problem in CONNECTION_PROBLEMS for problem in problems):
        return {"extension_id": extension_id}
    return None


def _subscription_entry(
    route: Mapping[str, Any],
    *,
    workflow_names: Mapping[str, str],
    availability: _ExtensionAvailability,
) -> dict[str, Any]:
    route_key = _as_str(route.get("route_key"))
    workflow_id = _as_str(route.get("workflow_id"))
    problems = _route_problems(route, availability)
    enabled = bool(route.get("enabled", True))
    severity = _as_str(route.get("severity")) or "any"
    return {
        "entry_id": f"{ENTRY_SUBSCRIPTION}:{route_key}",
        "kind": ENTRY_SUBSCRIPTION,
        "name": _route_name(route),
        "source": {
            "scope_kind": "project",
            "workflow_id": "",
            "workflow_name": "",
            "scope_id": "",
            "scope_name": "",
        },
        "event_type": _as_str(route.get("event_type")),
        "conditions": {
            "severity": "" if severity == "any" else severity,
            "workflow_id": workflow_id,
            "workflow_name": workflow_names.get(workflow_id, workflow_id) if workflow_id else "",
            "fields": [],
            "debounce_seconds": 0,
        },
        "delivery": _route_destination(route),
        "enabled": enabled,
        "state": _state(enabled, problems),
        "problems": problems,
        "settings": {"kind": ENTRY_SUBSCRIPTION, "route_key": route_key},
        "connection": _connection_target(_route_extension_id(route), problems),
    }


def _condition_fields(conditions: Mapping[str, Any]) -> list[dict[str, str]]:
    fields: list[dict[str, str]] = []
    for key, value in conditions.items():
        name = _as_str(key)
        if not name or name == "source":
            continue
        if isinstance(value, (list, tuple, set)):
            text = ", ".join(_as_str(item) for item in value)
        elif isinstance(value, Mapping):
            text = ", ".join(f"{_as_str(k)}={_as_str(v)}" for k, v in value.items())
        else:
            text = _as_str(value) if not isinstance(value, bool) else str(value).lower()
        fields.append({"field": name, "value": text})
    return fields


class _AutomationContext:
    def __init__(
        self,
        *,
        routes: list[dict[str, Any]],
        availability: _ExtensionAvailability,
    ) -> None:
        self.routes = routes
        self.routes_by_key = {
            _as_str(route.get("route_key")).lower(): route
            for route in routes
            if _as_str(route.get("route_key"))
        }
        self.availability = availability


def _rule_delivery(
    action: Mapping[str, Any],
    *,
    rule_event_type: str,
    workflow_id: str,
    context: _AutomationContext,
) -> tuple[dict[str, Any], list[str], str]:
    """Where a notify rule delivers, its problems, and the extension it uses.

    Follows ``dispatch_notification_from_reaction``: named route keys win, then
    a direct extension destination, and otherwise every enabled project
    subscription that matches the event at run time.
    """
    target = _as_str(action.get("target")).lower()
    route_keys = normalize_reaction_route_keys(
        action.get("route_keys") or action.get("route_key") or ""
    )
    severity = _as_str(action.get("severity")) or "info"
    delivery: dict[str, Any] = {
        "method": TARGET_MATCHING_SUBSCRIPTIONS,
        "provider_key": "",
        "provider_name": "",
        "channel_id": "",
        "recipients": [],
        "subscriptions": [],
        "decided_at_run_time": False,
    }
    problems: list[str] = []
    if route_keys:
        delivery["method"] = TARGET_SUBSCRIPTIONS
        for key in route_keys:
            route = context.routes_by_key.get(key.lower())
            if route is None:
                problems.append(PROBLEM_SUBSCRIPTION_MISSING)
                delivery["subscriptions"].append({"route_key": key, "name": key, "found": False})
                continue
            delivery["subscriptions"].append(
                {
                    "route_key": _as_str(route.get("route_key")),
                    "name": _route_name(route),
                    "found": True,
                }
            )
            problems.extend(_route_problems(route, context.availability))
        return delivery, list(dict.fromkeys(problems)), ""
    if target == "extension_channel":
        provider_key = _as_str(action.get("provider_key"))
        channel_id = _as_str(action.get("channel_id"))
        recipients = _recipients(action.get("recipients"))
        delivery.update(
            method=TARGET_CHANNEL if channel_id or not recipients else TARGET_RECIPIENTS,
            provider_key=provider_key,
            provider_name=provider_key,
            channel_id=channel_id,
            recipients=recipients,
            decided_at_run_time=_decided_at_run_time(channel_id, *recipients),
        )
        if not provider_key or (not channel_id and not recipients):
            problems.append(PROBLEM_DESTINATION_MISSING)
        problems.extend(context.availability.problems(provider_key))
        return delivery, list(dict.fromkeys(problems)), provider_key
    # Every enabled subscription whose filters match the dispatched event.
    event_type = _as_str(action.get("event_type")) or rule_event_type or _DEFAULT_NOTIFY_EVENT
    delivery["decided_at_run_time"] = True
    for route in context.routes:
        if not bool(route.get("enabled", True)):
            continue
        route_workflow = _as_str(route.get("workflow_id"))
        if route_workflow and route_workflow != workflow_id:
            continue
        if not _event_type_matches(_as_str(route.get("event_type")), event_type):
            continue
        if not _severity_allows(_as_str(route.get("severity")), severity):
            continue
        delivery["subscriptions"].append(
            {
                "route_key": _as_str(route.get("route_key")),
                "name": _route_name(route),
                "found": True,
            }
        )
    if not delivery["subscriptions"]:
        problems.append(PROBLEM_NO_MATCHING_SUBSCRIPTION)
    return delivery, problems, ""


def _automation_entry(
    rule: Mapping[str, Any],
    *,
    workflow_id: str,
    workflow_name: str,
    scope_names: Mapping[str, Mapping[str, str]],
    context: _AutomationContext,
) -> dict[str, Any] | None:
    action = _as_dict(rule.get("action"))
    if _as_str(action.get("type")).lower() != "notify":
        return None
    stored_key = _as_str(rule.get("reaction_key"))
    scope_kind, scope_id, base_key = split_scoped_reaction_key(stored_key)
    event_type = _as_str(rule.get("event_type"))
    delivery, problems, extension_id = _rule_delivery(
        action, rule_event_type=event_type, workflow_id=workflow_id, context=context
    )
    if not is_supported_reaction_scope(scope_kind):
        problems = [PROBLEM_SCOPE_RETIRED, *problems]
    severity = _as_str(action.get("severity")) or "info"
    enabled = bool(rule.get("enabled", True))
    scope_name = _as_str(_as_dict(scope_names.get(scope_kind)).get(scope_id)) or scope_id
    return {
        "entry_id": f"{ENTRY_AUTOMATION}:{workflow_id}:{stored_key}",
        "kind": ENTRY_AUTOMATION,
        "name": _as_str(rule.get("display_name")) or base_key,
        "source": {
            "scope_kind": scope_kind,
            "workflow_id": workflow_id,
            "workflow_name": workflow_name,
            "scope_id": scope_id,
            "scope_name": scope_name,
        },
        "event_type": event_type,
        "conditions": {
            "severity": severity,
            "workflow_id": "",
            "workflow_name": "",
            "fields": _condition_fields(_as_dict(rule.get("conditions"))),
            "debounce_seconds": max(0, int(rule.get("debounce_seconds") or 0)),
        },
        "delivery": delivery,
        "enabled": enabled,
        "state": _state(enabled, problems),
        "problems": problems,
        "settings": {
            "kind": ENTRY_AUTOMATION,
            "workflow_id": workflow_id,
            "scope_kind": scope_kind,
            "scope_id": scope_id,
            "rule_key": base_key,
        },
        "connection": _connection_target(extension_id, problems),
    }


def build_project_notification_overview(
    bl: Any,
    *,
    project_id: str,
    account_id: str | None,
    workflows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """The project's automatic notifications, Automation rules first.

    ``workflows`` are the project's linked workflows the caller may read; their
    Automation rules are read with ``account_id``, exactly as the Automation
    editor reads them.
    """
    normalized_project_id = _as_str(project_id)
    routes = _rows(
        bl.list_notification_routes(normalized_project_id, enabled_only=False)
        if hasattr(bl, "list_notification_routes")
        else []
    )
    availability = _ExtensionAvailability(bl, normalized_project_id)
    context = _AutomationContext(routes=routes, availability=availability)
    workflow_rows = [dict(row) for row in workflows if isinstance(row, Mapping)]
    workflow_names = {
        _as_str(row.get("workflow_id")): _as_str(
            row.get("workflow_display_name") or row.get("display_name")
        )
        or _as_str(row.get("workflow_id"))
        for row in workflow_rows
        if _as_str(row.get("workflow_id"))
    }

    entries: list[dict[str, Any]] = []
    for workflow_id, workflow_name in workflow_names.items():
        rules = _rows(
            bl.list_workflow_reactions(workflow_id, enabled_only=False, account_id=account_id)
            if hasattr(bl, "list_workflow_reactions")
            else []
        )
        notify_rules = [
            rule
            for rule in rules
            if _as_str(_as_dict(rule.get("action")).get("type")).lower() == "notify"
        ]
        if not notify_rules:
            continue
        scope_names = (
            bl.list_workflow_reaction_scope_names(workflow_id)
            if hasattr(bl, "list_workflow_reaction_scope_names")
            else {}
        )
        scope_names = scope_names if isinstance(scope_names, Mapping) else {}
        for rule in notify_rules:
            entry = _automation_entry(
                rule,
                workflow_id=workflow_id,
                workflow_name=workflow_name,
                scope_names=scope_names,
                context=context,
            )
            if entry is not None:
                entries.append(entry)

    entries.extend(
        _subscription_entry(route, workflow_names=workflow_names, availability=availability)
        for route in routes
        if _as_str(route.get("route_key"))
    )
    return entries


__all__ = [
    "CONNECTION_PROBLEMS",
    "ENTRY_AUTOMATION",
    "ENTRY_SUBSCRIPTION",
    "STATE_DISABLED",
    "STATE_ENABLED",
    "STATE_SETUP_PROBLEM",
    "build_project_notification_overview",
]
