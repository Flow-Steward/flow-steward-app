"""Review fixes for project-scoped roles (ADR 0076 section 8, 2026-11-26).

A person limited to chosen projects sees only those projects: the dashboard, the
workload count and the account-wide metrics and lists are cut to them or
refused. The users list shows each person's global role only, approver candidates
follow the projects a workflow runs in, promoting a limited person to admin lifts the
limit in the same transaction, and the scoped roles port records its audit entry in
the write's transaction and refuses a person's own limit.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.auth.permissions import OPERATOR_PERMISSIONS, VIEWER_PERMISSIONS
from core.application.management.errors import ServiceForbiddenError
from core.application.management.scope import ScopeContext
from core.application.reports import dashboard_metrics_helpers as helpers

ACCOUNT = DEFAULT_ACCOUNT_ID
PERSON = "6a1c2b3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
PROJECT_X = "11111111-aaaa-4aaa-8aaa-111111111111"
PROJECT_Y = "22222222-bbbb-4bbb-8bbb-222222222222"
PROJECT_Z = "33333333-cccc-4ccc-8ccc-333333333333"


def _limited() -> ScopeContext:
    return ScopeContext(
        account_id=ACCOUNT,
        user_id=PERSON,
        actor_id="pat",
        principal_type="operator",
        auth_source="local",
        allowed_project_ids=frozenset({PROJECT_X}),
        permissions=OPERATOR_PERMISSIONS,
        project_permissions={PROJECT_X: OPERATOR_PERMISSIONS},
    )


def _unlimited() -> ScopeContext:
    return ScopeContext(account_id=ACCOUNT, user_id=PERSON, permissions=OPERATOR_PERMISSIONS)


# --- 1. the dashboard and other aggregates ---------------------------------------------


def _finished(job_id: str, project_id: str, name: str, *, days_ago: int = 0) -> dict[str, Any]:
    finished = datetime.now(UTC) - timedelta(days=days_ago, minutes=1)
    return {
        "job_id": job_id,
        "project_id": project_id,
        "project_name": name,
        "status": "completed",
        "selected_workflow": "wf",
        "started_at": (finished - timedelta(minutes=1)).isoformat(),
        "finished_at": finished.isoformat(),
    }


_ROWS = [
    _finished("job-x", PROJECT_X, "Alpha"),
    _finished("job-z", PROJECT_Z, "Secret"),
]


class _JobStore:
    """The account's jobs; it honours ``project_ids`` as the real query does."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def list_jobs_for_attention(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(dict(kwargs))
        if kwargs.get("offset"):
            return []
        wanted = kwargs.get("project_ids")
        return [dict(row) for row in _ROWS if not wanted or row["project_id"] in wanted]

    def get_jobs_usage_summary(self, job_ids: list[str]) -> dict[str, Any]:
        return {}


class _SloStore:
    def __init__(self, facts: list[dict[str, Any]] | None = None) -> None:
        self.facts = list(facts or [])
        self.rollup_calls = 0
        self.slo_calls = 0

    def list_dashboard_job_facts(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in self.facts]

    def list_dashboard_daily_rollups(self, **_kwargs: Any) -> list[dict[str, Any]]:
        self.rollup_calls += 1
        return [{"rollup_date": "2000-01-01", "completed_count": 99, "failed_count": 0}]

    def get_dashboard_rollup_state(self, **_kwargs: Any) -> dict[str, Any]:
        return {}

    def get_core_slo_metrics(self, **kwargs: Any) -> dict[str, Any]:
        self.slo_calls += 1
        return {"window_hours": kwargs["window_hours"], "samples": {}}


