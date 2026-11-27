"""Read side and module ports of assigned approvers and the approval journal (ADR 0082)."""

from __future__ import annotations

import ast
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.approval_assignees import (
    StepApproverRequiredError,
    job_workflow_ids,
    require_step_approver,
)
from core.application.management.approval_journal import require_journal_retention_entitled
from core.application.management.approval_ports import (
    ApprovalAssignmentsPort,
    ApprovalJournalPort,
)
from core.application.management.query_services import ApprovalQueryService, JobQueryService
from core.application.management.scope import ScopeContext
from core.application.reports.ui_page_registry import (
    PageRegistrySealedError,
    page_registration_scope,
)
from core.application.settings.feature_endpoints import (
    FeatureEndpointRegistry,
    entitled_feature_endpoints,
)
from core.application.settings.platform_contracts import (
    CoreEntitlementService,
    FeatureNotEntitledError,
)
from core.application.tooling.ops import retention_cleanup
from core.entrypoints.admin.settings_retention import create_settings_retention_router
from core.entrypoints.api.app_bootstrap import create_api_app_bootstrap_router

# These tests call the registry store outside a request or a job; they act for
# the installation account (ADR 0020, C5).
pytestmark = pytest.mark.usefixtures("installation_account")

ALICE = "11111111-1111-4111-8111-111111111111"
BOB = "22222222-2222-4222-8222-222222222222"
MALLORY = "44444444-4444-4444-8444-444444444444"
ACCOUNT = DEFAULT_ACCOUNT_ID
OTHER_ACCOUNT = "55555555-5555-4555-8555-555555555555"
WORKFLOW = "acme.invoice_review"
CHILD_WORKFLOW = "acme.invoice_check"
STEP = "review"
MODULE = "acme.approvals"


# --- A database that answers by statement ----------------------------------------------


class _Cursor:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn
        self.description = None
        self._rows: list[dict[str, Any]] = []
        self.rowcount = 0

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.statements.append((" ".join(str(sql).split()), params))
        self._rows = self._conn.answer(" ".join(str(sql).split()), params)

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None


