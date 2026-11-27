"""A person moves from company A to company B (ADR 0084, ADR 0085).

Dave works in A. He is invited to B and accepts through the single-use link, then A's
administrator Bob removes him from A. What he held in A ends; what he made in A stays
A's; a run he started finishes as A's; he can be invited back; and a person removed
from their only company signs in nowhere. On the disposable audit database through
Core's authentication middleware and auth routes (see ``tests/db/_companies.py``);
every test runs in a transaction that is rolled back.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from core.application.auth import inbound_oauth_repository
from core.application.auth.inbound_oauth_policy import secret_digest
from core.application.auth.membership_roles import revoke_account_access, set_membership_role
from core.application.management.approval_assignees import (
    ApprovalAssigneeRepository,
    StepApproverRequiredError,
    require_step_approver,
)
from tests.db._companies import (
    APPROVAL_STEP,
    PASSWORD,
    World,
    assign_approver,
    client,
    companies_world,
    invitation,
    job,
    last_scope,
    oauth_grant,
    person,
    personal_token,
    save_workflow,
    session,
    username,
)


@dataclass(frozen=True)
class People:
    bob: str  # admin of A
    carol: str  # admin of B
    dave: str  # operator of A, the person who moves


@pytest.fixture
def world(disposable_db, disposable_db_conn) -> Iterator[World]:
    with companies_world(disposable_db.dsn, disposable_db_conn) as built:
        yield built


@pytest.fixture
def people(world: World) -> People:
    return People(
        bob=person(world, "bob", world.a.id, {world.a.id: "admin"}),
        carol=person(world, "carol", world.b.id, {world.b.id: "admin"}),
        dave=person(world, "dave", world.a.id, {world.a.id: "operator"}),
    )


def _join_b(world: World, people: People, browser) -> None:
    """Dave accepts Carol's invitation to B and moves his browser there."""
    token = invitation(
        world,
        account_id=world.b.id,
        inviter=people.carol,
        email=username(world, people.dave),
        role="operator",
    )
    accepted = browser.post(
        "/api/auth/invitations/accept", json={"token": token, "role": "operator"}
    )
    assert accepted.status_code == 200, accepted.text
    moved = browser.post("/api/auth/switch-account", json={"account_id": world.b.id})
    assert moved.status_code == 200, moved.text


def _remove_from_a(world: World, people: People) -> None:
    bobs = client(world, cookie=session(world, people.bob, world.a.id))
    removed = bobs.delete(f"/api/auth/users/{people.dave}/membership")
    assert removed.status_code == 200, removed.text


def _approvers(world: World) -> tuple[str, ...]:
    """The assigned approvers of A's approval step."""
    return ApprovalAssigneeRepository(world.store.conn, account_id=world.a.id).list_approver_ids(
        world.workflow, APPROVAL_STEP
    )


def _operator_cannot_decide(world: World, operator: str) -> None:
    """An operator of A holds approvals.decide by role, yet is no approver of the step."""
    operators = client(world, cookie=session(world, operator, world.a.id))
    operators.get("/api/projects")
    assert "approvals.decide" in last_scope(operators).permissions
    with pytest.raises(StepApproverRequiredError):
        require_step_approver(
            world.store.conn,
            last_scope(operators),
            workflow_ids=[world.workflow],
            step_id=APPROVAL_STEP,
            account_id=world.a.id,
        )


def _company_service_account(world: World, created_by: str) -> tuple[str, str]:
    """A service account of A and its machine token, set up by ``created_by``."""
    account_row = world.conn.execute(
        """INSERT INTO service_accounts (account_id, service_account_key, display_name,
                                         scopes_jsonb, created_by)
           VALUES (%s, %s, 'Nightly import', '["projects:read"]'::jsonb, %s) RETURNING id""",
        (world.a.id, f"nightly-{uuid.uuid4().hex[:8]}", created_by),
    ).fetchone()
    token_row = world.conn.execute(
        """INSERT INTO machine_tokens (service_account_id, account_id, label, token_hash,
                                       created_by)
           VALUES (%s, %s, 'import', %s, %s) RETURNING id""",
        (
            account_row[0],
            world.a.id,
            hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            created_by,
        ),
    ).fetchone()
    return str(account_row[0]), str(token_row[0])