class _Queries:
    def list_jobs(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("status"):
            return {"jobs": [], "total": 0}
        return {"groups": {}}


def _dashboard_client(scope: ScopeContext, job_store: Any, slo_store: Any) -> TestClient:
    from core.entrypoints.api.dashboard_metrics import create_dashboard_metrics_router

    app = FastAPI()
    app.include_router(
        create_dashboard_metrics_router(
            request_scope=lambda _request, **_kwargs: scope,
            get_logger_only=lambda: object(),
            get_job_metrics_store=lambda: job_store,
            get_slo_store=lambda: slo_store,
            job_query_service=lambda: _Queries(),
            service_error_response=lambda exc, *_a, **_k: JSONResponse(
                {"error": str(exc)}, status_code=int(getattr(exc, "status_code", 500))
            ),
            api_error_response=lambda exc, *_a, **_k: JSONResponse(
                {"error": str(exc)}, status_code=500
            ),
            dashboard_default_range="7d",
            dashboard_range_hours={"7d": 168},
            dashboard_metrics_page_size=100,
            dashboard_metrics_max_rows=0,
            dashboard_triage_limit=25,
            dashboard_rollup_read_enabled=True,
        )
    )
    return TestClient(app)


def _project_names(payload: dict[str, Any]) -> set[str]:
    return {row["project_name"] for row in payload["breakdown_rows"]}


def test_the_dashboard_of_a_limited_person_shows_only_their_projects() -> None:
    job_store = _JobStore()

    response = _dashboard_client(_limited(), job_store, _SloStore()).get(
        "/api/dashboard/summary?range=7d"
    )

    assert response.status_code == 200
    assert _project_names(response.json()) == {"Alpha"}
    assert response.json()["hero_kpis"]["completed"]["value"] == 1
    assert job_store.calls
    assert all(call.get("project_ids") == [PROJECT_X] for call in job_store.calls)


def test_the_dashboard_of_an_unlimited_person_is_unchanged() -> None:
    job_store = _JobStore()

    response = _dashboard_client(_unlimited(), job_store, _SloStore()).get(
        "/api/dashboard/summary?range=7d"
    )

    assert _project_names(response.json()) == {"Alpha", "Secret"}
    assert all("project_ids" not in call for call in job_store.calls)


def test_rollup_facts_are_cut_to_the_projects_and_daily_rollups_are_not_read() -> None:
    facts = [
        _finished("old-x", PROJECT_X, "Alpha", days_ago=2),
        _finished("old-z", PROJECT_Z, "Secret", days_ago=2),
    ]
    slo_store = _SloStore(facts)
    start = datetime.now(UTC) - timedelta(days=5)
    end = datetime.now(UTC) - timedelta(days=1)

    rows, _truncated, source = helpers._dashboard_collect_historical_rows(
        bl=slo_store,
        account_id=ACCOUNT,
        historical_window_start=start,
        historical_window_end=end,
        project_ids=frozenset({PROJECT_X}),
    )
    rollups = helpers._dashboard_collect_daily_rollups(
        bl=slo_store,
        account_id=ACCOUNT,
        historical_source=source,
        historical_window_start=start,
        historical_window_end=end,
        project_ids=frozenset({PROJECT_X}),
    )

    assert source == "rollup_facts"
    assert [row["job_id"] for row in rows] == ["old-x"]
    # Daily rollups count every project of the account; a limited person gets none.
    assert rollups == []
    assert slo_store.rollup_calls == 0


def test_rollup_facts_with_none_of_the_projects_still_cover_the_window() -> None:
    slo_store = _SloStore([_finished("old-z", PROJECT_Z, "Secret", days_ago=2)])

    rows, _truncated, source = helpers._dashboard_collect_historical_rows(
        bl=slo_store,
        account_id=ACCOUNT,
        historical_window_start=datetime.now(UTC) - timedelta(days=5),
        historical_window_end=datetime.now(UTC) - timedelta(days=1),
        project_ids=frozenset({PROJECT_X}),
    )

    assert (rows, source) == ([], "rollup_facts")


def test_account_wide_slo_metrics_are_refused_to_a_limited_person() -> None:
    slo_store = _SloStore()

    refused = _dashboard_client(_limited(), _JobStore(), slo_store).get("/api/slo/core")
    allowed = _dashboard_client(_unlimited(), _JobStore(), slo_store).get("/api/slo/core")

    assert refused.status_code == 403
    assert allowed.status_code == 200
    assert slo_store.slo_calls == 1


def test_the_core_slo_dataset_is_refused_to_a_limited_person() -> None:
    from core.application.reports.analytics_service import AnalyticsService

    bl = MagicMock()
    service = AnalyticsService(get_logger_only=lambda: bl)

    with pytest.raises(ServiceForbiddenError):
        service._core_slo_summary(scope=_limited(), filters={})
    bl.get_core_slo_metrics.assert_not_called()


def test_a_saved_account_wide_report_is_refused_to_a_limited_person() -> None:
    from core.application.reports.reporting_service import ReportingService

    service = ReportingService(get_logger_only=MagicMock, job_query_service=MagicMock)
    row = {
        "account_id": ACCOUNT,
        "tenant_id": None,
        "report_type": "core_health_summary",
        "project_id": None,
        "params_jsonb": {},
        "payload_jsonb": {},
    }

    with pytest.raises(ServiceForbiddenError):
        service._enforce_report_scope(_limited(), row)
    service._enforce_report_scope(_unlimited(), row)


class _CountCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def __enter__(self) -> _CountCursor:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self) -> tuple[int]:
        return (3,)


