"""Approval steps of a person who leaves a company or stops deciding there (ADR 0085).

``approval_step_approvers`` names the people who may decide an Approval step of an
account (ADR 0082 §3a). A person who leaves the account, is deleted, or whose role
there no longer holds ``approvals.decide`` is taken off its steps, so a later
membership or role never brings an old assignment back.

A step whose last assigned approver goes is never opened to everyone who may decide
approvals: an approver list only narrows (ADR 0066). It is handed to the account's
active administrators at that moment, who decide it or assign someone else through
the approvers port. The decision check is unchanged: the administrators are simply
the step's assigned approvers now. If the account has no other active administrator
(the last-administrator rule keeps one, so only an inconsistent account), the step
keeps no approver rows and is decided as an unrestricted step, as before this rule.

Every function takes a cursor of the caller's transaction.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.auth.effective_permissions import role_permission_keys
from core.application.auth.permissions import ADMIN_ROLE, SYSTEM_ROLE_PERMISSIONS

#: What an assigned approver must hold through their role in the account.
DECIDE_PERMISSION = "approvals.decide"
#: ``created_by`` of the rows that hand a step to the administrators.
HANDED_TO_ADMINS_BY = "approver-left"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _pairs(rows: Any, keys: tuple[str, ...]) -> list[tuple[str, ...]]:
    result: list[tuple[str, ...]] = []
    for row in rows if isinstance(rows, (list, tuple)) else ():
        if isinstance(row, Mapping):
            values = tuple(_text(row.get(key)) for key in keys)
        elif isinstance(row, (list, tuple)) and len(row) >= len(keys):
            values = tuple(_text(item) for item in row[: len(keys)])
        else:
            continue
        if all(values):
            result.append(values)
    return result


def role_decides_approvals(cur: Any, role: str | None) -> bool:
    """Whether ``role`` holds ``approvals.decide`` by the stored role permissions.

    Read like the middleware reads them: ``admin`` holds everything, no role holds
    nothing, and a table without a single row falls back to the system roles' lists.
    """
    key = _text(role).lower()
    if not key:
        return False
    if key == ADMIN_ROLE:
        return True
    cur.execute(
        """SELECT r.key AS key, rp.permission_key AS permission_key
             FROM role_permissions AS rp JOIN roles AS r ON r.id = rp.role_id"""
    )
    stored: dict[str, set[str]] = {}
    for role_key, permission in _pairs(cur.fetchall(), ("key", "permission_key")):
        stored.setdefault(role_key.lower(), set()).add(permission)
    permissions = stored or SYSTEM_ROLE_PERMISSIONS
    return DECIDE_PERMISSION in role_permission_keys(key, permissions)


def release_approver(
    cur: Any, *, user_id: str, account_id: str | None = None
) -> list[dict[str, Any]]:
    """Take the person off the approval steps of ``account_id`` (every account: ``None``).

    Returns one entry per step left: ``workflow_id``, ``step_id``, ``account_id`` and
    ``handed_to_admins`` (the administrators the step went to when the person was its
    last approver; empty when other approvers remain).
    """
    user = _text(user_id)
    if not user:
        return []
    account = _text(account_id) or None
    cur.execute(
        """DELETE FROM approval_step_approvers
            WHERE user_id::text = %s AND (%s::text IS NULL OR account_id::text = %s)
        RETURNING account_id::text AS account_id, workflow_id, step_id""",
        (user, account, account),
    )
    released = _pairs(cur.fetchall(), ("account_id", "workflow_id", "step_id"))
    result: list[dict[str, Any]] = []
    for step_account, workflow, step in released:
        cur.execute(
            """SELECT count(*) AS remaining FROM approval_step_approvers
                WHERE account_id::text = %s AND workflow_id = %s AND step_id = %s""",
            (step_account, workflow, step),
        )
        row = cur.fetchone()
        remaining = int((row.get("remaining") if isinstance(row, Mapping) else row[0]) or 0)
        admins: list[str] = []
        if remaining == 0:
            cur.execute(
                """INSERT INTO approval_step_approvers
                       (workflow_id, step_id, user_id, created_by, account_id)
                   SELECT %s, %s, u.id, %s, m.account_id
                     FROM user_account_memberships AS m
                     JOIN roles AS r ON r.id = m.role_id AND r.key = 'admin'
                     JOIN users AS u ON u.id = m.user_id
                    WHERE m.account_id::text = %s
                      AND u.is_active AND u.deleted_at IS NULL
                      AND u.id::text <> %s
                   ON CONFLICT (account_id, workflow_id, step_id, user_id) DO NOTHING
                RETURNING user_id::text AS user_id""",
                (workflow, step, HANDED_TO_ADMINS_BY, step_account, user),
            )
            admins = sorted(item[0] for item in _pairs(cur.fetchall(), ("user_id",)))
        result.append(
            {
                "account_id": step_account,
                "workflow_id": workflow,
                "step_id": step,
                "handed_to_admins": admins,
            }
        )
    return result


def release_if_no_longer_deciding(
    cur: Any, *, user_id: str, account_id: str, role: str | None
) -> list[dict[str, Any]]:
    """After a role change: release the person's steps there unless ``role`` decides."""
    if role_decides_approvals(cur, role):
        return []
    return release_approver(cur, user_id=user_id, account_id=account_id)


__all__ = [
    "DECIDE_PERMISSION",
    "HANDED_TO_ADMINS_BY",
    "release_approver",
    "release_if_no_longer_deciding",
    "role_decides_approvals",
]