# --- B1: invited to B, accepted, then removed from A ---------------------------------------


def test_a_person_who_leaves_a_loses_everything_they_held_there(
    world: World, people: People
) -> None:
    dave = people.dave
    browser = client(world, cookie=session(world, dave, world.a.id))
    other_device = client(world, cookie=session(world, dave, world.a.id))
    token_a = client(world, bearer=personal_token(world, dave, world.a.id))
    grant_a, oauth_token_a = oauth_grant(world, dave, world.a.id)
    service_account, machine_token = _company_service_account(world, username(world, dave))

    _join_b(world, people, browser)
    _remove_from_a(world, people)

    # Every session, personal token and OAuth grant of A is gone.
    assert other_device.get("/api/projects").status_code == 401
    assert token_a.get("/api/projects").status_code == 401
    assert (
        inbound_oauth_repository.resolve_access_token(
            world.store.conn, secret_digest(oauth_token_a), datetime.now(UTC)
        )
        is None
    )
    revoked_reason = world.conn.execute(
        "SELECT revoked_reason FROM oauth_grants WHERE id = %s", (grant_a,)
    ).fetchone()[0]
    assert revoked_reason == "account_membership_removed"

    # The browser moved to B keeps working there, and reads nothing of A.
    assert set(browser.get("/api/projects").json()["projects"]) == {
        world.b.project,
        world.b.second_project,
    }
    assert browser.get(f"/api/projects/{world.a.project}/jobs").status_code == 403
    back = browser.post("/api/auth/switch-account", json={"account_id": world.a.id})
    assert back.status_code == 403

    # B is the home and the default company now.
    home = world.conn.execute(
        "SELECT account_id::text FROM users WHERE id = %s", (dave,)
    ).fetchone()
    defaults = world.conn.execute(
        "SELECT account_id::text FROM user_account_memberships WHERE user_id = %s AND is_default",
        (dave,),
    ).fetchall()
    assert home[0] == world.b.id
    assert [row[0] for row in defaults] == [world.b.id]

    # A service account belongs to the company, not to whoever set it up (ADR 0085 §4):
    # A's nightly import keeps its credential and acts with its own scopes.
    active, revoked = world.conn.execute(
        """SELECT s.is_active, t.revoked FROM service_accounts s
             JOIN machine_tokens t ON t.service_account_id = s.id
            WHERE s.id = %s AND t.id = %s""",
        (service_account, machine_token),
    ).fetchone()
    assert (active, revoked) == (True, False)


def test_the_new_home_is_a_company_where_the_person_has_a_role(
    world: World, people: People
) -> None:
    # Dave also has a membership of C without a role (a directory took it away).
    world.conn.execute(
        """INSERT INTO user_account_memberships (user_id, account_id, is_default, role_id)
           VALUES (%s, %s, false, NULL)""",
        (people.dave, world.c.id),
    )
    world.conn.execute(
        """INSERT INTO user_account_memberships (user_id, account_id, is_default, role_id)
           VALUES (%s, %s, false, (SELECT id FROM roles WHERE key = 'operator'))""",
        (people.dave, world.b.id),
    )

    _remove_from_a(world, people)

    home = world.conn.execute(
        "SELECT account_id::text FROM users WHERE id = %s", (people.dave,)
    ).fetchone()[0]
    assert home == world.b.id


# --- B2: what stays in A --------------------------------------------------------------------