def test_the_workload_count_of_a_limited_person_covers_their_projects() -> None:
    from core.application.jobs.workload_service import _count_active_jobs

    cursor = _CountCursor()
    bl = SimpleNamespace(conn=SimpleNamespace(cursor=lambda: cursor))

    assert _count_active_jobs(bl, account_id=ACCOUNT, project_ids=[PROJECT_X]) == 3
    sql, params = cursor.calls[-1]
    assert "j.project_id::text = ANY(%s::text[])" in sql
    assert params == (ACCOUNT, [PROJECT_X])


def _request(user: dict[str, Any]) -> Any:
    return SimpleNamespace(state=SimpleNamespace(auth_user=user))


def _operator(**extra: Any) -> dict[str, Any]:
    return {"id": PERSON, "username": "op", "roles": ["operator"], **extra}


def test_the_run_list_is_refused_to_a_limited_person(monkeypatch) -> None:
    from core.entrypoints.admin import monitoring

    listed: list[Any] = []
    monkeypatch.setattr(
        monitoring,
        "MonitoringRunsService",
        lambda: SimpleNamespace(list_runs=lambda **kw: listed.append(kw) or {"runs": []}),
    )

    with pytest.raises(HTTPException) as refused:
        monitoring.list_runs(_request(_operator(allowed_project_ids=[PROJECT_X])))
    assert refused.value.status_code == 403
    assert listed == []
    assert monitoring.list_runs(_request(_operator())) == {"runs": []}


def test_the_process_list_is_refused_to_a_limited_person(monkeypatch) -> None:
    from core.entrypoints.api import processes

    monkeypatch.setattr(processes, "list_process_payload", lambda: {"jobs": []})

    with pytest.raises(HTTPException) as refused:
        processes.list_processes(_request(_operator(allowed_project_ids=[PROJECT_X])))
    assert refused.value.status_code == 403
    # Anyone else who is not the installation administrator sees their account's share.
    assert processes.list_processes(_request(_operator()))["jobs"] == []


# --- 2. the users list shows the global role only ----------------------------------------


class _UsersCursor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __enter__(self) -> _UsersCursor:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, sql: str, _params: Any = ()) -> None:
        self.calls.append(" ".join(str(sql).split()))

    def fetchone(self) -> dict[str, int]:
        return {"total": 0}

    def fetchall(self) -> list[dict[str, Any]]:
        return []


def test_the_users_list_reads_membership_roles_only_in_the_list_and_the_role_filter() -> None:
    from core.application.auth.user_admin.repository import list_auth_users

    cursor = _UsersCursor()
    bl = SimpleNamespace(conn=SimpleNamespace(cursor=lambda *_a, **_k: cursor))

    list_auth_users(
        bl,
        search="",
        include_inactive=False,
        include_deleted=False,
        limit=50,
        offset=0,
        role="operator",
        active_filter=None,
        managed_account_ids=None,
    )

    count_sql, list_sql = cursor.calls[-2:]
    # The role filter: a project-scoped operator row does not make a viewer an operator;
    # the role is the membership's in the account (ADR 0085).
    assert "FROM user_account_memberships m_scope JOIN roles r_scope" in count_sql
    assert "user_roles" not in count_sql
    # The roles column: project rows are not merged into the membership role.
    assert (
        "LEFT JOIN user_account_memberships rm ON rm.user_id = u.id "
        "AND rm.account_id = COALESCE(%s::uuid, u.account_id)"
    ) in list_sql
    assert "user_roles" not in list_sql


