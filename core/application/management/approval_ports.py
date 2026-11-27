"""Core's approval ports for platform modules (ADR 0082, section 3a).

A module that changes assigned approvers or reads the approval journal never imports
Core's repositories by path: Core hands it these ports on the runtime container as
``container.approval_assignments`` and ``container.approval_journal``. Each call
opens its own connection through the ``connection`` factory, which yields a
connection inside one transaction and commits on a clean exit.

The ports validate what a module cannot be trusted to know about Core's data. A
refusal is raised as :class:`LookupError` (an unknown or foreign workflow; a module
answers 404) or :class:`ValueError` (a user id that is not a person of the account
who may decide approvals; a module answers 422). The account always comes from the
module's authenticated caller, never from the request.

``approval_step_approvers`` names the workflow's account next to its id (ADR 0020,
C4b): only a workflow the account owns accepts assignments, and the assignments of
one account never apply to another account's workflow with the same id.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from typing import Any

from core.application.auth.effective_permissions import (
    load_role_permissions,
    role_permission_keys,
)
from core.application.auth.scoped_assignments import SCOPED_ROLE_KEYS, load_account_scope_limits

from .approval_assignees import ApprovalAssigneeRepository
from .approval_journal import ApprovalJournalRepository

#: The permission an assigned approver must hold through a role.
DECIDE_PERMISSION = "approvals.decide"

ConnectionFactory = Callable[[], AbstractContextManager[Any]]


class ApprovalWorkflowNotFoundError(LookupError):
    """The workflow does not exist or does not belong to the caller's account."""


class ApprovalApproverInvalidError(ValueError):
    """A user id is not a person of the account who may decide approvals."""


class ApprovalStepInvalidError(ValueError):
    """The step does not exist in the workflow or is not an Approval step."""


#: ``registry_workflow_steps.step_kind`` of an Approval step.
APPROVAL_STEP_KIND = "approval"


def _rows(cur: Any) -> list[dict[str, Any]]:
    rows = cur.fetchall() or []
    names = [column[0] for column in (cur.description or [])]
    return [
        dict(row) if isinstance(row, Mapping) else dict(zip(names, row, strict=False))
        for row in rows
    ]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _account(account_id: Any) -> str:
    account = _text(account_id)
    if not account:
        raise ApprovalWorkflowNotFoundError("The caller has no account")
    return account


def workflow_owned_by_account(conn: Any, workflow_id: str, account_id: str) -> bool:
    """Whether ``workflow_id`` is a live workflow that ``account_id`` owns."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT 1 AS present
                 FROM public.registry_workflows
                WHERE workflow_key = %s
                  AND deleted_at IS NULL
                  AND CAST(account_id AS TEXT) = %s
                LIMIT 1""",
            (workflow_id, account_id),
        )
        return bool(_rows(cur))


def workflow_step_kind(
    conn: Any, workflow_id: str, step_id: str, *, account_id: Any = None
) -> str | None:
    """The ``step_kind`` of ``step_id`` in the account's ``workflow_id``; ``None`` without one."""
    from core.application.registry.workflow_account import workflow_owner

    with conn.cursor() as cur:
        cur.execute(
            """SELECT step_kind
                 FROM public.registry_workflow_steps
                WHERE owner_workflow_id = %s
                  AND step_id = %s
                  AND CAST(account_id AS TEXT) = %s
                LIMIT 1""",
            (workflow_id, step_id, workflow_owner(account_id)),
        )
        rows = _rows(cur)
    return _text(rows[0].get("step_kind")) if rows else None


def _limited_decide_projects(
    conn: Any,
    account_id: str,
    role_permissions: Mapping[str, Any],
    *,
    workflow_id: str | None = None,
) -> dict[str, set[str]]:
    """``{user id: projects where their scoped role decides}`` of the account's limited people.

    A limited person (ADR 0076 section 8a) decides only where the most specific grant
    covering the place holds ``approvals.decide``: with ``workflow_id``, in that
    workflow of each project (workflow grant, then project, then workspace grant);
    without it, at project level. A limited person who decides nowhere maps to an
    empty set. A read error propagates (fail closed).
    """
    workflow = _text(workflow_id)
    limited: dict[str, set[str]] = {}
    for user_id, limit in load_account_scope_limits(conn, account_id=account_id).items():
        projects = limited.setdefault(user_id, set())
        for project_id in limit.project_ids:
            roles = (
                limit.workflow_level_roles(project_id, workflow)
                if workflow
                else limit.project_level_roles(project_id)
            )
            if roles and any(
                role in SCOPED_ROLE_KEYS
                and DECIDE_PERMISSION in role_permission_keys(role, role_permissions)
                for role in roles
            ):
                projects.add(project_id)
    return limited


