"""Core's scoped roles port for platform modules (ADR 0076 section 8a, ADR 0082).

A module that limits people to chosen places never imports Core's repositories by path:
Core hands it this port on the runtime container as ``container.scoped_roles``. Each
call opens its own connection through the ``connection`` factory, which yields a
connection inside one transaction and commits on a clean exit.

A limited person has a row in ``scoped_role_users`` and one scoped ``user_roles``
row per granted place (see :mod:`core.application.auth.scoped_assignments`). A grant
is ``{"scope": "workspace" | "project" | "workflow", "id", "role"}``; a workflow
grant also names its ``project_id``. The port validates what a module cannot be
trusted to know about Core's data. A refusal is raised as :class:`LookupError` (an
unknown id, one of another account, or a workflow not linked to the named project;
a module answers 404) or :class:`ValueError` (an administrator, an unknown scope, a
place listed twice, or a role other than :data:`SCOPED_ROLE_KEYS`; a module answers
422). The account always comes
from the module's authenticated caller, never from the request.

Writes take the user-lifecycle lock that the users API takes for global roles, so a
concurrent role change cannot make an administrator limited. Every write records an
auth audit entry (``auth_scoped_roles_replace`` / ``auth_scoped_roles_lift``, the
shape of :func:`core.application.auth.audit.log_auth_audit_event`) in its own
transaction: a change is never stored without its audit entry, and an audit entry
that cannot be written undoes the change. A person never changes their own limit
(``acting_user_id``).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from core.application.auth.membership_roles import ACCOUNT_ADMIN_PREDICATE, is_account_admin

from .scoped_assignments import SCOPED_ROLE_KEYS

ConnectionFactory = Callable[[], AbstractContextManager[Any]]

#: The keys a grant may carry.
_GRANT_KEYS = frozenset({"scope", "id", "project_id", "role"})
#: The places a grant may name.
GRANT_SCOPES = ("workspace", "project", "workflow")
_USER_LIFECYCLE_LOCK = "flow-steward:user-lifecycle"
#: ``user_roles`` rows this port owns: every scoped row (workspace, project, workflow).
SCOPED_ROW_PREDICATE = "(ur.workspace_id IS NOT NULL OR ur.project_id IS NOT NULL)"
#: Scoped rows on places of one account (two ``%s``: the account, twice). A limit is
#: per account (ADR 0085): replacing or lifting it in one account keeps the others.
ACCOUNT_SCOPED_ROW_PREDICATE = (
    "(ur.workspace_id IN (SELECT ws.id FROM public.workspaces AS ws"
    " WHERE CAST(ws.account_id AS TEXT) = %s)"
    " OR ur.project_id IN (SELECT pr.id FROM public.projects AS pr"
    " WHERE CAST(pr.account_id AS TEXT) = %s))"
)
#: SQL (alias ``u``) true when the person is a member of the account bound to ``%s``.
_ACCOUNT_MEMBER_PREDICATE = (
    "EXISTS (SELECT 1 FROM public.user_account_memberships AS um"
    " WHERE um.user_id = u.id AND CAST(um.account_id AS TEXT) = %s)"
)
#: Auth audit actions of the port's writes (stored as ``auth_<action>``).
SCOPE_LIMIT_REPLACE_AUDIT_ACTION = "scoped_roles_replace"
SCOPE_LIMIT_LIFT_AUDIT_ACTION = "scoped_roles_lift"


class ScopedRoleUserNotFoundError(LookupError):
    """The user does not exist or is not a person of the caller's account."""


class ScopedRoleProjectNotFoundError(LookupError):
    """A workspace, project or workflow does not exist or is not of the caller's account."""


class ScopedRoleGrantInvalidError(ValueError):
    """The grant cannot be stored: an administrator, an unknown scope or an unknown role."""


class ScopedRoleSelfChangeError(ValueError):
    """The acting person asked to change their own project limit."""


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
        raise ScopedRoleUserNotFoundError("The caller has no account")
    return account