# --- 4. approver candidates follow the workflow's projects --------------------------------


WORKFLOW = "acme.invoices"
ALICE = "aaaaaaaa-0000-4000-8000-000000000001"
BOB = "bbbbbbbb-0000-4000-8000-000000000002"


class _ApprovalConn:
    """Alice unlimited; Bob limited, operator in X and viewer in Y."""

    def __init__(self, workflow_projects: list[str]) -> None:
        self.workflow_projects = workflow_projects
        self.written: list[Any] = []

    def cursor(self, *_args: Any, **_kwargs: Any) -> Any:
        conn = self

        class _Cursor:
            description = None
            rowcount = 0

            def __enter__(self) -> Any:
                return self

            def __exit__(self, *_exc: object) -> bool:
                return False

            def execute(self, sql: str, params: Any = None) -> None:
                text = " ".join(sql.split())
                self._rows: list[dict[str, Any]] = []
                if "FROM role_permissions" in text:
                    self._rows = [
                        {"key": "operator", "permission_key": key}
                        for key in sorted(OPERATOR_PERMISSIONS)
                    ] + [
                        {"key": "viewer", "permission_key": key}
                        for key in sorted(VIEWER_PERMISSIONS)
                    ]
                elif "FROM public.scoped_role_users" in text:
                    self._rows = [{"user_id": BOB}]
                elif "FROM public.user_roles ur" in text:
                    self._rows = [
                        {"user_id": BOB, "project_id": PROJECT_X, "role_key": "operator"},
                        {"user_id": BOB, "project_id": PROJECT_Y, "role_key": "viewer"},
                    ]
                elif "FROM public.project_workflows" in text:
                    self._rows = [{"project_id": pid} for pid in conn.workflow_projects]
                elif "FROM public.registry_workflows" in text:
                    self._rows = [{"present": 1}]
                elif "FROM public.registry_workflow_steps" in text:
                    self._rows = [{"step_kind": "approval"}]
                elif "JOIN public.user_account_memberships m" in text:
                    self._rows = [
                        {"user_id": u, "username": n, "display_name": n, "role_key": "operator"}
                        for u, n in ((ALICE, "Alice"), (BOB, "Bob"))
                    ]
                elif text.startswith(("INSERT", "DELETE")):
                    conn.written.append(text)

            def fetchall(self) -> list[dict[str, Any]]:
                return list(self._rows)

            def fetchone(self) -> dict[str, Any] | None:
                return self._rows[0] if self._rows else None

        return _Cursor()


def _assignments(conn: _ApprovalConn) -> Any:
    from core.application.management.approval_ports import ApprovalAssignmentsPort

    @contextmanager
    def connection():
        yield conn

    return ApprovalAssignmentsPort(connection)


@pytest.mark.parametrize(
    ("projects", "names"),
    [
        ([PROJECT_X], ["Alice", "Bob"]),
        ([PROJECT_X, PROJECT_Y], ["Alice"]),
        ([PROJECT_Z], ["Alice"]),
        ([], ["Alice"]),
    ],
    ids=["decides-in-every-project", "viewer-in-one", "out-of-reach", "no-project"],
)
def test_candidates_of_a_workflow_follow_its_projects(projects, names) -> None:
    port = _assignments(_ApprovalConn(projects))

    found = [row["display_name"] for row in port.candidates(ACCOUNT, workflow_id=WORKFLOW)]

    assert found == names
    # Without a workflow the picker keeps the account-wide answer.
    assert [row["display_name"] for row in port.candidates(ACCOUNT)] == ["Alice", "Bob"]


def test_a_limited_person_who_cannot_decide_every_project_is_not_assigned() -> None:
    conn = _ApprovalConn([PROJECT_X, PROJECT_Y])
    port = _assignments(conn)

    with pytest.raises(ValueError, match=BOB):
        port.replace_approvers(WORKFLOW, "review", [BOB], account_id=ACCOUNT, created_by="a")
    assert conn.written == []

    port.replace_approvers(WORKFLOW, "review", [ALICE], account_id=ACCOUNT, created_by="a")
    assert conn.written