class _Conn:
    """Approvers, users, roles and workflows as plain lists."""

    def __init__(
        self,
        *,
        approvers: list[tuple[str, str, str]] = (),
        users: list[dict[str, Any]] = (),
        workflows: list[tuple[str, str]] = (),
        role_permissions: list[tuple[str, str]] = (("operator", "approvals.decide"),),
        steps: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self.approvers = list(approvers)
        # (workflow_id, step_id) -> registry_workflow_steps.step_kind
        self.steps = {(WORKFLOW, STEP): "approval"} if steps is None else dict(steps)
        self.users = {user["id"]: user for user in users}
        self.workflows = list(workflows)
        self.role_permissions = list(role_permissions)
        self.statements: list[tuple[str, Any]] = []
        self.inserted: list[Any] = []

    def cursor(self, *_args: Any, **_kwargs: Any) -> _Cursor:
        return _Cursor(self)

    def answer(self, sql: str, params: Any) -> list[dict[str, Any]]:
        if "FROM public.approval_step_approvers a LEFT JOIN public.users" in sql:
            workflows = set(params[0])
            return [
                {
                    "workflow_id": wf,
                    "step_id": step,
                    "user_id": user,
                    "username": self.users.get(user, {}).get("username", ""),
                    "display_name": self.users.get(user, {}).get("display_name", ""),
                }
                for wf, step, user in self.approvers
                if wf in workflows
            ]
        if "FROM public.approval_step_approvers a JOIN public.users" in sql:
            wf, step = params[0], params[1]
            return [
                {
                    "user_id": user,
                    "username": self.users[user]["username"],
                    "display_name": self.users[user]["display_name"],
                }
                for a_wf, a_step, user in self.approvers
                if (a_wf, a_step) == (wf, step) and user in self.users
            ]
        if "SELECT user_id::text AS user_id FROM public.approval_step_approvers" in sql:
            wf, step = params[0], params[1]
            return [
                {"user_id": user}
                for a_wf, a_step, user in self.approvers
                if (a_wf, a_step) == (wf, step)
            ]
        if "FROM public.registry_workflows" in sql and "AS present" in sql:
            return [{"present": 1}] if tuple(params) in self.workflows else []
        if "SELECT step_kind FROM public.registry_workflow_steps" in sql:
            kind = self.steps.get(tuple(params[:2]))
            return [{"step_kind": kind}] if kind is not None else []
        if "FROM role_permissions rp JOIN roles r" in sql:
            return [{"key": key, "permission_key": perm} for key, perm in self.role_permissions]
        if "JOIN public.user_account_memberships m" in sql:
            return [
                {
                    "user_id": user["id"],
                    "username": user["username"],
                    "display_name": user["display_name"],
                    "role_key": user["role"],
                }
                for user in self.users.values()
                if user["account_id"] == params[0] and user.get("active", True)
            ]
        if "AS user_id FROM public.user_account_memberships AS m" in sql:
            ids, account = params
            return [
                {"user_id": user_id}
                for user_id in ids
                if self.users.get(user_id, {}).get("account_id") == account
            ]
        if sql.startswith(
            (
                "INSERT INTO public.approval_step_approvers",
                "DELETE FROM public.approval_step_approvers",
            )
        ):
            self.inserted.append((sql.split()[0], params))
        if sql.startswith("INSERT INTO public.approval_decision_journal"):
            self.inserted.append(("JOURNAL", params))
        return []

    def approver_queries(self) -> list[str]:
        return [sql for sql, _ in self.statements if "approval_step_approvers" in sql]


def _user(user_id: str, name: str, *, account: str = ACCOUNT, role: str = "operator"):
    return {
        "id": user_id,
        "username": name.lower(),
        "display_name": name,
        "account_id": account,
        "role": role,
    }


USERS = [_user(ALICE, "Alice"), _user(BOB, "Bob"), _user(MALLORY, "Mallory", account=OTHER_ACCOUNT)]


def _person(user_id: str | None) -> ScopeContext:
    return ScopeContext(account_id=ACCOUNT, user_id=user_id, principal_type="operator")


# --- the approval gate ------------------------------------------------------------------


def _gate_service(conn: _Conn, *, job: dict[str, Any] | None = None) -> ApprovalQueryService:
    bl = SimpleNamespace(
        conn=conn,
        get_job=lambda _job_id: dict(
            job
            or {
                "id": "job-1",
                "status": "awaiting_approval",
                "thread_id": "thread-1",
                "project_id": "7",
                "selected_workflow_key": WORKFLOW,
            }
        ),
        get_project=lambda _project_id: {"id": "7", "name": "Demo", "account_id": ACCOUNT},
    )
    state = MagicMock()
    state.tasks = [MagicMock(interrupts=[MagicMock(value={"stage": "Review", "step_id": STEP})])]
    return ApprovalQueryService(
        get_logger=lambda: bl,
        get_thread_state_snapshot=lambda _thread_id: state,
        approval_fallback_payload=lambda _job: {},
        to_json_obj=lambda value: value,
    )


def test_gate_lets_an_assigned_approver_decide_and_names_the_approvers() -> None:
    conn = _Conn(approvers=[(WORKFLOW, STEP, ALICE), (WORKFLOW, STEP, BOB)], users=USERS)

    gate = _gate_service(conn).get_current_gate("job-1", scope=_person(ALICE)).dump()

    assert gate["can_decide"] is True
    assert gate["approvers"] == [
        {"username": "alice", "display_name": "Alice"},
        {"username": "bob", "display_name": "Bob"},
    ]


def test_gate_tells_anyone_else_they_cannot_decide() -> None:
    conn = _Conn(approvers=[(WORKFLOW, STEP, ALICE)], users=USERS)

    for scope in (_person(BOB), _person(None)):
        gate = _gate_service(conn).get_current_gate("job-1", scope=scope).dump()
        assert gate["can_decide"] is False
        assert gate["approvers"] == [{"username": "alice", "display_name": "Alice"}]


def test_gate_without_assigned_approvers_is_decided_as_before() -> None:
    gate = _gate_service(_Conn(users=USERS)).get_current_gate("job-1", scope=_person(BOB)).dump()

    assert gate["can_decide"] is True
    assert gate["approvers"] == []


def test_gate_and_decision_check_agree_on_an_invoke_child() -> None:
    """5a: a child job's gate is checked under the child workflow's assignments."""
    child_job = {
        "id": "child-1",
        "status": "awaiting_approval",
        "thread_id": "thread-child",
        "project_id": "7",
        # What enqueue_workflow_start writes for an Invoke child.
        "selected_workflow_key": CHILD_WORKFLOW,
        "meta": {"invoke_parent_job_id": "job-1", "invoke_child_workflow_id": CHILD_WORKFLOW},
    }
    conn = _Conn(approvers=[(CHILD_WORKFLOW, STEP, ALICE)], users=USERS)

    assert job_workflow_ids(child_job) == [CHILD_WORKFLOW]
    gate = _gate_service(conn, job=child_job).get_current_gate("child-1", scope=_person(BOB))
    assert gate.can_decide is False
    with pytest.raises(StepApproverRequiredError):
        require_step_approver(
            conn, _person(BOB), workflow_ids=job_workflow_ids(child_job), step_id=STEP
        )
    assert require_step_approver(
        conn, _person(ALICE), workflow_ids=job_workflow_ids(child_job), step_id=STEP
    ) == frozenset({ALICE})


def test_job_detail_gate_carries_the_assignment() -> None:
    from core.application.reports import job_detail_contract

    source = Path(job_detail_contract.__file__).read_text(encoding="utf-8")
    assert 'approval_gate["can_decide"]' in source
    assert 'approval_gate["approvers"]' in source


# --- the awaiting-approval queue --------------------------------------------------------


def _queue_row(job_id: str, workflow: str, step: str | None) -> dict[str, Any]:
    return {
        "id": job_id,
        "project_id": "7",
        "status": "awaiting_approval",
        "account_id": ACCOUNT,
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "selected_workflow": workflow,
        "approval_step_id": step,
    }


def _queue_service(conn: _Conn, rows: list[dict[str, Any]]) -> JobQueryService:
    bl = SimpleNamespace(
        conn=conn,
        get_awaiting_approval_jobs=lambda account_id=None: [dict(row) for row in rows],
    )
    return JobQueryService(
        get_logger=lambda: bl,
        serialize_attention_job_row=lambda row: row,
        serialize_awaiting_approval_job_row=lambda row: {
            "id": row["id"],
            "project_id": row["project_id"],
            "selected_workflow": row["selected_workflow"],
        },
        job_attention_group=lambda status, acknowledged_at=None: "running",
        build_runtime_payload_for_api=AsyncMock(return_value={}),
    )


def test_queue_rows_say_who_may_decide_with_one_query_for_the_page() -> None:
    conn = _Conn(approvers=[(WORKFLOW, STEP, ALICE), ("acme.other", "check", BOB)], users=USERS)
    rows = [
        _queue_row("mine", WORKFLOW, STEP),
        _queue_row("theirs", "acme.other", "check"),
        _queue_row("open", "acme.free", "approve"),
        _queue_row("unknown-step", WORKFLOW, None),
    ]

    page = _queue_service(conn, rows).page_awaiting_approval_jobs(scope=_person(ALICE)).dump()

    by_id = {item["id"]: item for item in page["jobs"]}
    assert by_id["mine"]["can_decide"] is True
    assert by_id["mine"]["approvers"] == [{"username": "alice", "display_name": "Alice"}]
    assert by_id["theirs"]["can_decide"] is False
    assert by_id["theirs"]["approvers"] == [{"username": "bob", "display_name": "Bob"}]
    assert by_id["open"]["can_decide"] is True
    assert by_id["open"]["approvers"] == []
    assert by_id["unknown-step"]["can_decide"] is True
    assert len(conn.approver_queries()) == 1


def test_queue_survives_an_unreadable_assignment_table() -> None:
    class _Broken(_Conn):
        def answer(self, sql: str, params: Any) -> list[dict[str, Any]]:
            if "approval_step_approvers" in sql:
                raise RuntimeError("relation does not exist")
            return super().answer(sql, params)

    page = _queue_service(_Broken(), [_queue_row("a", WORKFLOW, STEP)]).page_awaiting_approval_jobs(
        scope=_person(ALICE)
    )

    assert page.jobs[0].can_decide is None


def test_queue_sql_selects_the_waiting_step_and_workflow_ids() -> None:
    from core.infrastructure.db import job_admin_store

    source = Path(job_admin_store.__file__).read_text(encoding="utf-8")
    assert "AS approval_step_id" in source
    assert "j.selected_workflow_key" in source
    assert "AS meta_selected_workflow_id" in source


# --- feature endpoints ------------------------------------------------------------------


def test_a_module_registers_an_endpoint_under_its_own_tree() -> None:
    registry = FeatureEndpointRegistry()
    with page_registration_scope(module_id=MODULE):
        registry.register("approval_assignees", f"/api/modules/{MODULE}/approval-steps")

    assert registry.snapshot() == {"approval_assignees": f"/api/modules/{MODULE}/approval-steps"}


@pytest.mark.parametrize(
    "path",
    [
        "/api/modules/acme.other/approval-steps",
        "/api/approvals",
        f"/api/modules/{MODULE}",
        f"/api/modules/{MODULE}/../acme.other/x",
        "https://example.test/api/modules/acme.approvals/x",
    ],
)
def test_an_endpoint_outside_the_module_tree_is_refused(path: str) -> None:
    registry = FeatureEndpointRegistry()
    with page_registration_scope(module_id=MODULE), pytest.raises(ValueError):
        registry.register("approval_assignees", path)
    assert registry.snapshot() == {}


def test_only_a_prepared_module_registers_endpoints() -> None:
    registry = FeatureEndpointRegistry()
    with pytest.raises(ValueError):
        registry.register("approval_assignees", f"/api/modules/{MODULE}/approval-steps")
    with page_registration_scope(module_id=None), pytest.raises(ValueError):
        registry.register("approval_assignees", f"/api/modules/{MODULE}/approval-steps")


def test_registration_closes_when_sealed_and_a_feature_has_one_endpoint() -> None:
    registry = FeatureEndpointRegistry()
    with page_registration_scope(module_id=MODULE):
        registry.register("approval_assignees", f"/api/modules/{MODULE}/approval-steps")
        with pytest.raises(ValueError):
            registry.register("approval_assignees", f"/api/modules/{MODULE}/other")
        registry.seal()
        with pytest.raises(PageRegistrySealedError):
            registry.register("approval_journal", f"/api/modules/{MODULE}/journal")
    assert registry.sealed
    assert list(registry.snapshot()) == ["approval_assignees"]


def test_composition_root_seals_endpoints_with_the_page_registries() -> None:
    from core.entrypoints.api import app_factory

    source = Path(app_factory.__file__).read_text(encoding="utf-8")
    seal_pages = source.index("seal_page_registries(container.registries)")
    assert source.index("container.feature_endpoints.seal()") > seal_pages
    assert source.index("_attach_module_ports(container)") < source.index("load_platform_modules(")


def _bootstrap_client(entitled: dict[str, bool], endpoints: dict[str, str]) -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def _auth(request, call_next):
        request.state.auth_source = "local"
        request.state.auth_user = {"id": ALICE, "username": "alice", "account_id": ACCOUNT}
        return await call_next(request)

    entitlements = SimpleNamespace(snapshot=lambda _account: dict(entitled))
    app.include_router(
        create_api_app_bootstrap_router(
            request_principal=lambda _request: None,
            local_auth_enabled=lambda: True,
            local_auth_bootstrap_required=lambda: False,
            external_auth_enabled=False,
            current_app_edition=lambda: "community",
            platform_contract=lambda key: entitlements if key == "entitlement_service" else None,
            build_client_i18n_payload=lambda request, auth_user=None: {"locale": "en"},
            api_error_response=lambda exc, request=None: JSONResponse(
                {"error": str(exc)}, status_code=500
            ),
            feature_endpoints_resolver=lambda: dict(endpoints),
        )
    )
    return TestClient(app, raise_server_exceptions=False)


def test_bootstrap_sends_endpoints_of_entitled_features_only() -> None:
    endpoints = {
        "approval_assignees": f"/api/modules/{MODULE}/approval-steps",
        "approval_journal": f"/api/modules/{MODULE}/journal",
    }

    body = _bootstrap_client({"approval_assignees": True}, endpoints).get("/api/app/bootstrap")

    assert body.status_code == 200
    assert body.json()["feature_endpoints"] == {
        "approval_assignees": f"/api/modules/{MODULE}/approval-steps"
    }
    nothing = _bootstrap_client({}, endpoints).get("/api/app/bootstrap").json()
    assert nothing["feature_endpoints"] == {}


def test_entitled_feature_endpoints_needs_a_true_flag() -> None:
    assert entitled_feature_endpoints({"a": "/api/modules/m/a"}, {"a": "yes"}) == {}


# --- ports for modules ------------------------------------------------------------------


def _ports(conn: _Conn) -> tuple[ApprovalAssignmentsPort, ApprovalJournalPort]:
    @contextmanager
    def connection():
        yield conn

    return ApprovalAssignmentsPort(connection), ApprovalJournalPort(connection)


def test_replace_refuses_a_foreign_workflow_before_writing() -> None:
    conn = _Conn(users=USERS, workflows=[(WORKFLOW, OTHER_ACCOUNT)])
    assignments, _ = _ports(conn)

    with pytest.raises(LookupError):
        assignments.replace_approvers(
            WORKFLOW, STEP, [ALICE], account_id=ACCOUNT, created_by="alice"
        )
    assert conn.inserted == []


@pytest.mark.parametrize("user_id", [MALLORY, "66666666-6666-4666-8666-666666666666", "nobody"])
def test_replace_refuses_a_user_outside_the_account(user_id: str) -> None:
    conn = _Conn(users=USERS, workflows=[(WORKFLOW, ACCOUNT)])
    assignments, _ = _ports(conn)

    with pytest.raises(ValueError):
        assignments.replace_approvers(
            WORKFLOW, STEP, [ALICE, user_id], account_id=ACCOUNT, created_by="alice"
        )
    assert conn.inserted == []


def test_replace_refuses_a_person_who_cannot_decide() -> None:
    viewer = _user("77777777-7777-4777-8777-777777777777", "Vic", role="viewer")
    conn = _Conn(users=[*USERS, viewer], workflows=[(WORKFLOW, ACCOUNT)])
    assignments, _ = _ports(conn)

    with pytest.raises(ValueError):
        assignments.replace_approvers(
            WORKFLOW, STEP, [viewer["id"]], account_id=ACCOUNT, created_by="alice"
        )


@pytest.mark.parametrize(
    "steps",
    [{}, {(WORKFLOW, "other-step"): "approval"}, {(WORKFLOW, STEP): "agent"}],
    ids=["no-steps", "step-missing", "not-an-approval-step"],
)
def test_replace_refuses_a_step_that_is_not_an_approval_step_of_the_workflow(
    steps: dict[tuple[str, str], str],
) -> None:
    from core.application.management.approval_ports import ApprovalStepInvalidError

    conn = _Conn(users=USERS, workflows=[(WORKFLOW, ACCOUNT)], steps=steps)
    assignments, _ = _ports(conn)

    with pytest.raises(ApprovalStepInvalidError) as refused:
        assignments.replace_approvers(
            WORKFLOW, STEP, [ALICE], account_id=ACCOUNT, created_by="alice"
        )
    # A ValueError, so the module answers 422.
    assert isinstance(refused.value, ValueError)
    assert conn.inserted == []


def test_replace_writes_people_of_the_account() -> None:
    conn = _Conn(users=USERS, workflows=[(WORKFLOW, ACCOUNT)])
    assignments, _ = _ports(conn)

    assignments.replace_approvers(
        WORKFLOW, STEP, [ALICE, BOB.upper()], account_id=ACCOUNT, created_by="alice"
    )

    inserted = [params for verb, params in conn.inserted if verb == "INSERT"]
    assert [params[2] for params in inserted] == [ALICE, BOB]


def test_candidates_are_people_of_the_account_who_may_decide() -> None:
    admin = _user("88888888-8888-4888-8888-888888888888", "Ada", role="admin")
    viewer = _user("77777777-7777-4777-8777-777777777777", "Vic", role="viewer")
    conn = _Conn(users=[*USERS, admin, viewer])
    assignments, _ = _ports(conn)

    names = [user["display_name"] for user in assignments.candidates(ACCOUNT)]

    assert sorted(names) == ["Ada", "Alice", "Bob"]


def test_list_approvers_with_an_account_checks_the_workflow_and_people() -> None:
    conn = _Conn(
        approvers=[(WORKFLOW, STEP, ALICE), (WORKFLOW, STEP, MALLORY)],
        users=USERS,
        workflows=[(WORKFLOW, ACCOUNT)],
    )
    assignments, _ = _ports(conn)

    assert [a["user_id"] for a in assignments.list_approvers(WORKFLOW, STEP)] == [ALICE, MALLORY]
    assert [
        a["user_id"] for a in assignments.list_approvers(WORKFLOW, STEP, account_id=ACCOUNT)
    ] == [ALICE]
    with pytest.raises(LookupError):
        assignments.list_approvers(WORKFLOW, STEP, account_id=OTHER_ACCOUNT)


def test_journal_port_reads_one_account_and_refuses_unknown_filters() -> None:
    conn = MagicMock()
    _, journal = _ports(conn)
    with pytest.raises(ValueError):
        journal.list_entries(ACCOUNT, account_id=OTHER_ACCOUNT)


class _JournalConn:
    """Journal rows; ``job_exists`` as the database computes it from ``jobs``."""

    def __init__(self, rows: list[dict[str, Any]], jobs: set[tuple[str, str]]) -> None:
        self.rows = rows
        self.jobs = jobs
        self.statements: list[tuple[str, Any]] = []

    def cursor(self, *_args: Any, **_kwargs: Any) -> _Cursor:
        return _Cursor(self)  # type: ignore[arg-type]

    def answer(self, sql: str, _params: Any) -> list[dict[str, Any]]:
        if sql.startswith("SELECT count(*)"):
            return [{"total": len(self.rows)}]
        return [
            {**row, "job_exists": (row["job_id"], row["account_id"]) in self.jobs}
            for row in self.rows
        ]


def test_journal_rows_say_whether_their_run_still_exists() -> None:
    kept, deleted = "11111111-aaaa-4aaa-8aaa-111111111111", "22222222-bbbb-4bbb-8bbb-222222222222"
    rows = [
        {"id": "e1", "account_id": ACCOUNT, "job_id": kept, "action": "approve"},
        {"id": "e2", "account_id": ACCOUNT, "job_id": deleted, "action": "reject"},
    ]
    conn = _JournalConn(rows, jobs={(kept, ACCOUNT), (deleted, OTHER_ACCOUNT)})
    _, journal = _ports(conn)  # type: ignore[arg-type]

    page = journal.list_entries(ACCOUNT, limit=10)

    assert [(item["job_id"], item["job_exists"]) for item in page["items"]] == [
        (kept, True),
        (deleted, False),
    ]
    listing = conn.statements[-1][0]
    # Hard-deleted or another account's job reads as gone; the trash does not count.
    assert (
        "EXISTS (SELECT 1 FROM public.jobs job WHERE job.id = j.job_id "
        "AND job.account_id = j.account_id) AS job_exists" in listing
    )
    assert "deleted_at" not in listing
    assert "WHERE j.account_id = %s" in listing


def test_journal_rows_without_the_flag_stay_as_they_are() -> None:
    from core.application.management.approval_journal import _entry_view

    assert "job_exists" not in _entry_view({"id": "e1", "job_id": "j1"})


# --- retention --------------------------------------------------------------------------


def _scheduled_cfg(monkeypatch, entitlement_service) -> Any:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(retention_cleanup, "resolve_observability_dsn", lambda value: "dsn://x")
    monkeypatch.setattr(
        retention_cleanup,
        "_load_db_retention_settings",
        lambda *_a, **_k: {
            "action_logs_days": 30,
            "job_logs_days": 30,
            "trash_days": 30,
            "terminal_job_checkpoints_days": 30,
            "revisions_keep": 5,
        },
    )
    monkeypatch.setattr(
        retention_cleanup, "run_cleanup", lambda dsn, cfg: seen.setdefault("cfg", cfg) and {}
    )
    retention_cleanup.run_scheduled_retention(entitlement_service=entitlement_service)
    return seen["cfg"]


class _Source:
    def __init__(self, accounts: dict[str, list[str]]) -> None:
        self.accounts = accounts

    def features_for(self, account_id: str) -> list[str]:
        return self.accounts.get(account_id, [])

    def limits_for(self, account_id: str) -> dict[str, int]:
        return {}


def test_scheduled_retention_honours_an_entitled_account(monkeypatch) -> None:
    service = CoreEntitlementService()
    service.register_entitlement_source(_Source({ACCOUNT: ["approval_journal"]}))

    cfg = _scheduled_cfg(monkeypatch, service)

    assert cfg.journal_entitled(ACCOUNT) is True
    assert cfg.journal_entitled(OTHER_ACCOUNT) is False


def test_scheduled_retention_without_entitlements_follows_the_jobs(monkeypatch) -> None:
    assert _scheduled_cfg(monkeypatch, CoreEntitlementService()).journal_entitled(ACCOUNT) is False
    assert _scheduled_cfg(monkeypatch, None).journal_entitled is None


class _BrokenSource(_Source):
    def features_for(self, account_id: str) -> list[str]:
        raise OSError("entitlement file unreadable")


def test_scheduled_retention_reports_a_failed_source_as_unknown(monkeypatch) -> None:
    service = CoreEntitlementService()
    service.register_entitlement_source(_BrokenSource({}))

    cfg = _scheduled_cfg(monkeypatch, service)

    # is_entitled fails closed to False; the purge must see "unknown" instead.
    assert service.is_entitled(ACCOUNT, "approval_journal") is False
    assert cfg.journal_entitled(ACCOUNT) is None


def test_scheduled_retention_reports_a_degraded_role_as_unknown(monkeypatch) -> None:
    # A platform module failed to load in the control role and its source was rolled
    # back: the remaining service has no source, but "not entitled" is not definite.
    service = CoreEntitlementService()
    service.mark_loading_degraded("acme.accounts 1.2.0")

    cfg = _scheduled_cfg(monkeypatch, service)

    assert cfg.journal_entitled(ACCOUNT) is None


def test_control_role_logs_the_modules_that_failed_to_load(caplog) -> None:
    from core.entrypoints.cli import control_process

    with caplog.at_level("WARNING", logger=control_process.logger.name):
        control_process._log_platform_module_failures([])
        control_process._log_platform_module_failures(
            [{"id": "acme.accounts", "version": "1.2.0", "status": "failed", "load_error": "x"}]
        )

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1
    assert "acme.accounts 1.2.0" in messages[0]
    assert "approval journal" in messages[0]
    source = Path(control_process.__file__).read_text(encoding="utf-8")
    assert "failures=module_failures" in source


def test_control_role_hands_modules_and_retention_one_entitlement_service(monkeypatch) -> None:
    from core.entrypoints.cli import control_process

    service = CoreEntitlementService()
    calls: list[Any] = []
    monkeypatch.setattr(control_process, "_CONTROL_ENTITLEMENT_SERVICE", service)
    monkeypatch.setattr(
        retention_cleanup,
        "run_scheduled_retention",
        lambda dsn="", *, entitlement_service=None: calls.append(entitlement_service) or {},
    )

    control_process._run_retention_pass()

    assert calls == [service]
    source = Path(control_process.__file__).read_text(encoding="utf-8")
    assert '"entitlement_service": _CONTROL_ENTITLEMENT_SERVICE,' in source


def test_role_hooks_receive_role_services_without_losing_the_identity() -> None:
    from core.infrastructure.platform_modules import loader

    tree = ast.parse(Path(loader.__file__).read_text(encoding="utf-8"))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "register_platform_modules_for_role"
    )
    assert "services" in [arg.arg for arg in function.args.kwonlyargs]


