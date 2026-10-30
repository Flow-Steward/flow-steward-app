"""An agent's own inputs and outputs, saved apart from the skills and tools it inherits from.

The older ``PUT /api/agents/{id}/inputs`` writes the primary skill's fields when the agent
has one, so it edits every agent that shares the skill. These helpers always read and
write the agent's own rows, and refuse a name the contract rebuild would drop: the
materialized contract keeps the first field of a name, letter case aside, and inherited
fields come first.
"""

from __future__ import annotations

from typing import Any, Literal

from core.application.registry.contract_item_validation import validate_contract_items

IoKind = Literal["inputs", "outputs"]

_INHERITED_SOURCES = frozenset({"skill", "tool"})


def _issue(code: str, message: str, path: str) -> dict[str, str]:
    return {"code": code, "message": message, "path": path, "severity": "error"}


def inherited_fields(db: Any, agent_id: str, kind: IoKind) -> dict[str, dict[str, Any]]:
    """The skill and tool fields of one kind, keyed by lower-cased name."""
    from core.application.registry.io_resolver import IOResolver

    effective = IOResolver(db).compute_effective_io(agent_id, own_only=True)
    return {
        str(item.get("name") or "").strip().lower(): item
        for item in effective.get(kind) or []
        if item.get("source") in _INHERITED_SOURCES and str(item.get("name") or "").strip()
    }


def own_field_conflicts(
    items: list[dict[str, Any]], inherited: dict[str, dict[str, Any]], *, path_prefix: str
) -> list[dict[str, str]]:
    """Names the contract rebuild would drop: an inherited name, or a letter-case twin."""
    issues: list[dict[str, str]] = []
    first_spelling: dict[str, str] = {}
    for index, item in enumerate(items):
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        key = name.lower()
        path = f"{path_prefix}[{index}].name"
        owner = inherited.get(key)
        if owner is not None:
            issues.append(
                _issue(
                    "agent.own_field_inherited",
                    f"'{name}' is already provided by {owner.get('source')} "
                    f"'{owner.get('source_id')}' as '{owner.get('name')}'; an own field with "
                    "that name would be dropped when the contract is rebuilt. Rename it.",
                    path,
                )
            )
        elif key in first_spelling and first_spelling[key] != name:
            issues.append(
                _issue(
                    "agent.own_field_case_duplicate",
                    f"'{name}' differs from '{first_spelling[key]}' only in letter case; "
                    "the contract keeps one of them. Use one name.",
                    path,
                )
            )
        first_spelling.setdefault(key, name)
    return issues


def get_agent_own_io(db: Any, agent_id: str, kind: IoKind) -> list[dict[str, Any]]:
    getter = db.get_agent_own_inputs if kind == "inputs" else db.get_agent_own_outputs
    return list(getter(agent_id) or [])


def save_agent_own_io(
    db: Any, agent_id: str, kind: IoKind, items: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Replace the agent's own fields of one kind and rebuild its contract.

    Returns the saved own fields, or no fields and the issues that refused the save.
    Skills, tools and other agents are not touched.
    """
    path_prefix = f"agent.own_{kind}"
    _, issues = validate_contract_items(
        items, path_prefix=path_prefix, default_required=kind == "outputs"
    )
    if not issues:
        issues = own_field_conflicts(
            items, inherited_fields(db, agent_id, kind), path_prefix=path_prefix
        )
    if issues:
        return [], issues
    setter = db.set_agent_inputs if kind == "inputs" else db.set_agent_outputs
    setter(agent_id, items)
    db.materialize_agent_contract(agent_id)
    return get_agent_own_io(db, agent_id, kind), []


__all__ = [
    "get_agent_own_io",
    "inherited_fields",
    "own_field_conflicts",
    "save_agent_own_io",
]