# --- 5. promoting a limited person to admin lifts the limit -------------------------------


class _PromotionCursor:
    def __init__(self, limited: bool) -> None:
        self.limited = limited
        self.calls: list[tuple[str, Any]] = []
        self._last = ""

    def __enter__(self) -> _PromotionCursor:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = ()) -> None:
        self._last = " ".join(str(sql).split())
        self.calls.append((self._last, params))

    def fetchone(self) -> Any:
        if self._last.startswith("DELETE FROM scoped_role_users"):
            return (PERSON,) if self.limited else None
        if self._last.startswith("UPDATE user_account_memberships SET role_id"):
            return ("membership",)
        return None

    def fetchall(self) -> list[Any]:
        return []


class _PromotionConn:
    def __init__(self, cursor: _PromotionCursor) -> None:
        self._cursor = cursor
        self.conn = None
        self.transaction = None
        self.autocommit = True

    def cursor(self, *_args: Any, **_kwargs: Any) -> _PromotionCursor:
        return self._cursor

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


def _promote(cursor: _PromotionCursor, role: str) -> list[bool]:
    from core.application.auth.user_admin.repository import update_auth_user

    lifted: list[bool] = []
    update_auth_user(
        SimpleNamespace(conn=_PromotionConn(cursor)),
        user_id=PERSON,
        existing_user={"username": "pat@example.test", "account_id": ACCOUNT},
        display_name=None,
        is_active=None,
        password=None,
        must_change_password=None,
        roles=[role],
        requested_account_ids=None,
        primary_account_id=None,
        revoke_sessions=False,
        on_scope_limit_lifted=lambda: lifted.append(True),
        actor="root",
    )
    return lifted


def test_promoting_a_limited_person_to_admin_lifts_the_limit_in_the_transaction() -> None:
    cursor = _PromotionCursor(limited=True)

    assert _promote(cursor, "admin") == [True]

    sql = [text for text, _ in cursor.calls]
    role_write = next(
        i
        for i, text in enumerate(sql)
        if text.startswith("UPDATE user_account_memberships SET role_id")
    )
    lift = next(i for i, text in enumerate(sql) if text.startswith("DELETE FROM scoped_role_users"))
    rows = next(i for i, text in enumerate(sql) if text.startswith("DELETE FROM user_roles"))
    audit = next(
        i for i, text in enumerate(sql) if "INSERT INTO public.platform_action_log" in text
    )
    assert role_write < lift < rows < audit
    audit_params = cursor.calls[audit][1]
    assert audit_params[1:3] == ("root", "auth_scoped_roles_lift")
    assert '"pat@example.test"' in audit_params[3]


@pytest.mark.parametrize("role", ["operator", "viewer"])
def test_a_role_change_below_admin_keeps_the_limit(role: str) -> None:
    cursor = _PromotionCursor(limited=True)

    assert _promote(cursor, role) == []
    assert not any("scoped_role_users" in text for text, _ in cursor.calls)


def test_promoting_an_unlimited_person_records_no_lift() -> None:
    cursor = _PromotionCursor(limited=False)

    assert _promote(cursor, "admin") == []
    assert not any("platform_action_log" in text for text, _ in cursor.calls)


def test_the_user_update_audit_says_the_limit_was_lifted() -> None:
    from core.application.auth.user_admin.command_service import UserAdminCommandService
    from core.application.auth.user_admin.contracts import UserUpdateInput

    class _Repository:
        def update_auth_user(self, _bl: Any, **kwargs: Any) -> int:
            self.kwargs = kwargs
            kwargs["on_scope_limit_lifted"]()
            return 0

    repository = _Repository()
    audits: list[dict[str, Any]] = []
    user = {
        "id": PERSON,
        "username": "pat@example.test",
        "roles": ["operator"],
        "account_id": ACCOUNT,
    }
    service = UserAdminCommandService(
        get_logger=lambda: object(),
        auth_tables_available_fn=lambda _logger: True,
        get_user_by_id_fn=lambda *_args: dict(user),
        log_auth_audit_event_fn=lambda _bl, **kwargs: audits.append(dict(kwargs)),
        seed_default_roles_fn=lambda _bl: None,
        repository=repository,
    )

    service.update_user(UserUpdateInput(user_id=PERSON, role="admin", actor="root"))

    assert repository.kwargs["actor"] == "root"
    assert audits[-1]["action"] == "user_update"
    assert audits[-1]["detail"]["lifted_project_scope_limit"] is True