@pytest.mark.parametrize("days", [30, "7"])
def test_saving_journal_retention_needs_the_entitlement(days: Any) -> None:
    with pytest.raises(FeatureNotEntitledError):
        require_journal_retention_entitled(
            {"approval_journal_retention_days": days},
            account_id=ACCOUNT,
            entitlement_service=CoreEntitlementService(),
        )
    entitled = CoreEntitlementService()
    entitled.register_entitlement_source(_Source({ACCOUNT: ["approval_journal"]}))
    require_journal_retention_entitled(
        {"approval_journal_retention_days": days}, account_id=ACCOUNT, entitlement_service=entitled
    )


@pytest.mark.parametrize(
    "settings", [{}, {"approval_journal_retention_days": 0}, {"trash_days": 3}]
)
def test_zero_or_absent_journal_retention_needs_nothing(settings: dict[str, Any]) -> None:
    require_journal_retention_entitled(settings, account_id=ACCOUNT, entitlement_service=None)


def _retention_client(store: Any, entitlements: Any) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_settings_retention_router(
            require_authenticated_user=lambda request, required_roles=None: {"id": ALICE},
            auth_admin_roles={"admin"},
            get_logger_only=lambda: store,
            request_scope=lambda request, **_k: SimpleNamespace(account_id=ACCOUNT),
            resolve_actor_username=lambda request, requested, default="system": "alice",
            entitlement_service=lambda: entitlements,
        )
    )
    return TestClient(app, raise_server_exceptions=False)


