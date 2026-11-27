"""Approvers assigned to an Approval step, and the check every decision passes (ADR 0082).

``approval_step_approvers`` lists, per ``(account_id, workflow_id, step_id)``, the users
who may decide that step; a workflow id is unique only within its account (ADR 0020,
C4b), so every read and write names the workflow's account. Core reads it at the one point every decision path reaches
(``ApprovalCommandService``) and when a decision override targets the step; a
platform module owns the write API. A step without rows is decided exactly as
before. A step with rows stays restricted when the feature's entitlement ends
(ADR 0082 section 4): access never becomes wider than it was, and job cancel stays
the way out.

Only a person can be an assigned approver: a signed-in user, or a personal token or
OAuth grant of that user, whose ``users.id`` is listed. Service accounts, machine
tokens, the system actor, external actors and reactions never pass a restricted
step.

The caller owns the connection and its transaction. Rows come back as dicts whether
the cursor yields mappings or tuples.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from .errors import ServiceForbiddenError, ServiceUnavailableError
from .guards import require_project_permission
from .scope import ScopeContext, normalize_scope

#: What deciding a step needs; a person limited to chosen projects needs it in the
#: job's project (ADR 0076 section 8).
DECIDE_PERMISSION = "approvals.decide"
#: ``project_id`` not given: the caller checked the project itself.
_PROJECT_NOT_GIVEN: Any = object()


def may_decide_in_project(
    scope: ScopeContext | None, project_id: Any, workflow_id: Any = None
) -> bool:
    """Whether a person limited to chosen projects holds the decision in ``project_id``.

    Everybody without a project limit passes: their global permission was checked by
    the route. A limited person passes only when the project is theirs and their
    role there decides approvals; with ``workflow_id`` (the run's workflow) the role
    in that workflow decides (a workflow grant, then the project's).
    """
    if scope is None or scope.project_permissions is None:
        return True
    try:
        require_project_permission(
            scope,
            str(project_id or ""),
            DECIDE_PERMISSION,
            workflow_id=str(workflow_id or "").strip() or None,
        )
    except ServiceForbiddenError:
        return False
    return True


APPROVAL_NOT_ASSIGNED_CODE = "approval_not_assigned"
APPROVAL_ASSIGNEES_FEATURE = "approval_assignees"

_NON_PERSONAL_PRINCIPAL_TYPES = frozenset({"service_account", "system_actor"})
_NON_PERSONAL_AUTH_SOURCES = frozenset(
    {"machine", "mcp_dev", "project_connection", "oauth_callback"}
)


class StepApproverRequiredError(ServiceForbiddenError):
    """A decision on a step with assigned approvers by someone who is not one of them."""

    def __init__(
        self,
        message: str = "Only the approvers assigned to this step can decide it",
        *,
        workflow_id: str = "",
        step_id: str = "",
    ) -> None:
        super().__init__(
            message,
            payload={
                "error": message,
                "code": APPROVAL_NOT_ASSIGNED_CODE,
                "workflow_id": workflow_id,
                "step_id": step_id,
            },
        )


def _rows(cur: Any) -> list[dict[str, Any]]:
    rows = cur.fetchall() or []
    names = [column[0] for column in (cur.description or [])]
    return [
        dict(row) if isinstance(row, Mapping) else dict(zip(names, row, strict=False))
        for row in rows
    ]


def _normalized_user_id(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        return None


class ApprovalAssigneeRepository:
    """Reads and replaces the assigned approvers of Approval steps of one account.

    ``account_id`` is the account that owns the workflows (the job's account for a
    decision); without one the default account's steps are read.
    """

    def __init__(self, conn: Any, *, account_id: Any = None) -> None:
        from core.application.registry.workflow_account import workflow_owner

        self.conn = conn
        self.account_id = workflow_owner(account_id)

    def list_approver_ids(self, workflow_id: str, step_id: str) -> tuple[str, ...]:
        workflow = str(workflow_id or "").strip()
        step = str(step_id or "").strip()
        if not workflow or not step:
            return ()
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT user_id::text AS user_id
                     FROM public.approval_step_approvers
                    WHERE workflow_id = %s AND step_id = %s AND account_id = %s
                    ORDER BY user_id""",
                (workflow, step, self.account_id),
            )
            rows = _rows(cur)
        ids = (_normalized_user_id(row.get("user_id")) for row in rows)
        return tuple(sorted({user_id for user_id in ids if user_id}))

    def list_approvers(self, workflow_id: str, step_id: str) -> list[dict[str, Any]]:
        workflow = str(workflow_id or "").strip()
        step = str(step_id or "").strip()
        if not workflow or not step:
            return []
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT a.user_id::text AS user_id,
                          COALESCE(u.username, '') AS username,
                          COALESCE(u.display_name, '') AS display_name
                     FROM public.approval_step_approvers a
                     JOIN public.users u ON u.id = a.user_id
                    WHERE a.workflow_id = %s AND a.step_id = %s AND a.account_id = %s
                    ORDER BY lower(COALESCE(NULLIF(u.display_name, ''), u.username)),
                             a.user_id""",
                (workflow, step, self.account_id),
            )
            rows = _rows(cur)
        return [
            {
                "user_id": str(row.get("user_id") or ""),
                "username": str(row.get("username") or ""),
                "display_name": str(row.get("display_name") or ""),
            }
            for row in rows
        ]

    def replace_approvers(
        self,
        workflow_id: str,
        step_id: str,
        user_ids: Iterable[str],
        *,
        created_by: str,
    ) -> None:
        """Make ``user_ids`` the step's approvers; an empty list lifts the restriction.

        A value that is not a UUID is refused before anything is written. Rows of
        users who stay keep their ``created_at`` and ``created_by``.
        """
        workflow = str(workflow_id or "").strip()
        step = str(step_id or "").strip()
        if not workflow or not step:
            raise ValueError("workflow_id and step_id are required")
        normalized: list[str] = []
        for raw in user_ids or ():
            user_id = _normalized_user_id(raw)
            if user_id is None:
                raise ValueError(f"'{raw}' is not a user id")
            if user_id not in normalized:
                normalized.append(user_id)
        actor = str(created_by or "").strip()
        with self.conn.cursor() as cur:
            cur.execute(
                """DELETE FROM public.approval_step_approvers
                    WHERE workflow_id = %s AND step_id = %s
                      AND NOT (user_id::text = ANY(%s::text[]))
                      AND account_id = %s""",
                (workflow, step, normalized, self.account_id),
            )
            for user_id in normalized:
                cur.execute(
                    """INSERT INTO public.approval_step_approvers
                           (workflow_id, step_id, user_id, created_by, account_id)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (account_id, workflow_id, step_id, user_id) DO NOTHING""",
                    (workflow, step, user_id, actor, self.account_id),
                )

    def approver_ids_by_step(
        self, workflow_ids: Iterable[str]
    ) -> dict[tuple[str, str], frozenset[str]]:
        """Approver ids of every restricted step of ``workflow_ids``, in one query."""
        workflows = sorted(
            {str(item).strip() for item in (workflow_ids or ()) if str(item or "").strip()}
        )
        if not workflows:
            return {}
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT workflow_id, step_id, user_id::text AS user_id
                     FROM public.approval_step_approvers
                    WHERE workflow_id = ANY(%s::text[]) AND account_id = %s""",
                (workflows, self.account_id),
            )
            rows = _rows(cur)
        grouped: dict[tuple[str, str], set[str]] = {}
        for row in rows:
            user_id = _normalized_user_id(row.get("user_id"))
            if user_id is None:
                continue
            key = (str(row.get("workflow_id") or ""), str(row.get("step_id") or ""))
            grouped.setdefault(key, set()).add(user_id)
        return {key: frozenset(ids) for key, ids in grouped.items()}

    def approvers_by_step(
        self, workflow_ids: Iterable[str]
    ) -> dict[tuple[str, str], list[dict[str, str]]]:
        """The approvers of every restricted step of ``workflow_ids``, names included.

        One query for any number of workflows, so a page of waiting approvals reads
        its assignments at once. The ids are exactly those :meth:`approver_ids_by_step`
        returns (a left join keeps a row whose user could not be read), so a
        ``can_decide`` computed from them matches the decision check.
        """
        workflows = sorted(
            {str(item).strip() for item in (workflow_ids or ()) if str(item or "").strip()}
        )
        if not workflows:
            return {}
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT a.workflow_id, a.step_id, a.user_id::text AS user_id,
                          COALESCE(u.username, '') AS username,
                          COALESCE(u.display_name, '') AS display_name
                     FROM public.approval_step_approvers a
                     LEFT JOIN public.users u ON u.id = a.user_id
                    WHERE a.workflow_id = ANY(%s::text[]) AND a.account_id = %s
                    ORDER BY a.workflow_id, a.step_id,
                             lower(COALESCE(NULLIF(u.display_name, ''), u.username, '')),
                             a.user_id""",
                (workflows, self.account_id),
            )
            rows = _rows(cur)
        grouped: dict[tuple[str, str], list[dict[str, str]]] = {}
        for row in rows:
            user_id = _normalized_user_id(row.get("user_id"))
            if user_id is None:
                continue
            key = (str(row.get("workflow_id") or ""), str(row.get("step_id") or ""))
            grouped.setdefault(key, []).append(
                {
                    "user_id": user_id,
                    "username": str(row.get("username") or ""),
                    "display_name": str(row.get("display_name") or ""),
                }
            )
        return grouped


def job_workflow_ids(row: Mapping[str, Any], *, meta: Mapping[str, Any] | None = None) -> list[str]:
    """Every id a job's workflow is known by; assignments under any of them apply.

    ``selected_workflow`` and ``selected_workflow_key`` of the job row, then
    ``meta.selected_workflow_id``. An Invoke child is its own job whose
    ``selected_workflow_key`` is the child workflow's id, so a child's gate is
    checked against the child workflow's assignments. ``meta`` replaces the row's
    own ``meta`` for a caller that selected only that key.
    """
    ids: list[str] = []
    for key in ("selected_workflow", "selected_workflow_key"):
        value = str(row.get(key) or "").strip()
        if value and value not in ids:
            ids.append(value)
    if meta is None:
        raw_meta: Any = row.get("meta")
        if isinstance(raw_meta, str):
            try:
                raw_meta = json.loads(raw_meta)
            except ValueError:
                raw_meta = {}
        meta = raw_meta if isinstance(raw_meta, Mapping) else {}
    meta_workflow = str(meta.get("selected_workflow_id") or "").strip()
    if meta_workflow and meta_workflow not in ids:
        ids.append(meta_workflow)
    return ids


def step_approvers(
    approvers_by_step: Mapping[tuple[str, str], Iterable[Mapping[str, Any]]],
    *,
    workflow_ids: Iterable[str],
    step_id: str,
) -> list[dict[str, str]]:
    """The approvers of ``step_id`` under any of ``workflow_ids``, each person once."""
    step = str(step_id or "").strip()
    if not step:
        return []
    merged: dict[str, dict[str, str]] = {}
    for workflow in dict.fromkeys(str(item or "").strip() for item in workflow_ids or ()):
        if not workflow:
            continue
        for approver in approvers_by_step.get((workflow, step), ()) or ():
            user_id = _normalized_user_id(approver.get("user_id"))
            if user_id and user_id not in merged:
                merged[user_id] = {
                    "user_id": user_id,
                    "username": str(approver.get("username") or ""),
                    "display_name": str(approver.get("display_name") or ""),
                }
    return list(merged.values())


def assignment_view(
    scope: ScopeContext | None,
    approvers: Iterable[Mapping[str, Any]],
    *,
    project_id: Any = _PROJECT_NOT_GIVEN,
    workflow_id: Any = None,
) -> dict[str, Any]:
    """``can_decide`` and the public ``approvers`` list a gate or a queue row carries.

    ``can_decide`` comes from :func:`may_decide_step`, the function the decision
    check uses, and, with ``project_id``, from :func:`may_decide_in_project`.
    ``approvers`` names people only (no user ids) and is empty for an unrestricted
    step.
    """
    listed = list(approvers or ())
    can_decide = may_decide_step(scope, [item.get("user_id") for item in listed])
    if can_decide and project_id is not _PROJECT_NOT_GIVEN:
        can_decide = may_decide_in_project(scope, project_id, workflow_id)
    return {
        "can_decide": can_decide,
        "approvers": [
            {
                "username": str(item.get("username") or ""),
                "display_name": str(item.get("display_name") or ""),
            }
            for item in listed
        ],
    }


def read_step_assignment(
    conn: Any,
    scope: ScopeContext | None,
    *,
    workflow_ids: Iterable[str],
    step_id: str,
    project_id: Any = _PROJECT_NOT_GIVEN,
    account_id: Any = None,
) -> dict[str, Any] | None:
    """The assignment view of one waiting step in one query; ``None`` without a connection.

    ``account_id`` is the job's account, which owns its workflow (ADR 0020, C4b).

    ``None`` leaves ``can_decide`` absent: the browser then offers the decision and
    the decision check still refuses one it must refuse.
    """
    if conn is None or not callable(getattr(conn, "cursor", None)):
        return None
    workflows = [str(item).strip() for item in workflow_ids or () if str(item or "").strip()]
    step = str(step_id or "").strip()
    run_workflow = workflows[0] if workflows else None
    if not workflows or not step:
        return assignment_view(scope, (), project_id=project_id, workflow_id=run_workflow)
    grouped = ApprovalAssigneeRepository(
        conn, account_id=_assignment_account(scope, account_id)
    ).approvers_by_step(workflows)
    return assignment_view(
        scope,
        step_approvers(grouped, workflow_ids=workflows, step_id=step),
        project_id=project_id,
        workflow_id=run_workflow,
    )


def _assignment_account(scope: Any, account_id: Any) -> Any:
    """The account whose step assignments apply: the job's, else the deciding scope's."""
    if str(account_id or "").strip():
        return account_id
    return getattr(scope, "account_id", None) if scope is not None else None


def may_decide_step(scope: ScopeContext | None, approver_ids: Iterable[str] | None) -> bool:
    """Whether ``scope`` may decide a step with these assigned approvers.

    An empty set means the step is not restricted: ``True``. Otherwise only a person
    whose user id is listed passes; a service account, a machine token, the system
    actor, an external actor and a reaction (whose scope carries no user id) do not.
    """
    raw_ids = [str(item).strip() for item in approver_ids or () if str(item or "").strip()]
    if not raw_ids:
        return True
    listed = {user_id for user_id in map(_normalized_user_id, raw_ids) if user_id}
    if not listed or scope is None:
        # Rows exist but none is a readable user id, or nobody is acting: refuse.
        return False
    if str(scope.principal_type or "").strip().lower() in _NON_PERSONAL_PRINCIPAL_TYPES:
        return False
    if str(scope.auth_source or "").strip().lower() in _NON_PERSONAL_AUTH_SOURCES:
        return False
    user_id = _normalized_user_id(scope.user_id)
    return user_id is not None and user_id in listed


def without_personal_identity(scope: Any) -> ScopeContext | None:
    """The scope a reaction or a dispatched command decides with: no person behind it.

    A reaction rule or a command acts for whoever configured or sent it, not as a
    person reviewing the step, so it never passes as an assigned approver even when
    the event that triggered it came in with a person's credentials.
    """
    if scope is None:
        return None
    resolved = scope if isinstance(scope, ScopeContext) else normalize_scope(scope)
    return resolved.model_copy(update={"user_id": None})


def require_step_approver(
    conn: Any,
    scope: ScopeContext | None,
    *,
    workflow_ids: Iterable[str],
    step_id: str,
    external_actor: Any = None,
    unknown_step_fails_closed: bool = False,
    project_id: Any = _PROJECT_NOT_GIVEN,
    account_id: Any = None,
) -> frozenset[str]:
    """Refuse with :class:`StepApproverRequiredError` unless ``scope`` may decide the step.

    ``workflow_ids`` are every workflow id the step may be addressed under (a caller
    that names one must not escape a restriction recorded under another); the
    approvers of all of them apply. Unreadable assignments fail closed: the lookup
    error propagates and the decision is not taken. Returns the step's approver ids
    (empty for an unrestricted step) when the decision may go ahead.

    With ``unknown_step_fails_closed`` a decision whose step cannot be told (no
    step id) is refused when any step of the workflows has assigned approvers,
    since it might be one of them.

    With ``project_id`` (the job's project) a person limited to chosen projects must
    also decide approvals in that project (ADR 0076 section 8); a job without a
    project is out of their reach.

    ``account_id`` is the job's account, which owns the workflow whose assignments
    apply (ADR 0020, C4b); without it the deciding scope's account is used.
    """
    workflows = [str(item).strip() for item in workflow_ids or () if str(item or "").strip()]
    if project_id is not _PROJECT_NOT_GIVEN and scope is not None:
        resolved = scope if isinstance(scope, ScopeContext) else normalize_scope(scope)
        if resolved.project_permissions is not None:
            # The run's workflow decides: a workflow grant, then the project's.
            require_project_permission(
                resolved,
                str(project_id or ""),
                DECIDE_PERMISSION,
                workflow_id=workflows[0] if workflows else None,
            )
    step = str(step_id or "").strip()
    if not workflows:
        return frozenset()
    if conn is None:
        # Without a connection the assignments cannot be read, so a restricted
        # step cannot be told from an unrestricted one: refuse rather than guess.
        raise ServiceUnavailableError("Approver assignments cannot be read without a database")
    repository = ApprovalAssigneeRepository(conn, account_id=_assignment_account(scope, account_id))
    if not step:
        if unknown_step_fails_closed and repository.approver_ids_by_step(workflows):
            raise StepApproverRequiredError(workflow_id=workflows[0], step_id="")
        return frozenset()
    approver_ids: set[str] = set()
    for workflow in dict.fromkeys(workflows):
        approver_ids.update(repository.list_approver_ids(workflow, step))
    if not approver_ids:
        return frozenset()
    if external_actor is not None or not may_decide_step(scope, approver_ids):
        raise StepApproverRequiredError(workflow_id=workflows[0], step_id=step)
    return frozenset(approver_ids)


__all__ = [
    "APPROVAL_ASSIGNEES_FEATURE",
    "APPROVAL_NOT_ASSIGNED_CODE",
    "DECIDE_PERMISSION",
    "ApprovalAssigneeRepository",
    "StepApproverRequiredError",
    "assignment_view",
    "job_workflow_ids",
    "may_decide_in_project",
    "may_decide_step",
    "read_step_assignment",
    "require_step_approver",
    "step_approvers",
    "without_personal_identity",
]