def test_what_the_person_made_in_a_stays_as_and_their_approvals_are_released(
    world: World, people: People
) -> None:
    dave, name = people.dave, username(world, people.dave)
    flow = f"daves_flow_{uuid.uuid4().hex[:8]}"
    save_workflow(world.registry, world.a.id, flow, "Dave's flow", author=name)
    finished = job(world, world.a, status="succeeded", started_by=name)
    schedule = world.conn.execute(
        """INSERT INTO scheduled_jobs (project_id, workflow_id, cron_expr, account_id, created_by,
                                       next_run_at, name)
           VALUES (%s, %s, '0 6 * * *', %s, %s, now() + interval '1 day', 'Morning run')
           RETURNING id""",
        (world.a.project, world.a.workflow, world.a.id, name),
    ).fetchone()[0]
    assign_approver(world, world.a, dave)
    waiting = job(world, world.a, status="awaiting_approval")
    # An operator of A, who holds approvals.decide but is no assigned approver.
    operator = person(world, "olga", world.a.id, {world.a.id: "operator"})
    browser = client(world, cookie=session(world, dave, world.a.id))
    _join_b(world, people, browser)

    bobs = client(world, cookie=session(world, people.bob, world.a.id))
    bobs.get("/api/projects")
    bob_scope = last_scope(bobs)
    # While Dave is A's assigned approver, nobody else decides that step.
    with pytest.raises(StepApproverRequiredError):
        require_step_approver(
            world.store.conn,
            bob_scope,
            workflow_ids=[world.workflow],
            step_id=APPROVAL_STEP,
            account_id=world.a.id,
        )

    _remove_from_a(world, people)

    # The workflow, its history and the runs stay A's, with Dave's name on them.
    assert world.registry.get_workflow(flow, account_id=world.a.id) is not None
    assert world.registry.get_workflow(flow, account_id=world.b.id) is None
    authors = world.conn.execute(
        """SELECT DISTINCT author, account_id::text FROM registry_changelog
            WHERE entity_type = 'workflow' AND entity_id = %s""",
        (flow,),
    ).fetchall()
    assert authors == [(name, world.a.id)]
    run = world.conn.execute(
        "SELECT account_id::text, status, meta->>'started_by' FROM jobs WHERE id = %s",
        (finished,),
    ).fetchone()
    assert run == (world.a.id, "succeeded", name)
    # The schedule stays A's and keeps running: a due run is started by the schedule,
    # never with the authority of the person who set it up.
    kept = world.conn.execute(
        "SELECT account_id::text, enabled, status, created_by FROM scheduled_jobs WHERE id = %s",
        (schedule,),
    ).fetchone()
    assert kept == (world.a.id, True, "active", name)

    # The approval Dave was the only approver of goes to A's administrators, not to
    # everyone who may decide approvals.
    assert _approvers(world) == (people.bob,)
    assert require_step_approver(
        world.store.conn,
        bob_scope,
        workflow_ids=[world.workflow],
        step_id=APPROVAL_STEP,
        account_id=world.a.id,
    ) == frozenset({people.bob})
    assert bobs.get("/api/approvals").json()["jobs"] == [{"id": waiting, "can_decide": True}]
    _operator_cannot_decide(world, operator)
    removal = next(entry for entry in world.audit if entry["action"] == "account_member_remove")
    assert removal["detail"]["released_approval_steps"] == [
        {
            "account_id": world.a.id,
            "workflow_id": world.workflow,
            "step_id": APPROVAL_STEP,
            "handed_to_admins": [people.bob],
        }
    ]


def test_another_assigned_approver_keeps_the_step(world: World, people: People) -> None:
    erin = person(world, "erin", world.a.id, {world.a.id: "operator"})
    world.conn.execute(
        """INSERT INTO user_account_memberships (user_id, account_id, is_default, role_id)
           VALUES (%s, %s, false, (SELECT id FROM roles WHERE key = 'operator'))""",
        (people.dave, world.b.id),
    )
    assign_approver(world, world.a, people.dave)
    assign_approver(world, world.a, erin)

    _remove_from_a(world, people)

    assert ApprovalAssigneeRepository(world.store.conn, account_id=world.a.id).list_approver_ids(
        world.workflow, APPROVAL_STEP
    ) == (erin,)


# --- B3: removed while a run of theirs is running -------------------------------------------