def test_settings_api_refuses_journal_retention_without_the_entitlement() -> None:
    store = MagicMock()
    store.save_retention_settings.return_value = {"approval_journal_retention_days": 30}

    refused = _retention_client(store, CoreEntitlementService()).post(
        "/api/settings/retention", json={"approval_journal_retention_days": 30}
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "feature_not_entitled"
    store.save_retention_settings.assert_not_called()

    other = _retention_client(store, CoreEntitlementService()).post(
        "/api/settings/retention", json={"trash_days": 7}
    )
    assert other.status_code == 200


@pytest.mark.parametrize(
    ("stored", "requested", "allowed"),
    [
        (30, 30, True),  # unchanged saved value: the rest of the form saves
        ("30", 30, True),
        (30, 0, True),  # lowering to the default rule
        (30, 60, False),  # raising
        (30, 7, False),  # any other positive value
        (0, 30, False),  # a new positive value
        (None, 30, False),  # saved value unknown
    ],
)
def test_an_unentitled_account_keeps_or_clears_but_never_sets_a_journal_retention(
    stored: Any, requested: int, allowed: bool
) -> None:
    def _check() -> None:
        require_journal_retention_entitled(
            {"approval_journal_retention_days": requested},
            account_id=ACCOUNT,
            entitlement_service=CoreEntitlementService(),
            stored_days=stored,
        )

    if allowed:
        _check()
    else:
        with pytest.raises(FeatureNotEntitledError):
            _check()


def test_settings_api_saves_an_unchanged_journal_retention_without_the_entitlement() -> None:
    store = MagicMock()
    store.get_retention_settings.return_value = {"approval_journal_retention_days": 30}
    store.save_retention_settings.return_value = {"approval_journal_retention_days": 30}
    client = _retention_client(store, CoreEntitlementService())

    unchanged = client.post(
        "/api/settings/retention", json={"approval_journal_retention_days": 30, "trash_days": 7}
    )
    cleared = client.post("/api/settings/retention", json={"approval_journal_retention_days": 0})
    raised = client.post("/api/settings/retention", json={"approval_journal_retention_days": 90})

    assert unchanged.status_code == 200
    assert cleared.status_code == 200
    assert raised.status_code == 403
    assert store.save_retention_settings.call_count == 2


# --- 5b: an Invoke child's legacy auto-approve flag --------------------------------------


def test_no_runtime_code_acts_on_the_child_auto_approve_flag() -> None:
    """A restricted step is never auto-approved: nothing reads the legacy flag to decide.

    The flag is only carried (state field, trusted restore from job meta); the
    Approval gate interrupts for a person whatever it says (see
    tests/unit/test_approval_gate.py). A new reader must come with its own
    assigned-approver check, so this test names the only allowed files.
    """
    root = Path(__file__).resolve().parents[2] / "core"
    readers = sorted(
        str(path.relative_to(root.parent))
        for path in root.rglob("*.py")
        if "invoke_child_auto_approve" in path.read_text(encoding="utf-8", errors="ignore")
    )
    assert readers == [
        "core/application/jobs/task_execution_request.py",
        "core/application/orchestration/state.py",
    ]


# --- decision overrides are journaled ---------------------------------------------------


def test_an_override_of_an_approval_step_is_journaled() -> None:
    from core.application.management.approval_journal_recording import record_override

    conn = _Conn()
    job = {"id": "job-1", "account_id": ACCOUNT, "project_id": "7", "meta": {}}

    assert record_override(
        conn,
        job=job,
        scope=_person(ALICE),
        workflow_id=WORKFLOW,
        step_id=STEP,
        original_value={"action": "reject"},
        override_value={"action": "approve"},
        reason="Wrong call",
    )

    (journal,) = [params for verb, params in conn.inserted if verb == "JOURNAL"]
    assert "override" in journal
    assert ALICE in journal
    assert "Wrong call" in journal


def _override(
    conn: _Conn | None,
    *,
    status: str,
    approvers: bool,
    current_step: str = STEP,
    cursor_step: str | None = None,
) -> MagicMock:
    from core.application.management.command_models import JobDecisionOverrideInput
    from core.application.management.command_services_job import JobCommandService

    if approvers and conn is not None:
        conn.approvers.append((WORKFLOW, STEP, ALICE))
    bl = MagicMock()
    bl.conn = conn
    bl.get_job.return_value = {
        "id": "job-9",
        "project_id": "1",
        "thread_id": "thread-9",
        "status": status,
        "current_step_id": current_step,
        "account_id": ACCOUNT,
        "selected_workflow": WORKFLOW,
        "meta": {},
    }
    bl.get_project.return_value = {"id": 1, "name": "Demo", "account_id": ACCOUNT}
    bl.create_job_decision_override.return_value = {
        "id": "override-1",
        "original_value": {"action": "reject"},
        "override_value": {"action": "approve"},
    }
    service = JobCommandService(
        get_logger=lambda: bl,
        get_thread_state_values=lambda thread_id: (
            {"run_cursor": {"step_id": cursor_step}} if cursor_step else {}
        ),
        runtime_step_scope_id=lambda runtime_state: "",
        latest_runtime_message_id=lambda runtime_state, step_execution_id="": None,
        override_workflow_id_from_value=lambda value: "",
        is_empty_original_value=lambda value: value in (None, "", {}),
        derive_override_original_value=lambda **kwargs: {"action": "reject"},
        validate_explicit_workflow_override=lambda **kwargs: None,
        to_json_obj=lambda value: value,
        serialize_decision_override_row=lambda row: row,
    )
    service.create_decision_override(
        "job-9",
        JobDecisionOverrideInput(
            domain="gate_outcome",
            override_value={"action": "approve"},
            reason="Wrong call",
            created_by="alice",
            step_id=STEP,
        ),
        scope=ScopeContext(account_id=ACCOUNT, actor_id="alice", user_id=ALICE),
    )
    return bl


@pytest.mark.parametrize(
    ("status", "approvers", "current_step", "cursor_step", "journaled"),
    [
        ("awaiting_approval", False, STEP, None, 1),  # the gate the job waits on
        ("awaiting_approval", True, STEP, None, 1),  # restricted, decided by Alice
        ("awaiting_approval", False, "other", STEP, 1),  # the cursor names the gate
        ("awaiting_approval", True, "other", None, 0),  # another step than the gate
        ("awaiting_approval", False, STEP, "other", 0),  # the cursor is elsewhere
        ("succeeded", True, STEP, None, 0),  # no gate: not an approval decision
        ("succeeded", False, STEP, None, 0),
    ],
)
def test_only_an_override_of_the_current_approval_gate_is_journaled(
    status: str, approvers: bool, current_step: str, cursor_step: str | None, journaled: int
) -> None:
    conn = _Conn(users=USERS)

    bl = _override(
        conn,
        status=status,
        approvers=approvers,
        current_step=current_step,
        cursor_step=cursor_step,
    )

    bl.create_job_decision_override.assert_called_once()
    rows = [params for verb, params in conn.inserted if verb == "JOURNAL"]
    assert len(rows) == journaled
    if rows:
        assert "override" in rows[0]
        assert ALICE in rows[0]


def test_an_override_naming_a_step_is_refused_without_a_connection() -> None:
    from core.application.management.command_services_common import ServiceUnavailableError

    with pytest.raises(ServiceUnavailableError):
        _override(None, status="awaiting_approval", approvers=False)
