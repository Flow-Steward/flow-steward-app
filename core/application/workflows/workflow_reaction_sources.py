from __future__ import annotations

from typing import Any

from core.application.workflows.reaction_scopes import normalize_reaction_scope
from core.application.workflows.workflow_reaction_events import (
    builtin_reaction_events,
    removed_reaction_event_message,
)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_str(value: Any) -> str:
    return str(value or "").strip()


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    normalized = _as_str(value).lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _humanize_event_name(value: str) -> str:
    text = _as_str(value).replace(".", " ").replace("_", " ").strip().lower()
    return text.capitalize() if text else ""


def _normalize_unique_event_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        event_type = _as_str(row.get("event_type"))
        if not event_type or event_type in seen:
            continue
        seen.add(event_type)
        payload = dict(row)
        payload["event_type"] = event_type
        payload["display_name"] = _as_str(row.get("display_name")) or _humanize_event_name(
            event_type
        )
        deduped.append(payload)
    return deduped


def builtin_workflow_reaction_event_options(scope_kind: str = "workflow") -> list[dict[str, Any]]:
    normalized_scope, _ = normalize_reaction_scope(scope_kind, None)
    return [dict(row) for row in builtin_reaction_events(normalized_scope)]


def extension_workflow_reaction_event_options(
    extension: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    payload = _as_dict(extension)
    compiled_manifest = _as_dict(payload.get("compiled_manifest"))
    rows: list[dict[str, Any]] = []

    for binding in _as_list(
        compiled_manifest.get("event_bindings") or payload.get("event_bindings")
    ):
        binding_payload = _as_dict(binding)
        event_type = _as_str(binding_payload.get("event_name") or binding_payload.get("event_type"))
        if not event_type:
            continue
        rows.append(
            {
                "event_type": event_type,
                "display_name": _as_str(binding_payload.get("display_name"))
                or _humanize_event_name(event_type),
                "trigger_id": _as_str(binding_payload.get("trigger_id")),
            }
        )

    supported_events = (
        compiled_manifest.get("supported_events") or payload.get("supported_events") or []
    )
    for item in supported_events:
        event_type = _as_str(item)
        if not event_type:
            continue
        rows.append(
            {
                "event_type": event_type,
                "display_name": _humanize_event_name(event_type),
            }
        )

    return _normalize_unique_event_rows(rows)


def build_workflow_reaction_trigger_sources(
    bl: Any,
    *,
    project_id: str | None = None,
    scope_kind: str = "workflow",
) -> list[dict[str, Any]]:
    normalized_scope, _ = normalize_reaction_scope(scope_kind, None)
    sources: list[dict[str, Any]] = [
        {
            "source_id": "__workflow__",
            "source_kind": "workflow",
            "display_name": "Workflow runtime",
            "display_name_key": "react.workflows.workflow_runtime",
            "enabled": True,
            "event_count": len(builtin_workflow_reaction_event_options(normalized_scope)),
            "events": builtin_workflow_reaction_event_options(normalized_scope),
        }
    ]
    if normalized_scope != "workflow" or not str(project_id or "").strip():
        return sources
    if not hasattr(bl, "list_project_connections") or not hasattr(bl, "get_extension"):
        return sources

    seen_extensions: set[str] = set()
    connection_rows = bl.list_project_connections(str(project_id), enabled_only=True) or []
    for row in connection_rows:
        payload = _as_dict(row)
        if not _coerce_bool(payload.get("enabled"), True):
            continue
        extension_id = _as_str(payload.get("extension_id"))
        if not extension_id or extension_id in seen_extensions:
            continue
        seen_extensions.add(extension_id)
        extension = _as_dict(bl.get_extension(extension_id))
        if not extension or not _coerce_bool(extension.get("enabled"), True):
            continue
        events = extension_workflow_reaction_event_options(extension)
        sources.append(
            {
                "source_id": extension_id,
                "source_kind": "extension",
                "display_name": _as_str(extension.get("display_name")) or extension_id,
                "enabled": True,
                "event_count": len(events),
                "connection_id": _as_str(payload.get("connection_id")),
                "events": events,
            }
        )
    return sources


def reaction_rule_source_id(rule: dict[str, Any] | None) -> str:
    conditions = _as_dict(_as_dict(rule).get("conditions"))
    raw_source = conditions.get("source")
    if raw_source is None:
        return ""
    if isinstance(raw_source, (list, tuple, set)):
        values = [_as_str(item) for item in raw_source if _as_str(item)]
        if not values:
            return ""
        if len(values) != 1:
            raise ValueError("Reaction trigger source must reference exactly one source")
        return values[0]
    return _as_str(raw_source)


def validate_workflow_reaction_rules(
    rules: list[dict[str, Any]] | list[Any],
    *,
    available_sources: list[dict[str, Any]],
    scope_kind: str = "workflow",
) -> None:
    normalized_scope, _ = normalize_reaction_scope(scope_kind, None)
    source_map = {
        _as_str(row.get("source_id")): _as_dict(row)
        for row in (available_sources or [])
        if _as_str(_as_dict(row).get("source_id"))
    }
    workflow_source = source_map.get("__workflow__", {})
    workflow_events = {
        _as_str(item.get("event_type"))
        for item in _as_list(workflow_source.get("events"))
        if _as_str(_as_dict(item).get("event_type"))
    }
    extension_sources = {
        source_id: row
        for source_id, row in source_map.items()
        if source_id != "__workflow__" and _as_str(row.get("source_kind")) == "extension"
    }
    extension_events = {
        _as_str(item.get("event_type"))
        for row in extension_sources.values()
        for item in _as_list(row.get("events"))
        if _as_str(_as_dict(item).get("event_type"))
    }

    for index, rule in enumerate(rules or []):
        payload = _as_dict(rule)
        event_type = _as_str(payload.get("event_type"))
        if not event_type:
            raise ValueError(f"Reaction #{index + 1} is missing event_type")
        removed = removed_reaction_event_message(event_type)
        if removed:
            raise ValueError(removed)

        source_id = reaction_rule_source_id(payload)
        if source_id == "__workflow__":
            source_id = ""

        if source_id:
            source = extension_sources.get(source_id)
            if not source:
                raise ValueError(
                    f"Trigger source '{source_id}' is not enabled for the selected project"
                )
            source_events = {
                _as_str(item.get("event_type"))
                for item in _as_list(source.get("events"))
                if _as_str(_as_dict(item).get("event_type"))
            }
            if not source_events:
                raise ValueError(
                    f"Trigger source '{source_id}' does not declare any workflow events"
                )
            if event_type not in source_events:
                raise ValueError(
                    f"Event '{event_type}' is not declared by trigger source '{source_id}'"
                )
            continue

        if event_type in workflow_events:
            continue
        if event_type in extension_events:
            raise ValueError(
                f"Event '{event_type}' requires an explicit enabled extension trigger source"
            )
        raise ValueError(
            f"Event '{event_type}' is not available for {normalized_scope} automation rules"
        )
