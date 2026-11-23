"""A user's role assignments and their scope (ADR 0076 §7, §8).

An assignment gives a user a role in the whole installation or in a part of it: a
workspace, a project, or one workflow of a project. The schema stores all four; this
edition is one team trust domain (ADR 0066), so it writes and reads only assignments
without a scope and refuses one with a scope. Within one scope a user holds at most
one system role: the highest of those requested.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from core.application.auth.permissions import keep_highest_system_role
from core.application.management.errors import ServiceValidationError

#: The SQL predicate (alias ``ur`` for ``user_roles``) of an assignment without a scope.
GLOBAL_ASSIGNMENT_PREDICATE = (
    "ur.workspace_id IS NULL AND ur.project_id IS NULL AND ur.workflow_id IS NULL"
)

SCOPED_ASSIGNMENT_REFUSAL = (
    "A role can only be assigned for the whole installation: assignments limited to a "
    "workspace, a project or a workflow are not available in this edition"
)


class ScopedRoleAssignmentError(ServiceValidationError):
    """A role assignment with a workspace, project or workflow scope was requested."""


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


@dataclass(frozen=True, slots=True)
class RoleAssignment:
    """One role of one user, in the whole installation or in a part of it."""

    role: str
    workspace_id: str | None = None
    project_id: str | None = None
    workflow_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", str(self.role or "").strip().lower())
        object.__setattr__(self, "workspace_id", _optional_text(self.workspace_id))
        object.__setattr__(self, "project_id", _optional_text(self.project_id))
        object.__setattr__(self, "workflow_id", _optional_text(self.workflow_id))

    @property
    def is_global(self) -> bool:
        return self.workspace_id is None and self.project_id is None and self.workflow_id is None


def global_assignments(roles: Iterable[str]) -> list[RoleAssignment]:
    """Assignments without a scope for ``roles``."""
    return [RoleAssignment(role) for role in roles if str(role or "").strip()]


def require_global_assignments(assignments: Iterable[RoleAssignment]) -> list[RoleAssignment]:
    """``assignments`` if none has a scope; otherwise :class:`ScopedRoleAssignmentError`."""
    checked = list(assignments)
    if any(not assignment.is_global for assignment in checked):
        raise ScopedRoleAssignmentError(SCOPED_ASSIGNMENT_REFUSAL)
    return checked


def replace_user_role_assignments(
    cur: Any, *, user_id: str, assignments: Iterable[RoleAssignment]
) -> list[str]:
    """Replace the user's assignments with ``assignments`` and return the stored roles.

    The caller owns the transaction and the user-lifecycle lock. Every assignment must
    be global; of several system roles only the highest is stored.
    """
    checked = require_global_assignments(assignments)
    roles = keep_highest_system_role(assignment.role for assignment in checked)
    cur.execute(
        f"DELETE FROM user_roles AS ur WHERE ur.user_id = %s AND {GLOBAL_ASSIGNMENT_PREDICATE}",  # nosec B608 - a constant predicate.
        (user_id,),
    )
    if roles:
        cur.execute(
            """
            INSERT INTO user_roles (user_id, role_id)
            SELECT %s, id
            FROM roles
            WHERE key = ANY(%s)
            ON CONFLICT (user_id, role_id, workspace_id, project_id, workflow_id) DO NOTHING
            """,
            (user_id, roles),
        )
    return roles


__all__ = [
    "GLOBAL_ASSIGNMENT_PREDICATE",
    "SCOPED_ASSIGNMENT_REFUSAL",
    "RoleAssignment",
    "ScopedRoleAssignmentError",
    "global_assignments",
    "replace_user_role_assignments",
    "require_global_assignments",
]
