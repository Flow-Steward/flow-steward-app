"""A user's role assignments and their scope (ADR 0076 §7, §8).

An assignment gives a user a role in an account or in a part of it: a workspace, a
project, or one workflow of a project. The role in an account is stored on the
membership (ADR 0085); ``user_roles`` stores the scoped rows only. The users API
writes only assignments without a scope and refuses one with a scope;
project-scoped grants are written through the scoped roles port
(``core.application.auth.scoped_role_ports``). Within one scope a user holds at most
one system role: the highest of those requested.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from core.application.auth.membership_roles import keep_one_role, set_membership_role
from core.application.management.errors import ServiceValidationError

SCOPED_ASSIGNMENT_REFUSAL = (
    "A role is assigned here for a whole account: assignments limited to a "
    "workspace, a project or a workflow are not accepted by this endpoint"
)


class ScopedRoleAssignmentError(ServiceValidationError):
    """A role assignment with a workspace, project or workflow scope was requested."""


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


@dataclass(frozen=True, slots=True)
class RoleAssignment:
    """One role of one user, in a whole account or in a part of it."""

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
    cur: Any, *, user_id: str, account_id: str, assignments: Iterable[RoleAssignment]
) -> list[str]:
    """Set the user's role in ``account_id`` from ``assignments``; returns the stored roles.

    A role is held per account on the membership (ADR 0085): the membership must
    exist. The caller owns the transaction and the user-lifecycle lock. Every
    assignment must be without a scope; of several system roles only the highest is
    stored, and no system role stores none (no access in that account).
    """
    checked = require_global_assignments(assignments)
    role = keep_one_role(assignment.role for assignment in checked)
    return set_membership_role(cur, user_id=user_id, account_id=account_id, role=role)


__all__ = [
    "SCOPED_ASSIGNMENT_REFUSAL",
    "RoleAssignment",
    "ScopedRoleAssignmentError",
    "global_assignments",
    "replace_user_role_assignments",
    "require_global_assignments",
]