# --- 3 and 6. the scoped roles port: audit in the transaction, no self-change -------------


class _PortConn:
    """A user of the account, no admin, and an audit table that may refuse the entry."""

    def __init__(self, *, audit_fails: bool = False) -> None:
        self.audit_fails = audit_fails
        self.statements: list[tuple[str, Any]] = []

    def cursor(self, *_args: Any, **_kwargs: Any) -> Any:
        conn = self

        class _Cursor:
            description = None

            def __enter__(self) -> Any:
                return self

            def __exit__(self, *_exc: object) -> bool:
                return False

            def execute(self, sql: str, params: Any = None) -> None:
                text = " ".join(sql.split())
                conn.statements.append((text, params))
                self._rows: list[dict[str, Any]] = []
                if "INSERT INTO public.platform_action_log" in text and conn.audit_fails:
                    raise RuntimeError("audit table refused the entry")
                if text.startswith("SELECT COALESCE(username, '') AS username"):
                    self._rows = [{"username": "pat@example.test"}]
                elif text.startswith("SELECT id::text AS id FROM public.projects"):
                    self._rows = [{"id": PROJECT_X}]

            def fetchall(self) -> list[dict[str, Any]]:
                return list(self._rows)

        return _Cursor()


def _port(conn: _PortConn) -> tuple[Any, list[str]]:
    from core.application.auth.scoped_role_ports import ScopedRolesPort

    events: list[str] = []

    @contextmanager
    def connection():
        events.append("begin")
        try:
            yield conn
        except Exception:
            events.append("rollback")
            raise
        events.append("commit")

    return ScopedRolesPort(connection), events


def test_the_audit_entry_is_written_in_the_write_transaction_and_names_the_person() -> None:
    conn = _PortConn()
    port, events = _port(conn)

    port.replace_user_grants(
        ACCOUNT,
        PERSON,
        [{"scope": "project", "id": PROJECT_X, "role": "viewer"}],
        created_by="alice",
    )

    assert events == ["begin", "commit"]
    audit = [params for sql, params in conn.statements if "platform_action_log" in sql]
    assert len(audit) == 1
    assert audit[0][1:3] == ("alice", "auth_scoped_roles_replace")
    assert '"username": "pat@example.test"' in audit[0][3]
    assert f'"user_id": "{PERSON}"' in audit[0][4]


@pytest.mark.parametrize("operation", ["replace", "lift"])
def test_a_failed_audit_write_fails_the_change(operation: str) -> None:
    conn = _PortConn(audit_fails=True)
    port, events = _port(conn)

    with pytest.raises(RuntimeError):
        if operation == "replace":
            port.replace_user_grants(ACCOUNT, PERSON, [], created_by="alice")
        else:
            port.lift_limit(ACCOUNT, PERSON, created_by="alice")
    assert events == ["begin", "rollback"]


def test_a_repeated_limit_replaces_who_set_it() -> None:
    conn = _PortConn()
    port, _ = _port(conn)

    port.replace_user_grants(ACCOUNT, PERSON, [], created_by="bob")

    upsert = next(
        sql for sql, _ in conn.statements if "INSERT INTO public.scoped_role_users" in sql
    )
    assert "created_by = EXCLUDED.created_by" in upsert
    assert "created_at = now()" in upsert


def test_a_person_cannot_change_their_own_limit_through_the_port() -> None:
    from core.application.auth.scoped_role_ports import ScopedRoleSelfChangeError

    conn = _PortConn()
    port, events = _port(conn)

    with pytest.raises(ScopedRoleSelfChangeError):
        port.replace_user_grants(
            ACCOUNT, PERSON, [], created_by="pat", acting_user_id=PERSON.upper()
        )
    with pytest.raises(ScopedRoleSelfChangeError):
        port.lift_limit(ACCOUNT, PERSON, created_by="pat", acting_user_id=PERSON)
    assert events == []