def test_a_running_job_stays_as_and_its_result_is_out_of_reach(
    world: World, people: People
) -> None:
    dave, name = people.dave, username(world, people.dave)
    running = job(world, world.a, status="running", started_by=name)
    old_session = client(world, cookie=session(world, dave, world.a.id))
    browser = client(world, cookie=session(world, dave, world.a.id))
    _join_b(world, people, browser)

    _remove_from_a(world, people)

    # Nothing about the run changes: it belongs to A and finishes there.
    assert world.conn.execute(
        "SELECT account_id::text, status FROM jobs WHERE id = %s", (running,)
    ).fetchone() == (world.a.id, "running")
    bobs = client(world, cookie=session(world, people.bob, world.a.id))
    seen_by_a = bobs.get(f"/api/projects/{world.a.project}/jobs").json()["jobs"]
    assert [row["id"] for row in seen_by_a] == [running]
    # Dave reads it from nowhere.
    assert old_session.get(f"/api/projects/{world.a.project}/jobs").status_code == 401
    assert browser.get(f"/api/projects/{world.a.project}/jobs").status_code == 403


# --- B4: invited back to A ------------------------------------------------------------------


def test_a_person_invited_back_gets_a_fresh_membership_and_old_tokens_stay_revoked(
    world: World, people: People
) -> None:
    dave = people.dave
    old_token = client(world, bearer=personal_token(world, dave, world.a.id))
    old_session = client(world, cookie=session(world, dave, world.a.id))
    assign_approver(world, world.a, dave)
    browser = client(world, cookie=session(world, dave, world.a.id))
    _join_b(world, people, browser)
    _remove_from_a(world, people)

    back = invitation(
        world, account_id=world.a.id, inviter=people.bob, email=username(world, dave), role="viewer"
    )
    accepted = browser.post("/api/auth/invitations/accept", json={"token": back, "role": "viewer"})
    assert accepted.status_code == 200, accepted.text

    role = world.conn.execute(
        """SELECT r.key FROM user_account_memberships m JOIN roles r ON r.id = m.role_id
            WHERE m.user_id = %s AND m.account_id = %s""",
        (dave, world.a.id),
    ).fetchone()[0]
    assert role == "viewer"
    assert (
        browser.post("/api/auth/switch-account", json={"account_id": world.a.id}).status_code == 200
    )
    run = browser.post(f"/api/workflows/{world.workflow}/run", json={"project_id": world.a.project})
    assert run.status_code == 403
    assert browser.get(f"/api/projects/{world.a.project}/jobs").status_code == 200
    # What was revoked stays revoked; the old assignment does not come back.
    assert old_token.get("/api/projects").status_code == 401
    assert old_session.get("/api/projects").status_code == 401
    assert _approvers(world) == (people.bob,)


# --- B5: removed from their only company ----------------------------------------------------


def _sign_in(world: World, user_id: str):
    return client(world).post(
        "/api/auth/login", json={"email": username(world, user_id), "password": PASSWORD}
    )


def test_a_person_with_one_company_is_deleted_not_removed_and_then_signs_in_nowhere(
    world: World, people: People
) -> None:
    dave = people.dave
    bobs = client(world, cookie=session(world, people.bob, world.a.id))
    token = client(world, bearer=personal_token(world, dave, world.a.id))
    browser = client(world, cookie=session(world, dave, world.a.id))
    assign_approver(world, world.a, dave)

    only = bobs.delete(f"/api/auth/users/{dave}/membership")
    assert only.status_code == 409
    assert "only company" in only.json()["error"]
    assert _sign_in(world, dave).status_code == 200

    deleted = bobs.delete(f"/api/auth/users/{dave}")
    assert deleted.status_code == 200, deleted.text

    refused = _sign_in(world, dave)
    assert refused.status_code == 401
    assert refused.json() == {"error": "Invalid credentials"}
    assert token.get("/api/projects").status_code == 401
    assert browser.get(f"/api/projects/{world.a.project}/jobs").status_code == 401
    # A deleted person is soft-deleted; their approval step goes to A's administrators.
    assert _approvers(world) == (people.bob,)


