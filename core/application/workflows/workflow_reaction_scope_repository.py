"""Definition/rule integrity checked inside the owning workflow transaction."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from core.application.workflows.reaction_scopes import split_scoped_reaction_key


def _field(row: Any, name: str) -> str:
    return str(row.get(name) if isinstance(row, Mapping) else row[0])


def reaction_scope_issues(
    rules: Iterable[Mapping[str, Any]],
    *,
    steps: Iterable[Mapping[str, Any]],
    groups: Iterable[Mapping[str, Any]],
) -> list[dict[str, str]]:
    targets = {
        "step": {str(row.get("step_id") or "") for row in steps},
        "group": {str(row.get("group_id") or "") for row in groups},
    }
    issues = []
    for row in rules:
        key = str(row.get("reaction_key") or "")
        kind, identity, _ = split_scoped_reaction_key(key)
        if kind == "workflow":
            continue
        if kind not in targets or not identity or identity not in targets[kind]:
            issues.append(
                {
                    "code": "workflow.reaction.scope_missing",
                    "path": f"reactions.{key}",
                    "message": f"Reaction '{key}' refers to missing {kind} scope '{identity}'. Remove or retarget its rule before deleting this scope.",
                    "severity": "error",
                }
            )
    return issues


def _owner(account_id: Any) -> str:
    from core.application.registry.workflow_account import workflow_owner

    return workflow_owner(account_id)


def retained_reaction_scope_issues(
    cur: Any,
    workflow_id: str,
    *,
    steps: list[dict],
    groups: list[dict],
    account_id: str | None = None,
) -> list[dict[str, str]]:
    """The caller holds the workflow row lock; the owning account's rules are retained.

    A workflow belongs to one account (ADR 0020, C4b), so only that account's
    reaction rules can name its steps and Groups.
    """
    cur.execute(
        "SELECT reaction_key FROM workflow_reactions WHERE account_id = %s AND workflow_id = %s",
        (_owner(account_id), workflow_id),
    )
    rules = [{"reaction_key": _field(row, "reaction_key")} for row in cur.fetchall()]
    return reaction_scope_issues(rules, steps=steps, groups=groups)


def require_current_reaction_scopes(
    cur: Any, workflow_id: str, rules: list[dict], *, account_id: str | None = None
) -> None:
    """Validate reaction replacement while holding the same lock as definition writes."""
    owner = _owner(account_id)
    cur.execute(
        "SELECT workflow_id FROM registry_workflows "
        "WHERE account_id = %s AND workflow_id = %s AND deleted_at IS NULL FOR UPDATE",
        (owner, workflow_id),
    )
    if cur.fetchone() is None:
        raise ValueError(f"Workflow '{workflow_id}' scope is unavailable")
    cur.execute(
        "SELECT step_id FROM registry_workflow_steps "
        "WHERE account_id = %s AND owner_workflow_id = %s",
        (owner, workflow_id),
    )
    steps = [{"step_id": _field(row, "step_id")} for row in cur.fetchall()]
    cur.execute(
        "SELECT group_id FROM registry_workflow_groups WHERE account_id = %s AND workflow_id = %s",
        (owner, workflow_id),
    )
    groups = [{"group_id": _field(row, "group_id")} for row in cur.fetchall()]
    issues = reaction_scope_issues(rules, steps=steps, groups=groups)
    if issues:
        raise ValueError(issues[0]["message"])
