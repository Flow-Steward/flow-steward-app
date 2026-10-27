"""Two-account, two-project fixtures for isolation tests.

Remediation epic 2026-07-29, WS-01/WS-02. Requirement 10 asks for at least two
accounts and two projects in every meaningful isolation test, and requires that a
cross-account read or write returns a safe 404/403 **without revealing that the
resource exists**.

Deliberate design choices:

- **Overlapping names.** Both accounts get a project called ``Shared Name`` and a
  workspace called ``Shared Workspace``. A scoping bug that resolves by name
  instead of by id passes a two-tenant test with distinct names and fails this
  one, which is the whole point.
- **Realistic ids.** Nothing is hardcoded to `DEFAULT_ACCOUNT_ID`. A row that
  falls back to the default account is a defect
  (see `CLAUDE-P2-01`, `CLAUDE-P3-02`), so the fixture must not accidentally
  supply it.
- **Rows, not mocks.** These go into the disposable canonical PostgreSQL, so the
  predicates being tested are real SQL predicates.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

TENANT_ID = "00000000-0000-0000-0000-000000000001"

#: Both accounts own an entity with each of these names, so any resolver that
#: keys on a human name rather than an id collides across the tenant boundary.
SHARED_PROJECT_NAME = "Shared Name"
SHARED_WORKSPACE_NAME = "Shared Workspace"


@dataclass(frozen=True)
class TenantProject:
    id: str
    name: str


@dataclass(frozen=True)
class Tenant:
    account_id: str
    account_key: str
    workspace_id: str
    projects: dict[str, TenantProject] = field(default_factory=dict)

    @property
    def shared(self) -> TenantProject:
        """The project whose name is duplicated in the other account."""
        return self.projects["shared"]

    @property
    def private(self) -> TenantProject:
        """The project whose name is unique to this account."""
        return self.projects["private"]


@dataclass(frozen=True)
class TwoTenantFixture:
    a: Tenant
    b: Tenant

    def other(self, tenant: Tenant) -> Tenant:
        return self.b if tenant is self.a else self.a


def _new_id() -> str:
    return str(uuid.uuid4())


def _create_tenant(conn, *, account_key: str, private_project_name: str) -> Tenant:
    account_id = _new_id()
    workspace_id = _new_id()
    projects: dict[str, TenantProject] = {}

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO accounts (id, tenant_id, account_key, display_name, is_active)
            VALUES (%s, %s, %s, %s, true)
            """,
            (account_id, TENANT_ID, account_key, f"Account {account_key}"),
        )
        cur.execute(
            """
            INSERT INTO workspaces
                (id, tenant_id, account_id, workspace_key, display_name)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                workspace_id,
                TENANT_ID,
                account_id,
                f"ws-{account_key}",
                SHARED_WORKSPACE_NAME,
            ),
        )
        for slug, name in (
            ("shared", SHARED_PROJECT_NAME),
            ("private", private_project_name),
        ):
            project_id = _new_id()
            cur.execute(
                """
                INSERT INTO projects (id, tenant_id, workspace_id, name, description)
                VALUES (%s, %s, %s, %s, '')
                """,
                (project_id, TENANT_ID, workspace_id, name),
            )
            projects[slug] = TenantProject(id=project_id, name=name)

    return Tenant(
        account_id=account_id,
        account_key=account_key,
        workspace_id=workspace_id,
        projects=projects,
    )


def create_two_tenants(conn) -> TwoTenantFixture:
    """Create two fully separate accounts, each with two projects."""
    suffix = uuid.uuid4().hex[:8]
    return TwoTenantFixture(
        a=_create_tenant(
            conn,
            account_key=f"tenant-a-{suffix}",
            private_project_name=f"Account A Only {suffix}",
        ),
        b=_create_tenant(
            conn,
            account_key=f"tenant-b-{suffix}",
            private_project_name=f"Account B Only {suffix}",
        ),
    )


def delete_two_tenants(conn, fixture: TwoTenantFixture) -> None:
    """Remove every row the fixture created, children first."""
    with conn.cursor() as cur:
        for tenant in (fixture.a, fixture.b):
            cur.execute(
                "DELETE FROM projects WHERE id = ANY(%s)",
                ([project.id for project in tenant.projects.values()],),
            )
            cur.execute("DELETE FROM workspaces WHERE id = %s", (tenant.workspace_id,))
            cur.execute("DELETE FROM accounts WHERE id = %s", (tenant.account_id,))


__all__ = [
    "SHARED_PROJECT_NAME",
    "SHARED_WORKSPACE_NAME",
    "TENANT_ID",
    "Tenant",
    "TenantProject",
    "TwoTenantFixture",
    "create_two_tenants",
    "delete_two_tenants",
]
