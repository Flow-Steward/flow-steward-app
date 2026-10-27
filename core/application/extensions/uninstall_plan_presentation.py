"""Plain-language rows for the extension uninstall plan.

The inventory collectors speak in storage terms: a row per table, keyed by
whatever id that table uses. The person confirming an uninstall needs to know
what goes away in words they recognise — the extension's name, the workflows it
added, the projects that use them. This pass keeps every row's ``kind`` and
``id`` (the plan revision is a hash of the whole plan, and the confirm step
recomputes it the same way) and adds a plain ``label`` and ``display_name``,
plus ``project_name`` where a row belongs to a project.

It also drops rows that would only add noise: rows whose count is zero, the
storage summary of what the extension projected (the projected workflows and
datasets are listed by name instead), and the bookkeeping records that are
grouped into one "Extension registration and history" row.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .uninstall_inventory_queries import uninstall_inventory_row

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)

ARTIFACT_LABELS = {
    "workflow": "Workflow",
    "agent": "Agent",
    "skill": "Skill",
    "tool": "Tool",
    "dataset": "Dataset",
    "saved_query": "Saved query",
}

# Host bookkeeping a user never sees as such: grouped into one row.
_RECORD_KINDS = frozenset(
    {
        "bundle_record",
        "version_record",
        "enabled_state_record",
        "invocation_log",
        "entitlement_record",
        "usage_record",
        "consumer_usage_records",
    }
)

_LABELS = {
    "runtime_package": "Extension files",
    "release": "Extension files",
    "extension_records": "Extension registration and history",
    "project_workflow_assignment": "Workflows in projects",
    "project_dataset_assignment": "Datasets in projects",
    "workspace_data_connection": "Data connection",
    "workspace_connection_dataset": "Dataset from a data connection",
    "project_connection": "Project connection",
    "connection_credential_refs": "Saved connection credentials",
    "consumer_runtime_connections": "Connections in other accounts",
    "consumer_connection_credential_refs": "Saved connection credentials in other accounts",
    "consumer_callback_registrations": "Incoming event hooks in other accounts",
    "consumer_llm_provider_sources": "Model providers in other accounts",
    "consumer_project_state": "Saved extension data in other accounts",
    "oauth_configuration": "Sign-in app",
    "oauth_configuration_credentials": "Sign-in app secrets",
    "credential_ref": "Saved model provider credentials",
    "callback_registration": "Incoming event hook",
    "llm_provider_source": "Model provider",
    "project_state": "Saved extension data",
    "external_effect_audit": "Record of actions sent to other services",
    "project": "Project",
    "extension_install": "Extension",
    **ARTIFACT_LABELS,
}

# Rows whose stored value is internal (a namespace/key, an account-wide
# aggregate): the plain value is fixed.
_FIXED_VALUES = {
    "credential_ref": "API keys and secrets for this provider",
    "consumer_runtime_connections": "Connections other accounts made with this extension",
    "consumer_connection_credential_refs": "Credentials saved for those connections",
    "consumer_callback_registrations": "Event hooks other accounts registered",
    "consumer_llm_provider_sources": "Model providers other accounts set up",
    "consumer_project_state": "Data the extension saved for other accounts",
    "oauth_configuration_credentials": "Client secrets and tokens",
    "external_effect_audit": "Kept for audit, with secrets removed",
}


@dataclass(frozen=True)
class PlanSubject:
    """The extension being uninstalled, as the user knows it."""

    extension_id: str
    name: str
    version: str

    @property
    def title(self) -> str:
        return " ".join(part for part in (self.name or self.extension_id, self.version) if part)

    def missing_reason(self) -> str:
        return (
            f"Needs {self.name or self.extension_id}; it stops working until that "
            "extension is installed again."
        )


def human_text(value: Any) -> str:
    """Return ``value`` when a person can read it; drop ids and raw booleans."""
    text = str(value or "").strip()
    if not text or _UUID.search(text) or text.lower() in {"true", "false", "none", "null"}:
        return ""
    return text


def descriptor_display_name(descriptor: Any) -> str:
    """The name an extension descriptor gives the extension, or ""."""
    data = dict(descriptor or {}) if isinstance(descriptor, dict) else {}
    identity = data.get("identity")
    identity = identity if isinstance(identity, dict) else {}
    return str(
        data.get("display_name") or identity.get("display_name") or data.get("name") or ""
    ).strip()


def kind_label(kind: str) -> str:
    return _LABELS.get(kind) or (kind.replace("_", " ").strip().capitalize() or "Item")


def present_uninstall_plan(
    *,
    bl: Any,
    subject: PlanSubject,
    remove: list[dict[str, Any]],
    affected: list[dict[str, Any]],
    retained: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    project_names = _project_names(bl, [*remove, *affected, *retained])
    return (
        _present_inventory(remove, subject, project_names, shared=False),
        [_present_affected(row, subject, project_names) for row in affected],
        _present_inventory(retained, subject, project_names, shared=True),
    )


def _count(row: dict[str, Any]) -> int | None:
    if "count" not in row or row.get("count") is None:
        return None
    try:
        return int(row.get("count") or 0)
    except (TypeError, ValueError):
        return None


def _present_inventory(
    rows: list[dict[str, Any]],
    subject: PlanSubject,
    project_names: dict[str, str],
    *,
    shared: bool,
) -> list[dict[str, Any]]:
    rows = [row for row in rows if isinstance(row, dict) and _count(row) != 0]
    kinds = {str(row.get("kind") or "") for row in rows}
    artifact_keys = {
        (str(row.get("kind") or ""), str(row.get("id") or ""))
        for row in rows
        if str(row.get("kind") or "") in ARTIFACT_LABELS
    }
    presented: list[dict[str, Any]] = []
    record_count = 0
    record_index: int | None = None
    for row in rows:
        kind = str(row.get("kind") or "")
        if kind in _RECORD_KINDS:
            record_count += _count(row) or 1
            record_index = len(presented) if record_index is None else record_index
            continue
        item = _present_inventory_row(
            row, kind=kind, kinds=kinds, artifact_keys=artifact_keys, subject=subject, shared=shared
        )
        if item is not None:
            presented.append(_with_project(item, project_names))
    if record_count:
        presented.insert(
            record_index if record_index is not None else len(presented),
            {
                "kind": "extension_records",
                "id": subject.extension_id,
                "label": _LABELS["extension_records"],
                "display_name": "Settings, installed versions and activity log",
                "count": record_count,
            },
        )
    return _group_provider_sources(_merge_duplicates(presented), subject)


_NAMED_PROVIDERS_SHOWN = 3


def _group_provider_sources(
    rows: list[dict[str, Any]], subject: PlanSubject
) -> list[dict[str, Any]]:
    """One "Model providers" line for an extension that registers many.

    OpenRouter registers a provider source per upstream owner; its plan listed
    65 separate "Model provider — Venice (OpenRouter)" rows.
    """
    sources = [row for row in rows if row.get("kind") == "llm_provider_source"]
    if len(sources) < 2:
        return rows
    names = _without_shared_suffix(
        sorted({str(row.get("display_name") or "") for row in sources} - {""}, key=str.lower)
    )
    shown = names[:_NAMED_PROVIDERS_SHOWN]
    rest = len(sources) - len(shown)
    value = ", ".join(shown) + (f" and {rest} more" if rest > 0 else "")
    grouped = {
        "kind": "llm_provider_source",
        "id": f"{subject.extension_id}:llm_provider_sources",
        "label": "Model providers",
        "display_name": value or "Model providers of this extension",
        "count": sum(_count(row) or 1 for row in sources),
    }
    first = rows.index(sources[0])
    kept = [row for row in rows if row.get("kind") != "llm_provider_source"]
    kept.insert(min(first, len(kept)), grouped)
    return kept


def _split_parenthesized_suffix(name: str) -> tuple[str, str] | None:
    """``"Venice (OpenRouter)"`` -> ``("Venice", "(OpenRouter)")``; else None."""
    if not name.endswith(")"):
        return None
    head, sep, tail = name.rpartition(" (")
    if not sep or not head.strip() or "(" in tail or ")" in tail[:-1]:
        return None
    return head.rstrip(), f"({tail}"


def _without_shared_suffix(names: list[str]) -> list[str]:
    """Drop a "(OpenRouter)" every name carries: it says nothing per row."""
    parts = [_split_parenthesized_suffix(name) for name in names]
    if not names or not all(parts):
        return names
    if len({part[1] for part in parts if part}) != 1:
        return names
    return [part[0] for part in parts if part]


def _present_inventory_row(
    row: dict[str, Any],
    *,
    kind: str,
    kinds: set[str],
    artifact_keys: set[tuple[str, str]],
    subject: PlanSubject,
    shared: bool,
) -> dict[str, Any] | None:
    if kind == "projection":
        # Storage summary; the projected artifacts are listed by name.
        return None
    if kind == "release" and "runtime_package" in kinds:
        # One extension, one line: its files and its release record go together.
        return None
    if kind in {"runtime_package", "release"}:
        label = _LABELS[kind] + (" (another install still uses them)" if shared else "")
        return {**row, "label": label, "display_name": subject.title}
    if kind == "projection_record":
        artifact_kind = str(row.get("artifact_kind") or "")
        if (artifact_kind, str(row.get("artifact_id") or "")) in artifact_keys:
            return None
        label = ARTIFACT_LABELS.get(artifact_kind, "Item added by the extension")
        return {
            **row,
            "label": label,
            "display_name": human_text(row.get("display_name")) or f"Unnamed {label.lower()}",
        }
    label = kind_label(kind)
    return {**row, "label": label, "display_name": _inventory_value(row, kind, label)}


def _inventory_value(row: dict[str, Any], kind: str, label: str) -> str:
    if kind in _FIXED_VALUES:
        return _FIXED_VALUES[kind]
    if kind == "project_state":
        return "Data the extension saved for this project"
    if kind == "callback_registration":
        return _humanize_identifier(row.get("display_name")) or "Event hook"
    if kind in {"project_workflow_assignment", "project_dataset_assignment"}:
        count = _count(row) or 0
        return human_text(row.get("display_name")) or (
            f"Used in {count} project{'s' if count != 1 else ''}"
        )
    return human_text(row.get("display_name")) or f"Unnamed {label.lower()}"


def _present_affected(
    row: dict[str, Any], subject: PlanSubject, project_names: dict[str, str]
) -> dict[str, Any]:
    if not isinstance(row, dict):
        return row
    kind = str(row.get("kind") or "")
    label = kind_label(kind)
    item = {
        **row,
        "label": label,
        "display_name": human_text(row.get("display_name"))
        or _humanize_identifier(row.get("id"))
        or f"Unnamed {label.lower()}",
    }
    # ``reason`` is what the confirm step persists as the availability text, so
    # it stays as the collectors wrote it; ``explanation`` is the plain line.
    if (
        str(row.get("reason") or "").strip()
        == f"Missing required extension: {subject.extension_id}"
    ):
        item["explanation"] = subject.missing_reason()
    if kind == "project":
        return item
    return _with_project(item, project_names)


def _with_project(row: dict[str, Any], project_names: dict[str, str]) -> dict[str, Any]:
    if human_text(row.get("project_name")):
        return row
    project_id = str(row.get("project_id") or "").strip()
    name = human_text(project_names.get(project_id)) if project_id else ""
    if name:
        return {**row, "project_name": name}
    return {key: value for key, value in row.items() if key != "project_name"}


def _merge_duplicates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold rows a person cannot tell apart (same thing, same project) into one."""
    merged: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            str(row.get("kind") or ""),
            str(row.get("label") or ""),
            str(row.get("display_name") or ""),
            str(row.get("project_name") or ""),
        )
        existing = merged.get(key)
        if existing is None:
            merged[key] = dict(row)
            continue
        existing["count"] = (_count(existing) or 1) + (_count(row) or 1)
    return list(merged.values())