def test_a_person_without_a_role_anywhere_is_told_whom_to_ask(world: World, people: People) -> None:
    dave = people.dave
    browser = client(world, cookie=session(world, dave, world.a.id))
    # What a directory sync does when it takes the role away (ADR 0085, E).
    with world.conn.cursor() as cur:
        set_membership_role(cur, user_id=dave, account_id=world.a.id, role=None)
        revoke_account_access(cur, user_id=dave, account_id=world.a.id, reason="role_removed")

    refused = _sign_in(world, dave)
    assert refused.status_code == 403
    assert refused.json() == {
        "error": "You have no role in this company. Ask its administrator.",
        "code": "no_role_in_account",
    }
    assert browser.get("/api/projects").status_code == 401


# --- a demotion leaves the approval steps like leaving does ---------------------------------


def test_a_demotion_that_stops_deciding_hands_the_step_to_the_administrators(
    world: World, people: People
) -> None:
    operator = person(world, "olga", world.a.id, {world.a.id: "operator"})
    assign_approver(world, world.a, people.dave)
    bobs = client(world, cookie=session(world, people.bob, world.a.id))

    demoted = bobs.put(f"/api/auth/users/{people.dave}", json={"role": "viewer"})
    assert demoted.status_code == 200, demoted.text

    assert _approvers(world) == (people.bob,)
    _operator_cannot_decide(world, operator)
    # Promoted again later, the old assignment does not come back.
    assert bobs.put(f"/api/auth/users/{people.dave}", json={"role": "operator"}).status_code == 200
    assert _approvers(world) == (people.bob,)


def test_a_demotion_with_other_approvers_only_takes_the_person_off(
    world: World, people: People
) -> None:
    erin = person(world, "erin", world.a.id, {world.a.id: "operator"})
    assign_approver(world, world.a, people.dave)
    assign_approver(world, world.a, erin)

    with world.conn.cursor() as cur:
        set_membership_role(cur, user_id=people.dave, account_id=world.a.id, role=None)

    assert _approvers(world) == (erin,)


def test_a_change_that_keeps_deciding_keeps_the_assignment(world: World, people: People) -> None:
    assign_approver(world, world.a, people.dave)
    bobs = client(world, cookie=session(world, people.bob, world.a.id))

    promoted = bobs.put(f"/api/auth/users/{people.dave}", json={"role": "admin"})
    assert promoted.status_code == 200, promoted.text

    assert _approvers(world) == (people.dave,)


# --- service accounts set up by someone who left ---------------------------------------------


def test_the_listing_marks_credentials_set_up_by_a_person_who_left(
    world: World, people: People
) -> None:
    dave_set_up, dave_token = _company_service_account(world, username(world, people.dave))
    system_set_up, _ = _company_service_account(world, "system")
    bob_set_up, _ = _company_service_account(world, username(world, people.bob))

    def marks() -> dict[str, bool]:
        listed = world.store.list_service_accounts(account_id=world.a.id, include_inactive=True)
        return {row["id"]: row["created_by_has_left"] for row in listed}

    assert marks() == {dave_set_up: False, system_set_up: False, bob_set_up: False}
    browser = client(world, cookie=session(world, people.dave, world.a.id))
    _join_b(world, people, browser)
    _remove_from_a(world, people)

    # Kept and working, but the administrator sees who no longer answers for it.
    assert marks() == {dave_set_up: True, system_set_up: False, bob_set_up: False}
    key = world.conn.execute(
        "SELECT service_account_key FROM service_accounts WHERE id = %s", (dave_set_up,)
    ).fetchone()[0]
    tokens = world.store.list_service_account_tokens(
        key, account_id=world.a.id, include_revoked=True
    )
    assert [(row["id"], row["created_by_has_left"], row["revoked"]) for row in tokens] == [
        (dave_token, True, False)
    ]
