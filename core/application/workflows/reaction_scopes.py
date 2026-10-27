from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import quote, unquote

#: Automation scopes: the whole workflow, one real group, or one step.
REACTION_SCOPE_VALUES = frozenset({"workflow", "group", "step"})
_SCOPED_KEY_PREFIX = "scope::"


def normalize_reaction_scope(scope_kind: str | None, scope_id: str | None) -> tuple[str, str]:
    """The scope of an automation; an unknown or retired scope is refused."""
    kind = str(scope_kind or "workflow").strip().lower() or "workflow"
    if kind == "phase":
        raise ValueError("The phase automation scope was removed; use 'group' or 'step'")
    if kind not in REACTION_SCOPE_VALUES:
        raise ValueError(f"Unsupported automation scope '{kind}'; use workflow, group or step")
    sid = str(scope_id or "").strip()
    if kind == "workflow":
        sid = ""
    return kind, sid


def compose_scoped_reaction_key(scope_kind: str, scope_id: str, reaction_key: str) -> str:
    key = str(reaction_key or "").strip() or "rule"
    if key.startswith(_SCOPED_KEY_PREFIX):
        raise ValueError(f"Reaction key '{key}' must not start with '{_SCOPED_KEY_PREFIX}'")
    kind, sid = normalize_reaction_scope(scope_kind, scope_id)
    if kind == "workflow":
        return key
    return f"scope::{kind}::{quote(sid, safe='')}::{key}"


def split_scoped_reaction_key(reaction_key: str | None) -> tuple[str, str, str]:
    """The scope kind, scope id and base key of a stored reaction key.

    A stored key of a retired or unknown scope (``scope::phase::…``) keeps its
    own kind: it never matches an event or a requested scope, and the next save
    of the workflow's automations drops it.
    """
    raw = str(reaction_key or "").strip()
    if not raw.startswith(_SCOPED_KEY_PREFIX):
        return "workflow", "", raw
    parts = raw.split("::", 3)
    if len(parts) != 4:
        return "workflow", "", raw
    _, kind, scope_id_raw, base_key = parts
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind == "workflow":
        return "workflow", "", raw
    scope_id = unquote(scope_id_raw)
    return normalized_kind, scope_id, str(base_key or "").strip() or "rule"


def is_supported_reaction_scope(scope_kind: str | None) -> bool:
    return str(scope_kind or "").strip().lower() in REACTION_SCOPE_VALUES


def reaction_scope_matches_target(reaction_key: str | None, scope_kind: str, scope_id: str) -> bool:
    key_scope_kind, key_scope_id, _ = split_scoped_reaction_key(reaction_key)
    target_kind, target_id = normalize_reaction_scope(scope_kind, scope_id)
    if target_kind == "workflow":
        return key_scope_kind == "workflow"
    return key_scope_kind == target_kind and key_scope_id == target_id


def serialize_scoped_reaction_rows(
    rows: Iterable[Any],
    *,
    scope_kind: str | None,
    scope_id: str | None,
) -> tuple[str, str, list[dict[str, Any]]]:
    target_kind, target_scope_id = normalize_reaction_scope(scope_kind, scope_id)
    if target_kind != "workflow" and not target_scope_id:
        raise ValueError(f"scope_id is required for {target_kind} automation scope")

    scoped_rows: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        if not reaction_scope_matches_target(row.get("reaction_key"), target_kind, target_scope_id):
            continue
        normalized = dict(row)
        scope_row_kind, scope_row_id, base_key = split_scoped_reaction_key(row.get("reaction_key"))
        normalized["reaction_key"] = base_key
        normalized["scope_kind"] = scope_row_kind
        normalized["scope_id"] = scope_row_id
        scoped_rows.append(normalized)
    return target_kind, target_scope_id, scoped_rows


def merge_scoped_reaction_rules(
    existing_rules: Iterable[Any],
    scoped_rules: Iterable[Any],
    *,
    scope_kind: str | None,
    scope_id: str | None,
) -> tuple[str, str, list[dict[str, Any]]]:
    target_kind, target_scope_id = normalize_reaction_scope(scope_kind, scope_id)
    if target_kind != "workflow" and not target_scope_id:
        raise ValueError(f"scope_id is required for {target_kind} automation scope")

    # Rules of other scopes are kept; a rule of a retired scope is dropped.
    kept_rules = [
        dict(row)
        for row in (existing_rules or [])
        if isinstance(row, Mapping)
        and is_supported_reaction_scope(split_scoped_reaction_key(row.get("reaction_key"))[0])
        and not reaction_scope_matches_target(row.get("reaction_key"), target_kind, target_scope_id)
    ]

    normalized_scoped_rules: list[dict[str, Any]] = []
    for idx, item in enumerate(scoped_rules or []):
        if isinstance(item, Mapping):
            payload = dict(item)
        elif hasattr(item, "model_dump"):
            payload = item.model_dump()
        else:
            try:
                payload = dict(item)
            except Exception:
                payload = {}
        raw_key = str(payload.get("reaction_key") or "").strip() or f"reaction_{idx + 1}"
        payload["reaction_key"] = compose_scoped_reaction_key(target_kind, target_scope_id, raw_key)
        normalized_scoped_rules.append(payload)

    return target_kind, target_scope_id, [*kept_rules, *normalized_scoped_rules]