def _uuid(value: Any, error: type[Exception], what: str) -> str:
    try:
        return str(uuid.UUID(_text(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise error(f"{what} {value!r} was not found") from exc


@dataclass(frozen=True)
class _Grant:
    scope: str
    place_id: str
    role: str
    project_id: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.scope, self.project_id, self.place_id)

    def view(self) -> dict[str, str]:
        item = {"scope": self.scope, "id": self.place_id, "role": self.role}
        if self.scope == "workflow":
            item["project_id"] = self.project_id
        return item


def _workflow_key(value: Any) -> str:
    text = _text(value)
    if not text or len(text) > 200:
        raise ScopedRoleProjectNotFoundError(f"Workflow {value!r} was not found")
    return text


def _normalise_grants(grants: Iterable[Any]) -> list[_Grant]:
    """The grants; refuses unknown keys and scopes, unknown roles and duplicates."""
    result: list[_Grant] = []
    seen: set[tuple[str, str, str]] = set()
    for grant in grants or ():
        if not isinstance(grant, Mapping):
            raise ScopedRoleGrantInvalidError("A grant is an object with scope, id and role")
        unknown = sorted(key for key in grant if key not in _GRANT_KEYS)
        if unknown:
            raise ScopedRoleGrantInvalidError("Unknown grant field: " + ", ".join(unknown))
        scope = _text(grant.get("scope")).lower()
        if scope not in GRANT_SCOPES:
            raise ScopedRoleGrantInvalidError(
                f"Scope {scope!r} cannot be granted; use one of: " + ", ".join(GRANT_SCOPES)
            )
        role = _text(grant.get("role")).lower()
        if role not in SCOPED_ROLE_KEYS:
            raise ScopedRoleGrantInvalidError(
                f"Role {role!r} cannot be granted for a {scope}; use one of: "
                + ", ".join(sorted(SCOPED_ROLE_KEYS))
            )
        if scope == "workflow":
            project_id = _uuid(grant.get("project_id"), ScopedRoleProjectNotFoundError, "Project")
            item = _Grant(scope, _workflow_key(grant.get("id")), role, project_id)
        else:
            if _text(grant.get("project_id")):
                raise ScopedRoleGrantInvalidError(
                    f"A {scope} grant names its place in id, not project_id"
                )
            label = "Workspace" if scope == "workspace" else "Project"
            item = _Grant(
                scope, _uuid(grant.get("id"), ScopedRoleProjectNotFoundError, label), role
            )
        if item.key in seen:
            raise ScopedRoleGrantInvalidError(
                f"The {scope} {item.place_id} is listed more than once"
            )
        seen.add(item.key)
        result.append(item)
    return result


def _lock_user_lifecycle(cur: Any) -> None:
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_USER_LIFECYCLE_LOCK,))


def _refuse_self_change(user_id: str, acting_user_id: str | None) -> None:
    if acting_user_id is None:
        return
    try:
        acting = str(uuid.UUID(_text(acting_user_id)))
    except (ValueError, AttributeError, TypeError):
        return
    if acting == user_id:
        raise ScopedRoleSelfChangeError("You cannot change your own project access")


def _require_account_user(cur: Any, account_id: str, user_id: str) -> str:
    """The username of ``user_id``, a member of the account; refused when there is none."""
    cur.execute(
        f"""SELECT COALESCE(username, '') AS username
             FROM public.users AS u
            WHERE u.id::text = %s
              AND {_ACCOUNT_MEMBER_PREDICATE}
              AND u.deleted_at IS NULL
            LIMIT 1""",  # nosec B608 - a constant predicate.
        (user_id, account_id),
    )
    rows = _rows(cur)
    if not rows:
        raise ScopedRoleUserNotFoundError(f"User {user_id!r} was not found")
    return _text(rows[0].get("username"))


def record_scope_limit_audit(
    cur: Any,
    *,
    action: str,
    actor: str,
    username: str,
    detail: Mapping[str, Any],
    status: str = "ok",
) -> None:
    """One auth audit entry in the write's transaction; an error undoes the write."""
    cur.execute(
        """INSERT INTO public.platform_action_log
               (project_id, job_id, task_id, task_summary, agent, action_type,
                payload, result, meta, status)
           VALUES (NULL, NULL, 'auth', %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s)""",
        (
            f"Auth {action}",
            _text(actor) or "auth",
            f"auth_{action}",
            json.dumps({"username": username}),
            json.dumps(dict(detail)),
            json.dumps({"audit_category": "auth"}),
            _text(status) or "ok",
        ),
    )