def workflow_project_ids(conn: Any, workflow_id: str, account_id: str) -> frozenset[str]:
    """The live projects of ``account_id`` that ``workflow_id`` is linked to."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT pw.project_id::text AS project_id
                 FROM public.project_workflows pw
                 JOIN public.projects p ON p.id = pw.project_id
                WHERE pw.workflow_id = %s
                  AND pw.deleted_at IS NULL
                  AND p.deleted_at IS NULL
                  AND CAST(p.account_id AS TEXT) = %s""",
            (workflow_id, account_id),
        )
        return frozenset(_text(row.get("project_id")) for row in _rows(cur) if row)


def decide_candidates(
    conn: Any,
    account_id: str,
    *,
    project_id: str | None = None,
    project_ids: Iterable[str] | None = None,
    workflow_id: str | None = None,
) -> list[dict[str, str]]:
    """Active members of the account whose role there holds ``approvals.decide``.

    Role keys are resolved by :func:`role_permission_keys`, the function the auth
    middleware uses (``admin`` holds every permission without rows). The role of the
    person's membership of the account is the ceiling (ADR 0085), whatever their home
    account; a person limited to chosen projects (ADR 0076 section 8) is a
    candidate only where a project's scoped role decides too: in ``project_id`` when
    it is given; in every one of ``project_ids`` when they are given (the projects a
    workflow runs in: an approver must be able to decide each of its runs, and a
    workflow linked to no project is out of a limited person's reach); else in at
    least one project.
    """
    role_permissions = load_role_permissions(conn)
    limited = _limited_decide_projects(conn, account_id, role_permissions, workflow_id=workflow_id)
    wanted_project = _text(project_id)
    wanted_projects = (
        None
        if project_ids is None
        else frozenset(_text(item) for item in project_ids if _text(item))
    )
    with conn.cursor() as cur:
        cur.execute(
            """SELECT u.id::text AS user_id,
                      COALESCE(u.username, '') AS username,
                      COALESCE(u.display_name, '') AS display_name,
                      r.key AS role_key
                 FROM public.users u
                 JOIN public.user_account_memberships m ON m.user_id = u.id
                 JOIN public.roles r ON r.id = m.role_id
                WHERE CAST(m.account_id AS TEXT) = %s
                  AND u.is_active
                  AND u.deleted_at IS NULL
                ORDER BY lower(COALESCE(NULLIF(u.display_name, ''), u.username)), u.id""",
            (account_id,),
        )
        rows = _rows(cur)
    people: dict[str, dict[str, str]] = {}
    for row in rows:
        if DECIDE_PERMISSION not in role_permission_keys(
            _text(row.get("role_key")), role_permissions
        ):
            continue
        user_id = _text(row.get("user_id"))
        if user_id in limited:
            decides_in = limited[user_id]
            if not decides_in or (wanted_project and wanted_project not in decides_in):
                continue
            if wanted_projects is not None and (
                not wanted_projects or not wanted_projects <= decides_in
            ):
                continue
        if user_id and user_id not in people:
            people[user_id] = {
                "user_id": user_id,
                "username": _text(row.get("username")),
                "display_name": _text(row.get("display_name")),
            }
    return list(people.values())


