"""Which trashed rows belong to an account (ADR 0020, step C5).

The trash surface lists, restores and purges rows of the caller's account only.
A row says whose it is through its own ``account_id``, else through its project
or its workspace. A row with no owner at all (a platform agent, skill or tool, a
dataset outside any workspace) is the installation account's: platform objects
are managed by the installation administrator, never by another account.
"""

from __future__ import annotations

from typing import Any

_UNOWNED_FOR_OTHER_ACCOUNTS = "FALSE"


def trash_account_predicate(
    *,
    has_account_id: bool,
    has_project_id: bool,
    has_workspace_id: bool,
    account_id: str,
    installation_account: bool,
) -> tuple[str, list[Any]]:
    """Return a SQL predicate and its parameters that keep one account's rows.

    The predicate refers to the row's own columns, so it can be appended to any
    trash query of the table it describes.
    """
    owner = str(account_id or "").strip()
    if not owner:
        return _UNOWNED_FOR_OTHER_ACCOUNTS, []
    if has_account_id:
        if installation_account:
            return "(account_id = %s OR account_id IS NULL)", [owner]
        return "account_id = %s", [owner]
    if has_project_id:
        clause = "project_id IN (SELECT p.id FROM projects p WHERE p.account_id = %s)"
        if installation_account:
            clause = f"({clause} OR project_id IS NULL)"
        return clause, [owner]
    if has_workspace_id:
        clause = "workspace_id IN (SELECT w.id FROM workspaces w WHERE w.account_id = %s)"
        if installation_account:
            clause = f"({clause} OR workspace_id IS NULL)"
        return clause, [owner]
    if installation_account:
        return "TRUE", []
    return _UNOWNED_FOR_OTHER_ACCOUNTS, []


__all__ = ["trash_account_predicate"]