def _is_admin(cur: Any, user_id: str, account_id: str) -> bool:
    """Whether the person is an administrator of the account (ADR 0085)."""
    return is_account_admin(cur, user_id, account_id)


def _missing(cur: Any, sql: str, params: tuple[Any, ...], wanted: list[str], key: str) -> list[str]:
    if not wanted:
        return []
    cur.execute(sql, params)
    found = {_text(row.get(key)) for row in _rows(cur)}
    return [item for item in wanted if item not in found]


def _require_account_places(cur: Any, account_id: str, grants: list[_Grant]) -> None:
    """Every granted place is a live place of the account; a workflow is linked to its project."""
    workspaces = sorted({g.place_id for g in grants if g.scope == "workspace"})
    projects = sorted(
        {g.place_id for g in grants if g.scope == "project"}
        | {g.project_id for g in grants if g.scope == "workflow"}
    )
    missing = _missing(
        cur,
        """SELECT id::text AS id FROM public.workspaces
            WHERE id::text = ANY(%s::text[])
              AND CAST(account_id AS TEXT) = %s
              AND deleted_at IS NULL""",
        (workspaces, account_id),
        workspaces,
        "id",
    )
    missing += _missing(
        cur,
        """SELECT id::text AS id FROM public.projects
            WHERE id::text = ANY(%s::text[])
              AND CAST(account_id AS TEXT) = %s
              AND deleted_at IS NULL""",
        (projects, account_id),
        projects,
        "id",
    )
    if missing:
        raise ScopedRoleProjectNotFoundError("Not found in this account: " + ", ".join(missing))
    pairs = sorted({f"{g.project_id}|{g.place_id}" for g in grants if g.scope == "workflow"})
    missing = _missing(
        cur,
        """SELECT pw.project_id::text || '|' || pw.workflow_id AS pair
             FROM public.project_workflows pw
             JOIN public.registry_workflows rw
             ON rw.account_id = pw.account_id AND rw.workflow_id = pw.workflow_id
            WHERE pw.project_id::text || '|' || pw.workflow_id = ANY(%s::text[])
              AND pw.deleted_at IS NULL
              AND rw.deleted_at IS NULL
              AND CAST(rw.account_id AS TEXT) = %s""",
        (pairs, account_id),
        pairs,
        "pair",
    )
    if missing:
        raise ScopedRoleProjectNotFoundError(
            "Workflow is not a workflow of the project in this account: "
            + ", ".join(item.replace("|", " / ") for item in missing)
        )


def _delete_scoped_rows(cur: Any, user_id: str, account_id: str) -> None:
    """Remove the person's scoped rows on places of ``account_id`` only."""
    cur.execute(
        f"DELETE FROM public.user_roles AS ur WHERE ur.user_id::text = %s AND {ACCOUNT_SCOPED_ROW_PREDICATE}",  # nosec B608 - a constant predicate.
        (user_id, account_id, account_id),
    )


