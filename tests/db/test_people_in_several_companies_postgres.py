"""One person, several companies: the role, the data and the tokens of the acting company.

ADR 0084 (accounts), ADR 0085 (roles per account), ADR 0076 §8b (scoped access) and
ADR 0082 §9 (installation administrator), on the disposable audit database through
Core's authentication middleware and auth routes (see ``tests/db/_companies.py``).
Every test runs in a transaction that is rolled back.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from core.application.auth import inbound_oauth_repository
from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.auth.inbound_oauth_policy import secret_digest
from core.application.auth.installation_admin import is_installation_admin
from core.application.auth.user_admin.command_service import UserAdminCommandService
from core.application.auth.user_admin.contracts import UserRolesUpdateInput
from core.application.management.approval_assignees import (
    StepApproverRequiredError,
    require_step_approver,
)
from core.application.management.approval_ports import ApprovalAssignmentsPort
from core.application.management.errors import ServiceConflictError
from core.application.management.trash_account_scope import trash_account_predicate
from core.application.registry.workflow_account import workflow_account_scope
from tests.db._companies import (
    APPROVAL_STEP,
    PASSWORD,
    World,
    assign_approver,
    client,
    companies_world,
    job,
    last_scope,
    limit_to_project,
    oauth_grant,
    person,
    personal_token,
    save_workflow,
    session,
    username,
)


@pytest.fixture
def world(disposable_db, disposable_db_conn) -> Iterator[World]:
    with companies_world(disposable_db.dsn, disposable_db_conn) as built:
        yield built


def _switch(browser, account_id: str):
    return browser.post("/api/auth/switch-account", json={"account_id": account_id})


def _listed_people(browser) -> set[str]:
    response = browser.get("/api/auth/users")
    assert response.status_code == 200, response.text
    return {user["id"] for user in response.json()["users"]}


def _run(browser, world: World, project_id: str):
    return browser.post(f"/api/workflows/{world.workflow}/run", json={"project_id": project_id})


# --- A1: an administrator of A and B --------------------------------------------------------


def test_an_admin_of_two_companies_signs_in_to_one_and_switches_to_the_other(
    world: World,
) -> None:
    alice = person(world, "alice", world.a.id, {world.a.id: "admin", world.b.id: "admin"})
    carol = person(world, "carol", world.c.id, {world.c.id: "admin"})
    bob = person(world, "bob", world.a.id, {world.a.id: "operator"})
    erin = person(world, "erin", world.b.id, {world.b.id: "viewer"})
    browser = client(world)

    # With a role in two companies the sign-in asks which one; C is never offered.
    choose = browser.post(
        "/api/auth/login", json={"email": username(world, alice), "password": PASSWORD}
    )
    assert choose.status_code == 409, choose.text
    offered = {item["account_id"] for item in choose.json()["account_options"]}
    assert offered == {world.a.id, world.b.id}

    signed_in = browser.post(
        "/api/auth/login",
        json={"email": username(world, alice), "password": PASSWORD, "account_id": world.a.id},
    )
    assert signed_in.status_code == 200, signed_in.text
    assert signed_in.json()["user"]["account_id"] == world.a.id

    # In A: A's projects, A's people.
    assert set(browser.get("/api/projects").json()["projects"]) == {
        world.a.project,
        world.a.second_project,
    }
    assert _listed_people(browser) == {alice, bob}

    # Switched to B: only B's projects, jobs, workflows and people.
    assert _switch(browser, world.b.id).status_code == 200
    assert set(browser.get("/api/projects").json()["projects"]) == {
        world.b.project,
        world.b.second_project,
    }
    job_a, job_b = job(world, world.a, status="succeeded"), job(world, world.b, status="succeeded")
    assert browser.get(f"/api/projects/{world.a.project}/jobs").status_code == 403
    jobs_b = browser.get(f"/api/projects/{world.b.project}/jobs").json()["jobs"]
    assert [row["id"] for row in jobs_b] == [job_b] and job_a not in str(jobs_b)
    acting = last_scope(browser).account_id
    assert acting == world.b.id
    shown = world.registry.get_workflow(world.workflow, account_id=acting)
    assert shown is not None and shown["display_name"] == "Invoices of B"
    assert _listed_people(browser) == {alice, erin}

    # C is never reachable: no switch, no user management there.
    assert _switch(browser, world.c.id).status_code == 403
    assert browser.put(f"/api/auth/users/{carol}", json={"role": "viewer"}).status_code == 403
    assert browser.delete(f"/api/auth/users/{carol}/membership").status_code == 404


def test_an_admin_of_companies_is_installation_admin_only_acting_in_the_installation_account(
    world: World,
) -> None:
    world.conn.execute(
        """INSERT INTO accounts (id, tenant_id, account_key, display_name, is_active)
           VALUES (%s, '00000000-0000-0000-0000-000000000001', 'default', 'Default', true)
           ON CONFLICT DO NOTHING""",
        (DEFAULT_ACCOUNT_ID,),
    )
    alice = person(world, "alice", world.a.id, {world.a.id: "admin", world.b.id: "admin"})
    dana = person(world, "dana", world.a.id, {world.a.id: "admin", DEFAULT_ACCOUNT_ID: "operator"})
    browser = client(world, cookie=session(world, alice, world.a.id))
    browser.get("/api/projects")
    assert not is_installation_admin(last_scope(browser))
    _switch(browser, world.b.id)
    browser.get("/api/projects")
    assert not is_installation_admin(last_scope(browser))

    # An operator of the installation account is an operator there, not its administrator.
    other = client(world, cookie=session(world, dana, world.a.id))
    assert _switch(other, DEFAULT_ACCOUNT_ID).status_code == 200
    other.get("/api/projects")
    assert not is_installation_admin(last_scope(other))

    # Admin of the installation account, acting there: the installation administrator.
    world.conn.execute(
        """INSERT INTO user_account_memberships (user_id, account_id, is_default, role_id)
           VALUES (%s, %s, false, (SELECT id FROM roles WHERE key = 'admin'))""",
        (alice, DEFAULT_ACCOUNT_ID),
    )
    assert _switch(browser, DEFAULT_ACCOUNT_ID).status_code == 200
    browser.get("/api/projects")
    assert is_installation_admin(last_scope(browser))


# --- A2: operator in A and C, viewer in B ---------------------------------------------------


def test_the_role_is_the_acting_companys_and_changes_on_switch(world: World) -> None:
    analyst = person(
        world,
        "analyst",
        world.a.id,
        {world.a.id: "operator", world.b.id: "viewer", world.c.id: "operator"},
    )
    browser = client(world, cookie=session(world, analyst, world.a.id))

    assert _run(browser, world, world.a.project).status_code == 200
    # Acting in A, B's project is another company's, whatever the role in B.
    assert _run(browser, world, world.b.project).status_code == 403

    assert _switch(browser, world.b.id).status_code == 200
    refused = _run(browser, world, world.b.project)
    assert refused.status_code == 403
    assert refused.json()["error"] == "Operator role required"
    assert browser.get(f"/api/projects/{world.b.project}/jobs").status_code == 200

    assert _switch(browser, world.c.id).status_code == 200
    assert _run(browser, world, world.c.project).status_code == 200
    assert _run(browser, world, world.a.project).status_code == 403


def test_history_trash_and_search_show_the_acting_company_only(world: World) -> None:
    analyst = person(
        world,
        "analyst",
        world.a.id,
        {world.a.id: "operator", world.b.id: "viewer", world.c.id: "operator"},
    )
    for company, name in ((world.a, "A second"), (world.b, "B second")):
        save_workflow(world.registry, company.id, world.workflow, name, author="setup")
    for company in (world.a, world.b):
        with workflow_account_scope(company.id):
            world.registry.delete_workflow(world.workflow, author="setup", account_id=company.id)
    browser = client(world, cookie=session(world, analyst, world.a.id))

    for company in (world.a, world.b, world.c):
        assert _switch(browser, company.id).status_code == 200
        browser.get("/api/projects")
        acting = last_scope(browser).account_id
        assert acting == company.id
        history = world.registry.get_entity_history("workflow", world.workflow, account_id=acting)
        assert {str(entry["account_id"]) for entry in history} == {company.id}
        hits = world.registry.search_entities(
            world.workflow, entity_type="workflow", account_id=acting
        )
        assert {str(hit["account_id"]) for hit in hits} <= {company.id}
        clause, params = trash_account_predicate(
            has_account_id=True,
            has_project_id=False,
            has_workspace_id=False,
            account_id=acting,
            installation_account=False,
        )
        trashed = world.conn.execute(
            "SELECT account_id::text FROM registry_workflows "
            f"WHERE deleted_at IS NOT NULL AND workflow_key = %s AND {clause}",
            (world.workflow, *params),
        ).fetchall()
        assert [row[0] for row in trashed] == ([company.id] if company is not world.c else [])


# --- A3: limited to one project in A, full operator in C -----------------------------------


def test_a_project_limit_applies_only_in_the_company_that_set_it(world: World) -> None:
    analyst = person(world, "analyst", world.a.id, {world.a.id: "operator", world.c.id: "operator"})
    limit_to_project(world, world.a.id, analyst, world.a.project)
    browser = client(world, cookie=session(world, analyst, world.a.id))

    assert browser.get("/api/projects").json()["projects"] == [world.a.project]
    assert browser.get(f"/api/projects/{world.a.project}/jobs").status_code == 200
    assert browser.get(f"/api/projects/{world.a.second_project}/jobs").status_code == 403
    assert _run(browser, world, world.a.project).status_code == 200
    assert _run(browser, world, world.a.second_project).status_code == 403

    assert _switch(browser, world.c.id).status_code == 200
    assert set(browser.get("/api/projects").json()["projects"]) == {
        world.c.project,
        world.c.second_project,
    }
    assert browser.get(f"/api/projects/{world.c.second_project}/jobs").status_code == 200
    assert _run(browser, world, world.c.second_project).status_code == 200


# --- A4: tokens are bound to the company they were made in ---------------------------------


def test_a_personal_token_made_in_a_never_reads_b(world: World) -> None:
    alice = person(world, "alice", world.a.id, {world.a.id: "admin", world.b.id: "admin"})
    erin = person(world, "erin", world.b.id, {world.b.id: "viewer"})
    token_a = personal_token(world, alice, world.a.id)
    api = client(world, bearer=token_a)

    assert set(api.get("/api/projects").json()["projects"]) == {
        world.a.project,
        world.a.second_project,
    }
    assert api.get(f"/api/projects/{world.b.project}/jobs").status_code == 403
    # The token manages A's people only, though its owner administers B too.
    listed = {user["id"] for user in api.get("/api/auth/users").json()["users"]}
    assert erin not in listed and alice in listed
    # The token acts in A, where Erin is no member: B's people are out of its reach.
    assert api.delete(f"/api/auth/users/{erin}/membership").status_code == 404
    assert (
        world.conn.execute(
            "SELECT count(*) FROM user_account_memberships WHERE user_id = %s", (erin,)
        ).fetchone()[0]
        == 1
    )

    # Switching the browser session to B does not move the token.
    browser = client(world, cookie=session(world, alice, world.a.id))
    assert _switch(browser, world.b.id).status_code == 200
    api.get("/api/projects")
    assert last_scope(api).account_id == world.a.id
    assert api.get(f"/api/projects/{world.b.project}/jobs").status_code == 403


def test_an_oauth_grant_made_in_a_answers_for_a_only(world: World) -> None:
    alice = person(world, "alice", world.a.id, {world.a.id: "admin", world.b.id: "viewer"})
    grant_a, token_a = oauth_grant(world, alice, world.a.id)
    browser = client(world, cookie=session(world, alice, world.a.id))

    def resolved() -> dict | None:
        return inbound_oauth_repository.resolve_access_token(
            world.store.conn, secret_digest(token_a), datetime.now(UTC)
        )

    row = resolved()
    assert row is not None
    assert (str(row["account_id"]), list(row["roles"])) == (world.a.id, ["admin"])

    assert _switch(browser, world.b.id).status_code == 200
    row = resolved()
    assert row is not None and str(row["account_id"]) == world.a.id
    assert str(row["grant_id"]) == grant_a


# --- A5: the last administrator is kept per company ----------------------------------------


def test_the_last_admin_rule_is_per_company(world: World) -> None:
    alice = person(world, "alice", world.a.id, {world.a.id: "admin", world.b.id: "admin"})
    carol = person(world, "carol", world.b.id, {world.b.id: "admin"})

    # A has no other administrator: the CLI (no acting person) is refused.
    service = UserAdminCommandService(get_logger=lambda: world.store)
    with pytest.raises(ServiceConflictError, match="last active administrator"):
        service.set_roles(
            UserRolesUpdateInput(user_id=alice, role="viewer", role_account_id=world.a.id)
        )
    # Nobody else reaches A's administrator either: Carol administers B only.
    carols = client(world, cookie=session(world, carol, world.b.id))
    demote_in_a = carols.put(
        f"/api/auth/users/{alice}", json={"role": "viewer", "role_account_id": world.a.id}
    )
    assert demote_in_a.status_code == 403
    # Alice never changes her own role.
    own = client(world, cookie=session(world, alice, world.a.id))
    assert own.put(f"/api/auth/users/{alice}", json={"role": "viewer"}).status_code in {403, 409}

    # In B, where Carol is an administrator too, Carol demotes Alice.
    demote_in_b = carols.put(f"/api/auth/users/{alice}", json={"role": "viewer"})
    assert demote_in_b.status_code == 200, demote_in_b.text
    roles = dict(
        world.conn.execute(
            """SELECT m.account_id::text, r.key FROM user_account_memberships m
                 JOIN roles r ON r.id = m.role_id WHERE m.user_id = %s""",
            (alice,),
        ).fetchall()
    )
    assert roles == {world.a.id: "admin", world.b.id: "viewer"}
    # The demotion revoked only B's session of Alice.
    assert own.get("/api/projects").status_code == 200


# --- A6: approvals of the acting company ---------------------------------------------------


def test_an_assigned_approver_sees_and_decides_the_acting_companys_approvals_only(
    world: World,
) -> None:
    # Pat lives in B and approves in A; Bob administers A.
    pat = person(world, "pat", world.b.id, {world.a.id: "operator", world.b.id: "operator"})
    bob = person(world, "bob", world.a.id, {world.a.id: "admin"})
    assign_approver(world, world.a, pat)
    waiting_a = job(world, world.a, status="awaiting_approval")
    waiting_b = job(world, world.b, status="awaiting_approval")
    browser = client(world, cookie=session(world, pat, world.a.id))

    in_a = browser.get("/api/approvals").json()["jobs"]
    assert in_a == [{"id": waiting_a, "can_decide": True}]
    scope_a = last_scope(browser)
    assert require_step_approver(
        world.store.conn,
        scope_a,
        workflow_ids=[world.workflow],
        step_id=APPROVAL_STEP,
        account_id=world.a.id,
    ) == frozenset({pat})

    assert _switch(browser, world.b.id).status_code == 200
    in_b = browser.get("/api/approvals").json()["jobs"]
    assert in_b == [{"id": waiting_b, "can_decide": True}]

    # Bob is no assigned approver of A's step.
    bobs = client(world, cookie=session(world, bob, world.a.id))
    assert bobs.get("/api/approvals").json()["jobs"] == [{"id": waiting_a, "can_decide": False}]
    with pytest.raises(StepApproverRequiredError):
        require_step_approver(
            world.store.conn,
            last_scope(bobs),
            workflow_ids=[world.workflow],
            step_id=APPROVAL_STEP,
            account_id=world.a.id,
        )


def test_the_approver_list_names_a_member_who_lives_in_another_company(world: World) -> None:
    """Approvers are people of the account by membership (ADR 0085 §6), not by home."""
    pat = person(world, "pat", world.b.id, {world.a.id: "operator", world.b.id: "operator"})
    assign_approver(world, world.a, pat)

    @contextmanager
    def connection():
        with world.conn.transaction():
            yield world.store.conn

    listed = ApprovalAssignmentsPort(connection).list_approvers(
        world.workflow, APPROVAL_STEP, account_id=world.a.id
    )
    assert [item["user_id"] for item in listed] == [pat]
