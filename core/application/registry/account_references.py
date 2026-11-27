"""Same-account references of a workflow's steps (ADR 0020, step C4c).

A workflow belongs to one account, and so does everything one of its steps names:

- the child workflow of an Invoke step and the target of a hook binding are
  workflows of the step's own account; workflows are never platform objects;
- an agent, skill or published block is either a platform object (no account),
  usable by every account, or an object of the step's own account.

Another account's object is reported exactly like a missing one, so a reference
never tells an account what another account owns.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from core.application.registry.workflow_account import workflow_owner


def _text(value: Any) -> str:
    return str(value or "").strip()


def row_account(row: Any) -> str:
    """The owning account of a row or manifest, ``""`` when it names none."""
    if isinstance(row, Mapping):
        return _text(row.get("account_id"))
    return _text(getattr(row, "account_id", ""))


def workflow_in_account(row: Any, account_id: Any = None) -> bool:
    """Whether a resolved workflow belongs to *account_id* (the bound one by default).

    A row without an account comes from an account-scoped read (an installed
    extension's projection, a cached manifest of the account's own catalog), so
    only a row that names another account is refused.
    """
    owner = row_account(row)
    return not owner or owner == workflow_owner(account_id)


def shared_object_in_account(row: Any, account_id: Any = None) -> bool:
    """Whether an agent, skill or published block is usable by *account_id*.

    A platform object (no account) is usable by every account; an owned one only
    by its own account.
    """
    owner = row_account(row)
    return not owner or owner == workflow_owner(account_id)


#: The columns of a step that name an agent, a skill or a published block, and the
#: registry table and key column each one names.
STEP_SHARED_REFERENCES: tuple[tuple[str, str, str], ...] = (
    ("agent", "registry_agents", "agent_id"),
    ("skill", "registry_skills", "skill_id"),
    ("published block", "registry_published_block_contracts", "block_key"),
)


def step_shared_reference_ids(step: Mapping[str, Any]) -> dict[str, set[str]]:
    """The agent, skill and published block ids one step names."""
    override = step.get("override")
    override = override if isinstance(override, Mapping) else {}
    agent = _text(step.get("agent_id") or step.get("agent"))
    skill = _text(step.get("skill_id") or step.get("skill"))
    blocks = {
        _text(step.get("block_key")),
        _text(override.get("published_block_key")),
        _text(override.get("block_key")),
    }
    return {
        "agent": {agent} - {""},
        "skill": {skill} - {""},
        "published block": blocks - {""},
    }


def foreign_shared_references(
    cur: Any, *, account_id: str, steps: Iterable[Mapping[str, Any]]
) -> list[str]:
    """Messages for agents, skills and published blocks of another account.

    Runs inside the caller's transaction, so the definition write that follows
    sees the same rows. Objects that do not exist are left to readiness, which
    already reports them.
    """
    wanted: dict[str, set[str]] = {kind: set() for kind, _, _ in STEP_SHARED_REFERENCES}
    for step in steps:
        if isinstance(step, Mapping):
            for kind, ids in step_shared_reference_ids(step).items():
                wanted[kind].update(ids)
    owner = workflow_owner(account_id)
    messages: list[str] = []
    for kind, table, key in STEP_SHARED_REFERENCES:
        ids = sorted(wanted[kind])
        if not ids:
            continue
        # A name is foreign when rows of it exist but none is the account's own or a
        # platform one: a published block key is unique only within its owner, so
        # another account's block of the same key does not make the name foreign.
        cur.execute(
            f"SELECT DISTINCT r.{key} FROM {table} AS r "  # nosec B608 -- fixed identifiers; values are bound.
            f"WHERE r.{key} = ANY(%s) AND NOT EXISTS ("
            f"SELECT 1 FROM {table} AS v WHERE v.{key} = r.{key} "
            "AND (v.account_id IS NULL OR v.account_id::text = %s)) "
            f"ORDER BY r.{key}",
            (ids, owner),
        )
        for row in cur.fetchall() or []:
            value = row.get(key) if isinstance(row, Mapping) else row[0]
            messages.append(f"{kind.capitalize()} '{_text(value)}' not found")
    return messages


__all__ = [
    "STEP_SHARED_REFERENCES",
    "foreign_shared_references",
    "row_account",
    "shared_object_in_account",
    "step_shared_reference_ids",
    "workflow_in_account",
]