class ScopedRolesPort:
    """``container.scoped_roles``: read and replace the scope limits of people."""

    def __init__(self, connection: ConnectionFactory) -> None:
        self._connection = connection

    def list_grants(
        self,
        account_id: str,
        /,
        *,
        user_id: str | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, str]]:
        """Grants of the account's limited people, one item per granted place.

        Each item is ``{"user_id", "username", "display_name", "scope", "id", "name",
        "role", "workspace_id", "workspace_name", "project_id", "project_name",
        "workflow_id", "workflow_name"}``: ``scope`` is ``workspace``, ``project`` or
        ``workflow`` and ``id`` / ``name`` the granted place; the other place fields
        name its containers (empty for a workspace's project or workflow). Only people
        with a limit row, of the account, and live places of the account are listed.
        ``project_id`` keeps the grants on that project and its workflows. A limited
        person with no place has no item: :meth:`list_limited_users` lists every
        limited person.
        """
        account = _account(account_id)
        filters = [_ACCOUNT_MEMBER_PREDICATE, "CAST(s.account_id AS TEXT) = %s"]
        # The workspace and project joins, then the filters.
        params: list[Any] = [account, account, account, account]
        if user_id is not None:
            filters.append("u.id::text = %s")
            params.append(_text(user_id))
        if project_id is not None:
            filters.append("ur.project_id::text = %s")
            params.append(_text(project_id))
        params.append(sorted(SCOPED_ROLE_KEYS))
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"""SELECT u.id::text AS user_id,
                           COALESCE(u.username, '') AS username,
                           COALESCE(u.display_name, '') AS display_name,
                           CASE WHEN ur.workflow_id IS NOT NULL THEN 'workflow'
                                WHEN ur.project_id IS NOT NULL THEN 'project'
                                ELSE 'workspace' END AS scope,
                           COALESCE(w.id::text, pw.id::text, '') AS workspace_id,
                           COALESCE(w.display_name, pw.display_name, '') AS workspace_name,
                           COALESCE(p.id::text, '') AS project_id,
                           COALESCE(p.name, '') AS project_name,
                           COALESCE(ur.workflow_id, '') AS workflow_id,
                           COALESCE(rw.display_name, ur.workflow_id, '') AS workflow_name,
                           r.key AS role
                      FROM public.scoped_role_users s
                      JOIN public.users u ON u.id = s.user_id
                      JOIN public.user_roles ur ON ur.user_id = u.id
                      JOIN public.roles r ON r.id = ur.role_id
                      LEFT JOIN public.workspaces w
                        ON w.id = ur.workspace_id AND ur.project_id IS NULL
                       AND w.deleted_at IS NULL AND CAST(w.account_id AS TEXT) = %s
                      LEFT JOIN public.projects p
                        ON p.id = ur.project_id
                       AND p.deleted_at IS NULL AND CAST(p.account_id AS TEXT) = %s
                      LEFT JOIN public.workspaces pw ON pw.id = p.workspace_id
                      LEFT JOIN public.registry_workflows rw
                        ON rw.account_id = ur.workflow_account_id AND rw.workflow_id = ur.workflow_id
                       AND rw.deleted_at IS NULL
                     WHERE {SCOPED_ROW_PREDICATE}
                       AND u.deleted_at IS NULL
                       AND (w.id IS NOT NULL OR p.id IS NOT NULL)
                       AND (ur.workflow_id IS NULL OR rw.workflow_id IS NOT NULL)
                       AND {" AND ".join(filters)}
                       AND r.key = ANY(%s::text[])
                     ORDER BY lower(COALESCE(NULLIF(u.display_name, ''), u.username)), u.id,
                              lower(COALESCE(w.display_name, pw.display_name, '')),
                              lower(COALESCE(p.name, '')), p.id,
                              lower(COALESCE(rw.display_name, ur.workflow_id, ''))""",  # nosec B608 - constant fragments; values are bound.
                tuple(params),
            )
            rows = _rows(cur)
        items: list[dict[str, str]] = []
        for row in rows:
            item = {key: _text(row.get(key)) for key in _LIST_KEYS}
            scope = item["scope"]
            item["id"] = {
                "workspace": item["workspace_id"],
                "project": item["project_id"],
                "workflow": item["workflow_id"],
            }[scope]
            item["name"] = {
                "workspace": item["workspace_name"],
                "project": item["project_name"],
                "workflow": item["workflow_name"],
            }[scope]
            items.append(item)
        return items

    def list_limited_users(self, account_id: str, /) -> list[dict[str, str]]:
        """Every limited person of the account, ``{"user_id", "created_by", "created_at"}``.

        ``created_by`` and ``created_at`` name who set the current limit and when.
        """
        account = _account(account_id)
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT s.user_id::text AS user_id,
                          s.created_by,
                          s.created_at
                     FROM public.scoped_role_users s
                     JOIN public.users u ON u.id = s.user_id
                    WHERE CAST(s.account_id AS TEXT) = %s
                      AND EXISTS (SELECT 1 FROM public.user_account_memberships AS um
                                   WHERE um.user_id = u.id
                                     AND CAST(um.account_id AS TEXT) = %s)
                      AND u.deleted_at IS NULL
                    ORDER BY s.user_id""",
                (account, account),
            )
            rows = _rows(cur)
        return [
            {
                "user_id": _text(row.get("user_id")),
                "created_by": _text(row.get("created_by")),
                "created_at": _iso(row.get("created_at")),
            }
            for row in rows
        ]

    def replace_user_grants(
        self,
        account_id: str,
        user_id: str,
        grants: Iterable[Mapping[str, Any]],
        /,
        *,
        created_by: str,
        acting_user_id: str | None = None,
        source: str = "",
    ) -> None:
        """Limit the person to ``grants`` (``{"scope", "id", "role"}`` each).

        A workflow grant also names its ``project_id``; the workflow must be linked to
        that project. One transaction writes the limit row and replaces the person's
        scoped rows. An empty list keeps the person limited to nothing;
        :meth:`lift_limit` removes the limit. Refused, with nothing written, for a
        person of another account or an administrator, a place of another account, an
        unknown scope, a role other than ``operator`` or ``viewer``, and for the acting
        person's own limit (``acting_user_id``).

        ``created_by`` and ``created_at`` of the limit row name who set the person's
        current limit and when: a repeated write replaces both, with the grants.
        """
        account = _account(account_id)
        user = _uuid(user_id, ScopedRoleUserNotFoundError, "User")
        _refuse_self_change(user, acting_user_id)
        normalised = _normalise_grants(grants)
        with self._connection() as conn, conn.cursor() as cur:
            _lock_user_lifecycle(cur)
            username = _require_account_user(cur, account, user)
            if _is_admin(cur, user, account):
                raise ScopedRoleGrantInvalidError("An administrator cannot be limited")
            _require_account_places(cur, account, normalised)
            cur.execute(
                """INSERT INTO public.scoped_role_users (user_id, account_id, created_by)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (user_id, account_id) DO UPDATE
                      SET created_by = EXCLUDED.created_by,
                          created_at = now()""",
                (user, account, _text(created_by)),
            )
            _delete_scoped_rows(cur, user, account)
            for grant in normalised:
                workspace_id = grant.place_id if grant.scope == "workspace" else None
                project_id = (
                    grant.place_id
                    if grant.scope == "project"
                    else grant.project_id
                    if grant.scope == "workflow"
                    else None
                )
                workflow_id = grant.place_id if grant.scope == "workflow" else None
                cur.execute(
                    """INSERT INTO public.user_roles
                           (user_id, role_id, workspace_id, project_id, workflow_id)
                       SELECT %s, r.id, %s, %s, %s FROM public.roles r WHERE r.key = %s""",
                    (user, workspace_id, project_id, workflow_id, grant.role),
                )
            record_scope_limit_audit(
                cur,
                action=SCOPE_LIMIT_REPLACE_AUDIT_ACTION,
                actor=created_by,
                username=username,
                detail={
                    "user_id": user,
                    "account_id": account,
                    "grants": [grant.view() for grant in normalised],
                    "source": _text(source),
                },
            )

    def lift_limit(
        self,
        account_id: str,
        user_id: str,
        /,
        *,
        created_by: str,
        acting_user_id: str | None = None,
        source: str = "",
    ) -> None:
        """Remove the person's limit and scoped rows in the account: their role there applies.

        ``created_by`` names who lifted it in the audit entry written in the same
        transaction; Core keeps no row for a person without a limit. Refused for the
        acting person's own limit (``acting_user_id``).
        """
        account = _account(account_id)
        user = _uuid(user_id, ScopedRoleUserNotFoundError, "User")
        _refuse_self_change(user, acting_user_id)
        with self._connection() as conn, conn.cursor() as cur:
            _lock_user_lifecycle(cur)
            username = _require_account_user(cur, account, user)
            _delete_scoped_rows(cur, user, account)
            cur.execute(
                "DELETE FROM public.scoped_role_users"
                " WHERE user_id::text = %s AND account_id::text = %s"
                " RETURNING user_id::text AS user_id",
                (user, account),
            )
            was_limited = bool(_rows(cur))
            record_scope_limit_audit(
                cur,
                action=SCOPE_LIMIT_LIFT_AUDIT_ACTION,
                actor=created_by,
                username=username,
                detail={
                    "user_id": user,
                    "account_id": account,
                    "was_limited": was_limited,
                    "source": _text(source),
                },
            )

    def candidates(self, account_id: str, /) -> list[dict[str, str]]:
        """What a module may offer: non-admin people and the account's places.

        People are ``{"kind": "user", "user_id", "username", "display_name"}``;
        workspaces ``{"kind": "workspace", "workspace_id", "name"}``; projects
        ``{"kind": "project", "project_id", "workspace_id", "name"}``; workflows
        ``{"kind": "workflow", "workflow_id", "project_id", "name"}``, one per live
        link of a workflow of the account to a live project.
        """
        account = _account(account_id)
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"""SELECT u.id::text AS user_id,
                           COALESCE(u.username, '') AS username,
                           COALESCE(u.display_name, '') AS display_name
                      FROM public.users u
                     WHERE {_ACCOUNT_MEMBER_PREDICATE}
                       AND u.is_active
                       AND u.deleted_at IS NULL
                       AND NOT {ACCOUNT_ADMIN_PREDICATE}
                     ORDER BY lower(COALESCE(NULLIF(u.display_name, ''), u.username)), u.id""",  # nosec B608 - constant predicates.
                (account, account),
            )
            users = _rows(cur)
            cur.execute(
                """SELECT id::text AS workspace_id, COALESCE(display_name, '') AS name
                     FROM public.workspaces
                    WHERE CAST(account_id AS TEXT) = %s
                      AND deleted_at IS NULL
                    ORDER BY lower(display_name), id""",
                (account,),
            )
            workspaces = _rows(cur)
            cur.execute(
                """SELECT id::text AS project_id, workspace_id::text AS workspace_id,
                          COALESCE(name, '') AS name
                     FROM public.projects
                    WHERE CAST(account_id AS TEXT) = %s
                      AND deleted_at IS NULL
                    ORDER BY lower(name), id""",
                (account,),
            )
            projects = _rows(cur)
            cur.execute(
                """SELECT rw.workflow_id, pw.project_id::text AS project_id,
                          COALESCE(NULLIF(rw.display_name, ''), rw.workflow_id) AS name
                     FROM public.project_workflows pw
                     JOIN public.projects p ON p.id = pw.project_id
                     JOIN public.registry_workflows rw
                       ON rw.account_id = pw.account_id AND rw.workflow_id = pw.workflow_id
                    WHERE CAST(p.account_id AS TEXT) = %s
                      AND CAST(rw.account_id AS TEXT) = %s
                      AND p.deleted_at IS NULL
                      AND pw.deleted_at IS NULL
                      AND rw.deleted_at IS NULL
                    ORDER BY lower(COALESCE(NULLIF(rw.display_name, ''), rw.workflow_id)),
                             rw.workflow_id, pw.project_id""",
                (account, account),
            )
            workflows = _rows(cur)
        return (
            [
                {
                    "kind": "user",
                    "user_id": _text(row.get("user_id")),
                    "username": _text(row.get("username")),
                    "display_name": _text(row.get("display_name")),
                }
                for row in users
            ]
            + [
                {
                    "kind": "workspace",
                    "workspace_id": _text(row.get("workspace_id")),
                    "name": _text(row.get("name")),
                }
                for row in workspaces
            ]
            + [
                {
                    "kind": "project",
                    "project_id": _text(row.get("project_id")),
                    "workspace_id": _text(row.get("workspace_id")),
                    "name": _text(row.get("name")),
                }
                for row in projects
            ]
            + [
                {
                    "kind": "workflow",
                    "workflow_id": _text(row.get("workflow_id")),
                    "project_id": _text(row.get("project_id")),
                    "name": _text(row.get("name")),
                }
                for row in workflows
            ]
        )


_LIST_KEYS = (
    "user_id",
    "username",
    "display_name",
    "scope",
    "workspace_id",
    "workspace_name",
    "project_id",
    "project_name",
    "workflow_id",
    "workflow_name",
    "role",
)


def _iso(value: Any) -> str:
    isoformat = getattr(value, "isoformat", None)
    return str(isoformat()) if callable(isoformat) else _text(value)


__all__ = [
    "ACCOUNT_SCOPED_ROW_PREDICATE",
    "GRANT_SCOPES",
    "SCOPED_ROW_PREDICATE",
    "SCOPE_LIMIT_LIFT_AUDIT_ACTION",
    "SCOPE_LIMIT_REPLACE_AUDIT_ACTION",
    "ScopedRoleGrantInvalidError",
    "ScopedRoleProjectNotFoundError",
    "ScopedRoleSelfChangeError",
    "ScopedRoleUserNotFoundError",
    "ScopedRolesPort",
    "record_scope_limit_audit",
]
