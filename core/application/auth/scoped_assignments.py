"""Scoped role limits of a person (ADR 0076 section 8a, ADR 0082).

A row in ``scoped_role_users`` marks a person whose access is limited to the places
their scoped ``user_roles`` rows name. A place is a workspace, a project or a
workflow of a project:

* a workspace grant (``workspace_id``, no project) covers every live project of the
  workspace and all their workflows, including projects created later: the projects
  are read on every request;
* a project grant (``project_id``) covers one project and all its workflows;
* a workflow grant (``project_id`` + ``workflow_id``) makes the project reachable but
  only those workflows inside it, unless a project or workspace grant also covers the
  project.

The effective role in a place is the role of the most specific grant covering it
(workflow, then project, then workspace), and the global role stays the ceiling.
Every other workspace, project and workflow is out of reach. A person without a
limit row keeps the access of the global role.

The limit row is durable on purpose: deleting the last granted place removes its
``user_roles`` rows by cascade, but never turns a limited person into an
unrestricted one. Limits keep applying after a feature entitlement ends. A limit
applies only in the account it was set in.

Only the roles in :data:`SCOPED_ROLE_KEYS` are read here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

#: Roles a scoped grant may carry.
SCOPED_ROLE_KEYS: frozenset[str] = frozenset({"operator", "viewer"})

#: The role a person holds in a place they reach only because a grant names
#: something inside it: a project reached only through workflow grants, or a
#: workspace reached only through project or workflow grants. It shows the place;
#: it never acts on it.
CONTAINER_ROLE = "viewer"


@dataclass(frozen=True)
class ScopeLimit:
    """The places a limited person may reach, with the scoped roles of each grant.

    ``project_workspaces`` names the workspace of every reachable project, the
    projects of granted workspaces included, as they are when the limit is read.
    """

    project_roles: Mapping[str, frozenset[str]] = field(default_factory=dict)
    workspace_roles: Mapping[str, frozenset[str]] = field(default_factory=dict)
    #: ``{project id: {workflow id: roles}}`` of workflow grants.
    workflow_roles: Mapping[str, Mapping[str, frozenset[str]]] = field(default_factory=dict)
    project_workspaces: Mapping[str, str] = field(default_factory=dict)

    @property
    def project_ids(self) -> frozenset[str]:
        """Every reachable project: granted, in a granted workspace, or holding a granted workflow."""
        return (
            frozenset(self.project_roles)
            | frozenset(project_id for project_id, flows in self.workflow_roles.items() if flows)
            | frozenset(
                project_id
                for project_id, workspace_id in self.project_workspaces.items()
                if workspace_id in self.workspace_roles
            )
        )

    @property
    def workspace_ids(self) -> frozenset[str]:
        """Every visible workspace: granted, or holding a reachable project."""
        reachable = self.project_ids
        return frozenset(self.workspace_roles) | frozenset(
            workspace_id
            for project_id, workspace_id in self.project_workspaces.items()
            if project_id in reachable and workspace_id
        )

    def project_level_roles(self, project_id: str) -> frozenset[str] | None:
        """The roles of the most specific grant that covers the whole project.

        A project grant wins over a workspace grant; ``None`` when neither covers it
        (the project is out of reach, or reached only through workflow grants).
        """
        if project_id in self.project_roles:
            return self.project_roles[project_id]
        workspace_id = self.project_workspaces.get(project_id)
        if workspace_id and workspace_id in self.workspace_roles:
            return self.workspace_roles[workspace_id]
        return None

    def workflow_only_projects(self) -> dict[str, frozenset[str]]:
        """``{project id: workflow ids}`` of projects reached only through workflow grants.

        In such a project only the listed workflows (and their runs) are reachable.
        """
        return {
            project_id: frozenset(workflows)
            for project_id, workflows in self.workflow_roles.items()
            if workflows and self.project_level_roles(project_id) is None
        }

    def workflow_level_roles(self, project_id: str, workflow_id: str) -> frozenset[str] | None:
        """The roles in one workflow of a project: workflow grant, then project, then workspace.

        ``None`` when the workflow is out of reach.
        """
        granted = (self.workflow_roles.get(project_id) or {}).get(workflow_id)
        if granted is not None:
            return granted
        return self.project_level_roles(project_id)


def _rows(cur: Any) -> list[dict[str, Any]]:
    rows = cur.fetchall() or []
    names = [column[0] for column in (cur.description or [])]
    return [
        dict(row) if isinstance(row, Mapping) else dict(zip(names, row, strict=False))
        for row in rows
    ]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _freeze(roles: Mapping[str, set[str]]) -> dict[str, frozenset[str]]:
    return {key: frozenset(values) for key, values in roles.items()}


_GRANTS_SQL = """SELECT ur.user_id::text AS user_id,
                      ur.workspace_id::text AS workspace_id,
                      ur.project_id::text AS project_id,
                      COALESCE(ur.workflow_id, '') AS workflow_id,
                      p.workspace_id::text AS project_workspace_id,
                      r.key AS role_key
                 FROM public.user_roles ur
                 JOIN public.roles r ON r.id = ur.role_id
                 LEFT JOIN public.projects p ON p.id = ur.project_id
                 LEFT JOIN public.workspaces w ON w.id = ur.workspace_id
                 LEFT JOIN public.registry_workflows rw
                   ON rw.account_id = ur.workflow_account_id AND rw.workflow_id = ur.workflow_id
                WHERE ur.user_id::text = ANY(%s::text[])
                  AND r.key = ANY(%s::text[])
                  AND (
                        (ur.project_id IS NOT NULL
                         AND p.account_id::text = %s
                         AND p.deleted_at IS NULL
                         AND (ur.workflow_id IS NULL
                              OR (rw.workflow_id IS NOT NULL AND rw.deleted_at IS NULL
                                  AND EXISTS (
                                        SELECT 1 FROM public.project_workflows pw
                                         WHERE pw.project_id = ur.project_id
                                           AND pw.workflow_id = ur.workflow_id
                                           AND pw.account_id = ur.workflow_account_id
                                           AND pw.account_id = p.account_id
                                           AND pw.deleted_at IS NULL))))
                     OR (ur.project_id IS NULL
                         AND ur.workspace_id IS NOT NULL
                         AND w.account_id::text = %s
                         AND w.deleted_at IS NULL)
                  )"""


class _Builder:
    def __init__(self) -> None:
        self.workspace_roles: dict[str, set[str]] = {}
        self.project_roles: dict[str, set[str]] = {}
        self.workflow_roles: dict[str, dict[str, set[str]]] = {}
        self.project_workspaces: dict[str, str] = {}

    def add(self, row: Mapping[str, Any]) -> None:
        role_key = _text(row.get("role_key"))
        project_id = _text(row.get("project_id"))
        workflow_id = _text(row.get("workflow_id"))
        workspace_id = _text(row.get("workspace_id"))
        if not role_key:
            return
        if project_id:
            self.project_workspaces[project_id] = _text(row.get("project_workspace_id"))
            if workflow_id:
                self.workflow_roles.setdefault(project_id, {}).setdefault(workflow_id, set()).add(
                    role_key
                )
            else:
                self.project_roles.setdefault(project_id, set()).add(role_key)
        elif workspace_id:
            self.workspace_roles.setdefault(workspace_id, set()).add(role_key)

    def build(self, workspace_projects: Mapping[str, Mapping[str, str]]) -> ScopeLimit:
        project_workspaces = dict(self.project_workspaces)
        for workspace_id in self.workspace_roles:
            project_workspaces.update(workspace_projects.get(workspace_id) or {})
        return ScopeLimit(
            project_roles=_freeze(self.project_roles),
            workspace_roles=_freeze(self.workspace_roles),
            workflow_roles={pid: _freeze(flows) for pid, flows in self.workflow_roles.items()},
            project_workspaces=project_workspaces,
        )


def _read_limits(cur: Any, account: str, user_ids: list[str]) -> dict[str, ScopeLimit]:
    builders = {user_id: _Builder() for user_id in user_ids}
    if not builders:
        return {}
    cur.execute(_GRANTS_SQL, (sorted(builders), sorted(SCOPED_ROLE_KEYS), account, account))
    for row in _rows(cur):
        builder = builders.get(_text(row.get("user_id")))
        if builder is not None:
            builder.add(row)
    workspaces = sorted({ws for b in builders.values() for ws in b.workspace_roles})
    workspace_projects: dict[str, dict[str, str]] = {}
    if workspaces:
        # The workspace's projects now: a project created after the grant is covered.
        cur.execute(
            """SELECT p.id::text AS project_id, p.workspace_id::text AS workspace_id
                 FROM public.projects p
                WHERE p.account_id::text = %s
                  AND p.deleted_at IS NULL
                  AND p.workspace_id::text = ANY(%s::text[])""",
            (account, workspaces),
        )
        for row in _rows(cur):
            project_id, workspace_id = _text(row.get("project_id")), _text(row.get("workspace_id"))
            if project_id and workspace_id:
                workspace_projects.setdefault(workspace_id, {})[project_id] = workspace_id
    return {user_id: builder.build(workspace_projects) for user_id, builder in builders.items()}


def load_scope_limit(conn: Any, *, account_id: str, user_id: str) -> ScopeLimit | None:
    """The person's scope limit in ``account_id``; ``None`` when there is no limit row.

    A database error propagates: the caller fails closed. Grants on places of another
    account, on deleted workspaces, projects or workflows are not listed. The
    projects of a granted workspace are read now, so a project created after the
    grant is covered.
    """
    account, user = _text(account_id), _text(user_id)
    if not account or not user:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """SELECT 1 AS present
                 FROM public.scoped_role_users
                WHERE user_id::text = %s
                  AND account_id::text = %s
                LIMIT 1""",
            (user, account),
        )
        if not _rows(cur):
            return None
        return _read_limits(cur, account, [user])[user]


def load_account_scope_limits(conn: Any, *, account_id: str) -> dict[str, ScopeLimit]:
    """``{user id: limit}`` of every limited person of the account (three queries).

    A database error propagates: the caller fails closed.
    """
    account = _text(account_id)
    if not account:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """SELECT user_id::text AS user_id
                 FROM public.scoped_role_users
                WHERE account_id::text = %s""",
            (account,),
        )
        users = sorted({_text(row.get("user_id")) for row in _rows(cur)} - {""})
        return _read_limits(cur, account, users)


__all__ = [
    "CONTAINER_ROLE",
    "SCOPED_ROLE_KEYS",
    "ScopeLimit",
    "load_account_scope_limits",
    "load_scope_limit",
]