class ApprovalAssignmentsPort:
    """``container.approval_assignments``: read and replace a step's approvers."""

    def __init__(self, connection: ConnectionFactory) -> None:
        self._connection = connection

    def list_approvers(
        self, workflow_id: str, step_id: str, *, account_id: str | None = None
    ) -> list[dict[str, str]]:
        """The step's approvers, ``{"user_id", "username", "display_name"}`` each.

        With ``account_id`` the workflow must belong to that account
        (:class:`ApprovalWorkflowNotFoundError` otherwise) and only people of that
        account are listed.
        """
        workflow, step = _text(workflow_id), _text(step_id)
        with self._connection() as conn:
            if account_id is not None:
                account = _account(account_id)
                if not workflow_owned_by_account(conn, workflow, account):
                    raise ApprovalWorkflowNotFoundError(f"Workflow {workflow!r} was not found")
                approvers = ApprovalAssigneeRepository(conn, account_id=account).list_approvers(
                    workflow, step
                )
                members = _account_user_ids(conn, account, [item["user_id"] for item in approvers])
                return [item for item in approvers if item["user_id"] in members]
            return ApprovalAssigneeRepository(conn).list_approvers(workflow, step)

    def replace_approvers(
        self,
        workflow_id: str,
        step_id: str,
        user_ids: Iterable[str],
        *,
        account_id: str,
        created_by: str,
    ) -> None:
        """Make ``user_ids`` the step's approvers; an empty list lifts the restriction.

        Refused, with nothing written, unless the account owns the workflow, the step
        exists in it and is an Approval step (:class:`ApprovalStepInvalidError`, a
        ``ValueError``), and every user id is a person of the account who may decide
        approvals (:func:`candidates`).
        """
        account = _account(account_id)
        workflow, step = _text(workflow_id), _text(step_id)
        if not workflow or not step:
            raise ApprovalWorkflowNotFoundError("workflow_id and step_id are required")
        requested: list[str] = []
        for raw in user_ids or ():
            try:
                user_id = str(uuid.UUID(_text(raw)))
            except ValueError as exc:
                raise ApprovalApproverInvalidError(f"'{raw}' is not a user id") from exc
            if user_id not in requested:
                requested.append(user_id)
        with self._connection() as conn:
            if not workflow_owned_by_account(conn, workflow, account):
                raise ApprovalWorkflowNotFoundError(f"Workflow {workflow!r} was not found")
            kind = workflow_step_kind(conn, workflow, step, account_id=account)
            if kind is None:
                raise ApprovalStepInvalidError(
                    f"Step {step!r} does not exist in workflow {workflow!r}"
                )
            if kind != APPROVAL_STEP_KIND:
                raise ApprovalStepInvalidError(f"Step {step!r} is not an Approval step")
            # An approver must be able to decide the step in every project the
            # workflow runs in; a person limited elsewhere is refused.
            allowed = {
                item["user_id"]
                for item in decide_candidates(
                    conn,
                    account,
                    project_ids=workflow_project_ids(conn, workflow, account),
                    workflow_id=workflow,
                )
            }
            refused = [user_id for user_id in requested if user_id not in allowed]
            if refused:
                raise ApprovalApproverInvalidError(
                    "Not a person of this account who may decide approvals: " + ", ".join(refused)
                )
            ApprovalAssigneeRepository(conn, account_id=account).replace_approvers(
                workflow, step, requested, created_by=created_by
            )

    def candidates(
        self, account_id: str, *, workflow_id: str | None = None
    ) -> list[dict[str, str]]:
        """People who may be assigned: active users of the account who may decide.

        With ``workflow_id`` (which the account must own) a person limited to chosen
        projects is listed only when they decide in every project the workflow runs
        in, the rule :meth:`replace_approvers` applies.
        """
        account = _account(account_id)
        workflow = _text(workflow_id)
        with self._connection() as conn:
            if not workflow:
                return decide_candidates(conn, account)
            if not workflow_owned_by_account(conn, workflow, account):
                raise ApprovalWorkflowNotFoundError(f"Workflow {workflow!r} was not found")
            return decide_candidates(
                conn,
                account,
                project_ids=workflow_project_ids(conn, workflow, account),
                workflow_id=workflow,
            )


class ApprovalJournalPort:
    """``container.approval_journal``: one account's journal, read only."""

    def __init__(self, connection: ConnectionFactory) -> None:
        self._connection = connection

    def list_entries(self, account_id: str, /, **filters: Any) -> dict[str, Any]:
        """``{"items", "total"}`` of the account's journal, newest first.

        Every item carries ``job_exists``: ``False`` once its run was hard-deleted (a
        trashed run still has a page), so a page shows a marker, not a dead link.

        Filters: ``workflow_id``, ``decided_by``, ``date_from``, ``date_to``,
        ``limit``, ``offset``; anything else is refused with :class:`ValueError`.
        """
        account = _account(account_id)
        unknown = sorted(set(filters) - _JOURNAL_FILTERS)
        if unknown:
            raise ValueError(f"Unknown journal filter: {', '.join(unknown)}")
        with self._connection() as conn:
            return ApprovalJournalRepository(conn).list_entries(account, **filters)


_JOURNAL_FILTERS = frozenset(
    {"workflow_id", "decided_by", "date_from", "date_to", "limit", "offset"}
)


def _account_user_ids(conn: Any, account_id: str, user_ids: list[str]) -> set[str]:
    if not user_ids:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id::text AS user_id FROM public.users
                WHERE id::text = ANY(%s::text[]) AND CAST(account_id AS TEXT) = %s""",
            (user_ids, account_id),
        )
        return {_text(row.get("user_id")) for row in _rows(cur)}


__all__ = [
    "APPROVAL_STEP_KIND",
    "DECIDE_PERMISSION",
    "ApprovalApproverInvalidError",
    "ApprovalAssignmentsPort",
    "ApprovalJournalPort",
    "ApprovalStepInvalidError",
    "ApprovalWorkflowNotFoundError",
    "decide_candidates",
    "workflow_owned_by_account",
    "workflow_project_ids",
    "workflow_step_kind",
]