def _humanize_identifier(value: Any) -> str:
    text = human_text(value)
    if not text:
        return ""
    words = re.sub(r"[_\-.]+", " ", text).strip()
    return words[:1].upper() + words[1:]


def _project_names(bl: Any, rows: Iterable[dict[str, Any]]) -> dict[str, str]:
    ids = sorted(
        {
            str(row.get("project_id") or "").strip()
            for row in rows
            if isinstance(row, dict)
            and str(row.get("project_id") or "").strip()
            and not human_text(row.get("project_name"))
        }
    )
    if (
        not ids
        or not callable(getattr(bl, "cursor", None))
        or not getattr(bl, "_table_exists", lambda *_args: False)("projects")
    ):
        return {}
    with bl.cursor() as cur:
        cur.execute(
            "SELECT id::text AS project_id, name FROM projects WHERE id::text = ANY(%s)",
            (ids,),
        )
        found = [
            uninstall_inventory_row(raw, ("project_id", "name")) for raw in cur.fetchall() or []
        ]
    return {
        str(row.get("project_id") or "").strip(): str(row.get("name") or "").strip()
        for row in found
    }


__all__ = [
    "ARTIFACT_LABELS",
    "PlanSubject",
    "descriptor_display_name",
    "human_text",
    "kind_label",
    "present_uninstall_plan",
]
