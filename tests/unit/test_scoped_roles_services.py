"""ADR 0076 section 8 (2026-11-26): services check a limited person's project permission.

The writes that name their project in the body, a job, an approval thread or a report
(``SERVICE_CHECKED_PROJECT_WRITES``) are let through by the middleware; their services
refuse a person limited to chosen projects where their scoped role does not hold the
permission. The person here is an operator in X, a viewer in Y and has nothing in Z.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.auth.permissions import OPERATOR_PERMISSIONS, VIEWER_PERMISSIONS
from core.application.management.approval_assignees import (
    assignment_view,
    may_decide_in_project,
    require_step_approver,
)
from core.application.management.approval_ports import decide_candidates
from core.application.management.command_models_api_inputs import (
    ApprovalDecisionInput,
    BuilderWorkflowRunInput,
    JobAcknowledgeInput,
    WorkflowRunInput,
)
from core.application.management.command_services_job import JobCommandService
from core.application.management.command_services_project_run_requests_mixin import (
    ProjectCommandServiceRunRequestsMixin,
)
from core.application.management.errors import ServiceError, ServiceForbiddenError
from core.application.management.guards import (
    PROJECT_ROLE_REFUSAL_MESSAGE,
    require_permission_for_optional_project,
    require_permission_in_every_project,
    require_project_access,
    require_project_permission,
)
from core.application.management.scope import ScopeContext, normalize_scope
from core.application.reports.reporting_service import ReportingService

PERSON = "6a1c2b3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
PROJECT_X = "11111111-aaaa-4aaa-8aaa-111111111111"
PROJECT_Y = "22222222-bbbb-4bbb-8bbb-222222222222"
PROJECT_Z = "33333333-cccc-4ccc-8ccc-333333333333"
ACCOUNT = DEFAULT_ACCOUNT_ID


def _limited(**extra: Any) -> ScopeContext:
    return ScopeContext(
        account_id=ACCOUNT,
        user_id=PERSON,
        actor_id="pat",
        principal_type="operator",
        auth_source="local",
        allowed_project_ids=frozenset({PROJECT_X, PROJECT_Y}),
        permissions=OPERATOR_PERMISSIONS,
        project_permissions={PROJECT_X: OPERATOR_PERMISSIONS, PROJECT_Y: VIEWER_PERMISSIONS},
        **extra,
    )


def _unlimited(**extra: Any) -> ScopeContext:
    return ScopeContext(
        account_id=ACCOUNT,
        user_id=PERSON,
        actor_id="pat",
        principal_type="operator",
        auth_source="local",
        permissions=OPERATOR_PERMISSIONS,
        **extra,
    )


def _project(project_id: str) -> dict[str, Any]:
    return {"id": project_id, "account_id": ACCOUNT}


# --- the guard ------------------------------------------------------------------------


@pytest.mark.parametrize("project", [_project(PROJECT_X), PROJECT_X])
def test_the_guard_lets_an_operator_in_x_run(project) -> None:
    require_project_permission(_limited(), project, "workflows.run")


@pytest.mark.parametrize("project", [_project(PROJECT_Y), PROJECT_Y])
def test_the_guard_refuses_a_viewer_in_y(project) -> None:
    with pytest.raises(ServiceForbiddenError, match=PROJECT_ROLE_REFUSAL_MESSAGE):
        require_project_permission(_limited(), project, "workflows.run")


@pytest.mark.parametrize("project", [_project(PROJECT_Z), PROJECT_Z, None, ""])
def test_the_guard_refuses_a_project_out_of_reach(project) -> None:
    with pytest.raises(ServiceForbiddenError):
        require_project_permission(_limited(), project, "projects.read")


def test_reads_stay_allowed_where_the_scoped_role_reads() -> None:
    require_project_permission(_limited(), PROJECT_Y, "jobs.read")


@pytest.mark.parametrize("project", [_project(PROJECT_Z), PROJECT_Z, None, ""])
def test_the_guard_changes_nothing_for_a_person_without_a_limit(project) -> None:
    require_project_permission(_unlimited(), project, "workflows.run")


def test_the_guard_keeps_a_tokens_project_allowlist() -> None:
    token = ScopeContext(
        account_id=ACCOUNT, auth_source="pat", allowed_project_ids=frozenset({PROJECT_X})
    )

    require_project_permission(token, PROJECT_X, "workflows.run")
    with pytest.raises(ServiceForbiddenError):
        require_project_permission(token, PROJECT_Z, "workflows.run")
    with pytest.raises(ServiceForbiddenError):
        require_project_permission(token, _project(PROJECT_Z), "workflows.run")


def test_the_guard_still_checks_the_account_of_a_record() -> None:
    with pytest.raises(ServiceForbiddenError):
        require_project_permission(
            _limited(), {"id": PROJECT_X, "account_id": "another"}, "workflows.run"
        )


def test_an_action_across_projects_needs_the_permission_in_every_project() -> None:
    require_permission_in_every_project(_unlimited(), "reports.write")
    with pytest.raises(ServiceForbiddenError):
        require_permission_in_every_project(_limited(), "reports.write")
    only_x = _limited().model_copy(
        update={"project_permissions": {PROJECT_X: OPERATOR_PERMISSIONS}}
    )
    require_permission_in_every_project(only_x, "reports.write")
    nothing = _limited().model_copy(update={"project_permissions": {}})
    with pytest.raises(ServiceForbiddenError):
        require_permission_in_every_project(nothing, "jobs.read")


def test_an_optional_project_picks_the_project_or_every_project() -> None:
    require_permission_for_optional_project(_limited(), PROJECT_X, "extensions.use")
    with pytest.raises(ServiceForbiddenError):
        require_permission_for_optional_project(_limited(), PROJECT_Y, "extensions.use")
    with pytest.raises(ServiceForbiddenError):
        require_permission_for_optional_project(_limited(), None, "extensions.use")
    require_permission_for_optional_project(_unlimited(), None, "extensions.use")


def test_require_project_access_alone_ignores_the_role_in_the_project() -> None:
    """Reads keep using require_project_access: a viewer in Y reads Y."""
    require_project_access(_limited(), _project(PROJECT_Y))
    with pytest.raises(ServiceForbiddenError):
        require_project_access(_limited(), _project(PROJECT_Z))


# --- the scope carries the limit ------------------------------------------------------


def test_the_scope_keeps_the_per_project_permissions_through_normalization() -> None:
    scope = normalize_scope(_limited(), project_id=PROJECT_X)

    assert scope.project_permissions == {
        PROJECT_X: OPERATOR_PERMISSIONS,
        PROJECT_Y: VIEWER_PERMISSIONS,
    }
    assert normalize_scope(_unlimited()).project_permissions is None
    empty = normalize_scope({"account_id": ACCOUNT, "project_permissions": {}})
    assert empty.project_permissions == {}


# --- jobs -------------------------------------------------------------------------------


def _jobs(project_id: str | None) -> tuple[JobCommandService, MagicMock]:
    bl = MagicMock()
    bl.get_job.return_value = {
        "id": 41,
        "project_id": project_id,
        "status": "failed",
        "account_id": ACCOUNT,
    }
    bl.get_project.return_value = _project(project_id) if project_id else None
    bl.acknowledge_job.return_value = True
    bl.unacknowledge_job.return_value = True
    return JobCommandService(get_logger=lambda: bl, append_runtime_journal=MagicMock()), bl


_JOB_WRITES = [
    (
        "acknowledge_job",
        lambda service, scope: service.acknowledge_job(41, JobAcknowledgeInput(), scope=scope),
    ),
    ("unacknowledge_job", lambda service, scope: service.unacknowledge_job(41, scope=scope)),
    ("cancel_job", lambda service, scope: service.cancel_job(41, scope=scope)),
    ("restart_job", lambda service, scope: service.restart_job(41, scope=scope)),
    (
        "retry_interrupted_step",
        lambda service, scope: service.retry_interrupted_step(41, None, scope=scope),
    ),
    (
        "create_decision_override",
        lambda service, scope: service.create_decision_override(41, None, scope=scope),
    ),
    ("delete_job", lambda service, scope: service.delete_job(41, scope=scope)),
    (
        "execute_step_replay",
        lambda service, scope: service.execute_step_replay(41, None, scope=scope),
    ),
]


@pytest.mark.parametrize(("name", "call"), _JOB_WRITES, ids=[name for name, _ in _JOB_WRITES])
@pytest.mark.parametrize("project_id", [PROJECT_Y, PROJECT_Z, None])
def test_a_job_write_is_refused_where_the_role_does_not_allow_it(name, call, project_id) -> None:
    service, bl = _jobs(project_id)

    with pytest.raises(ServiceForbiddenError):
        call(service, _limited())

    bl.acknowledge_job.assert_not_called()
    bl.unacknowledge_job.assert_not_called()


def test_acknowledging_a_job_in_x_is_allowed() -> None:
    service, bl = _jobs(PROJECT_X)

    service.acknowledge_job(41, JobAcknowledgeInput(actor="pat"), scope=_limited())
    service.unacknowledge_job(41, scope=_limited())

    bl.acknowledge_job.assert_called_once_with("41", "pat")
    bl.unacknowledge_job.assert_called_once_with("41")


def test_a_person_without_a_limit_acknowledges_in_any_project() -> None:
    service, bl = _jobs(PROJECT_Z)

    service.acknowledge_job(41, JobAcknowledgeInput(actor="pat"), scope=_unlimited())

    bl.acknowledge_job.assert_called_once()


# --- approvals ----------------------------------------------------------------------------


class _JobRowCursor:
    def __init__(self, row: tuple) -> None:
        self._row = row

    def __enter__(self) -> _JobRowCursor:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, *_args: Any) -> None:
        return None

    def fetchone(self) -> tuple:
        return self._row


def _approvals(project_id: str):
    from core.application.management.command_services_approval import ApprovalCommandService

    row = (project_id, ACCOUNT, None, "acme.review", "42")
    conn = MagicMock()
    conn.cursor.side_effect = lambda *_args, **_kwargs: _JobRowCursor(row)
    bl = MagicMock()
    bl.conn = conn
    return ApprovalCommandService(
        resume_shutdown_in_progress=lambda: False,
        get_checkpointer=lambda: object(),
        get_logger=lambda: bl,
        enqueue_workflow_resume=lambda **_: None,
        workflow_resume_repository_factory=lambda _: None,
    )


@pytest.mark.parametrize("project_id", [PROJECT_Y, PROJECT_Z])
def test_an_approval_decision_is_refused_outside_the_persons_deciding_projects(
    project_id,
) -> None:
    service = _approvals(project_id)

    with pytest.raises(ServiceForbiddenError):
        service.resume_task(
            thread_id="thread-42",
            scope=_limited(),
            decision=ApprovalDecisionInput(approved=True, approval_id="gate-1"),
        )


@pytest.mark.parametrize("scope", [_limited(), _unlimited()], ids=["limited-in-x", "unlimited"])
def test_an_approval_decision_in_x_passes_the_project_check(scope) -> None:
    service = _approvals(PROJECT_X)

    try:
        service.resume_task(
            thread_id="thread-42",
            scope=scope,
            decision=ApprovalDecisionInput(approved=True, approval_id="gate-1"),
        )
    except ServiceForbiddenError as exc:  # pragma: no cover - the failure being tested
        pytest.fail(f"the project check refused a decision it must allow: {exc}")
    except Exception:
        pass


class _NoApprovers:
    def cursor(self, *_args: Any, **_kwargs: Any) -> Any:
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchall.return_value = []
        cursor.description = [("user_id",)]
        return cursor


def test_the_step_approver_check_respects_the_limit() -> None:
    conn = _NoApprovers()
    kwargs = {"workflow_ids": ["acme.review"], "step_id": "review"}

    assert require_step_approver(conn, _limited(), project_id=PROJECT_X, **kwargs) == frozenset()
    for project_id in (PROJECT_Y, PROJECT_Z, None):
        with pytest.raises(ServiceForbiddenError):
            require_step_approver(conn, _limited(), project_id=project_id, **kwargs)
    # Without a limit, or without a project given, the check is unchanged.
    assert require_step_approver(conn, _unlimited(), project_id=PROJECT_Z, **kwargs) == frozenset()
    assert require_step_approver(conn, _limited(), **kwargs) == frozenset()


def test_the_decision_view_says_a_limited_person_cannot_decide_outside_x() -> None:
    assert may_decide_in_project(_limited(), PROJECT_X) is True
    assert may_decide_in_project(_limited(), PROJECT_Y) is False
    assert may_decide_in_project(_limited(), PROJECT_Z) is False
    assert may_decide_in_project(_unlimited(), PROJECT_Z) is True
    assert assignment_view(_limited(), (), project_id=PROJECT_Y)["can_decide"] is False
    assert assignment_view(_limited(), (), project_id=PROJECT_X)["can_decide"] is True
    assert assignment_view(_limited(), ())["can_decide"] is True


class _CandidatesConn:
    """Two operators: Alice unlimited, Bob limited to operator in X and viewer in Y."""

    ALICE = "aaaaaaaa-0000-4000-8000-000000000001"
    BOB = "bbbbbbbb-0000-4000-8000-000000000002"
    CAROL = "cccccccc-0000-4000-8000-000000000003"

    def __init__(self, scoped: list[tuple[str, str | None, str | None]]) -> None:
        self.scoped = scoped

    def cursor(self, *_args: Any, **_kwargs: Any) -> Any:
        conn = self

        class _Cursor:
            description = None

            def __enter__(self) -> Any:
                return self

            def __exit__(self, *_exc: object) -> bool:
                return False

            def execute(self, sql: str, _params: Any = None) -> None:
                if "FROM role_permissions" in sql:
                    self._rows = [
                        {"key": "operator", "permission_key": key}
                        for key in sorted(OPERATOR_PERMISSIONS)
                    ] + [
                        {"key": "viewer", "permission_key": key}
                        for key in sorted(VIEWER_PERMISSIONS)
                    ]
                elif "FROM public.scoped_role_users" in sql:
                    self._rows = [{"user_id": user} for user, _project, _role in conn.scoped]
                elif "FROM public.user_roles ur" in sql:
                    self._rows = [
                        {"user_id": user, "project_id": project, "role_key": role}
                        for user, project, role in conn.scoped
                        if project
                    ]
                else:
                    self._rows = [
                        {
                            "user_id": user,
                            "username": name,
                            "display_name": name,
                            "role_key": "operator",
                        }
                        for user, name in (
                            (conn.ALICE, "Alice"),
                            (conn.BOB, "Bob"),
                            (conn.CAROL, "Carol"),
                        )
                    ]

            def fetchall(self) -> list[dict[str, Any]]:
                return list(self._rows)

        return _Cursor()


def test_decide_candidates_respect_the_limit() -> None:
    conn = _CandidatesConn(
        [
            (_CandidatesConn.BOB, PROJECT_X, "operator"),
            (_CandidatesConn.BOB, PROJECT_Y, "viewer"),
            # Carol is limited and decides nowhere: viewer in Y only.
            (_CandidatesConn.CAROL, PROJECT_Y, "viewer"),
        ]
    )

    def _names(**kwargs: Any) -> list[str]:
        return [row["display_name"] for row in decide_candidates(conn, ACCOUNT, **kwargs)]

    assert _names() == ["Alice", "Bob"]
    assert _names(project_id=PROJECT_X) == ["Alice", "Bob"]
    assert _names(project_id=PROJECT_Y) == ["Alice"]


def test_decide_candidates_without_limits_are_unchanged() -> None:
    conn = _CandidatesConn([])

    assert [row["display_name"] for row in decide_candidates(conn, ACCOUNT)] == [
        "Alice",
        "Bob",
        "Carol",
    ]


def test_a_limited_person_without_projects_is_no_candidate() -> None:
    conn = _CandidatesConn([(_CandidatesConn.BOB, None, None)])

    assert [row["display_name"] for row in decide_candidates(conn, ACCOUNT)] == [
        "Alice",
        "Carol",
    ]


# --- workflow runs --------------------------------------------------------------------------


class _Stop(Exception):
    """Raised by the stub once the project check is behind the run."""


class _Runs(ProjectCommandServiceRunRequestsMixin):
    def __init__(self) -> None:
        self.bl = MagicMock()
        self.bl.get_project.side_effect = _project

    def _scope(self, scope: Any, **kwargs: Any) -> ScopeContext:
        return normalize_scope(scope, **kwargs)

    def _logger(self) -> Any:
        return self.bl

    @staticmethod
    def _require_project_in_scope(bl: Any, project_id: Any, scope: ScopeContext) -> dict:
        project = bl.get_project(project_id)
        require_project_access(scope, project)
        return project

    def _require_operator_publication(self, *_args: Any) -> None:
        raise _Stop

    def _project_run_config(self, *_args: Any, **_kwargs: Any) -> dict:
        raise _Stop


def _api_run(project_id: str) -> WorkflowRunInput:
    return WorkflowRunInput(project_id=project_id, workflow_id="acme.review")


def _builder_run(project_id: str) -> BuilderWorkflowRunInput:
    return BuilderWorkflowRunInput(
        project_id=project_id, expected_revision=1, acknowledged=True, summary="Run"
    )


@pytest.mark.parametrize("project_id", [PROJECT_Y, PROJECT_Z])
def test_a_run_is_refused_where_the_role_does_not_run_workflows(project_id) -> None:
    with pytest.raises(ServiceForbiddenError):
        _Runs().start_mcp_workflow_run(_api_run(project_id), scope=_limited())
    with pytest.raises(ServiceForbiddenError):
        _Runs().start_builder_workflow_run(
            "acme.review", _builder_run(project_id), scope=_limited()
        )


@pytest.mark.parametrize("scope", [_limited(), _unlimited()], ids=["limited-in-x", "unlimited"])
def test_a_run_in_x_passes_the_project_check(scope) -> None:
    with pytest.raises(_Stop):
        _Runs().start_mcp_workflow_run(_api_run(PROJECT_X), scope=scope)
    with pytest.raises(_Stop):
        _Runs().start_builder_workflow_run("acme.review", _builder_run(PROJECT_X), scope=scope)


# --- reports ----------------------------------------------------------------------------


def _reports(job_project: str | None = PROJECT_X) -> ReportingService:
    bl = MagicMock()
    bl.get_job.return_value = {"id": "7", "project_id": job_project}
    service = ReportingService(get_logger_only=lambda: bl, job_query_service=lambda: MagicMock())
    service._build_report = MagicMock(return_value={"summary_text": "ok", "payload": {}})
    return service


@pytest.mark.parametrize(
    ("report_type", "params"),
    [
        ("project_summary", {"project_id": PROJECT_Y}),
        ("project_stats", {"project_id": PROJECT_Z}),
        ("core_health_summary", {}),
    ],
)
def test_a_report_is_refused_where_the_role_does_not_write_reports(report_type, params) -> None:
    service = _reports()

    with pytest.raises(ServiceForbiddenError):
        service.report_generate(report_type, params, scope=_limited(), persist=False)

    service._build_report.assert_not_called()


@pytest.mark.parametrize("job_project", [PROJECT_Y, PROJECT_Z, None])
def test_a_job_report_is_refused_where_the_role_does_not_write_reports(job_project) -> None:
    service = _reports(job_project)

    with pytest.raises(ServiceForbiddenError):
        service.report_generate("job_summary", {"job_id": "7"}, scope=_limited(), persist=False)


@pytest.mark.parametrize("scope", [_limited(), _unlimited()], ids=["limited", "unlimited"])
def test_a_report_in_x_is_generated(scope) -> None:
    service = _reports(PROJECT_X)

    service.report_generate(
        "project_summary", {"project_id": PROJECT_X}, scope=scope, persist=False
    )
    service.report_generate("job_summary", {"job_id": "7"}, scope=scope, persist=False)

    assert service._build_report.call_count == 2


# --- extension actions and queries -------------------------------------------------------


def _extension_client(scope: ScopeContext) -> tuple[TestClient, MagicMock]:
    from core.application.management.command_models import (
        ExtensionActionResultView,
        ExtensionQueryResultView,
    )
    from core.entrypoints.api.reactions_extensions import create_reactions_extensions_router

    binding = MagicMock()
    binding.execute_action.return_value = ExtensionActionResultView(
        extension_id="acme", action_id="sync", action={"status": "succeeded"}
    )
    binding.execute_query.return_value = ExtensionQueryResultView(
        extension_id="acme", query_id="list"
    )

    def _service_error(exc: ServiceError, _request: Any = None) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=exc.status_code)

    def _api_error(exc: Exception, request: Any = None) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=500)

    app = FastAPI()
    app.include_router(
        create_reactions_extensions_router(
            event_query_service=lambda: MagicMock(),
            event_command_service=lambda: MagicMock(),
            job_query_service=lambda: MagicMock(),
            extension_query_service=lambda: MagicMock(),
            extension_binding_service=lambda: binding,
            tool_execution_service=lambda: MagicMock(),
            command_dispatch_service=lambda: MagicMock(),
            request_scope=lambda _request, **kwargs: normalize_scope(scope, **kwargs),
            service_error_response=_service_error,
            api_error_response=_api_error,
        )
    )
    return TestClient(app), binding


@pytest.mark.parametrize("kind", ["action/sync", "query/list"])
@pytest.mark.parametrize(
    ("context", "expected"),
    [
        ({"project_id": PROJECT_Y}, 403),
        ({"project_id": PROJECT_Z}, 403),
        ({}, 403),
    ],
)
def test_an_extension_operation_is_refused_where_the_role_does_not_use_it(
    kind, context, expected
) -> None:
    client, binding = _extension_client(_limited())

    response = client.post(f"/api/extensions/acme/{kind}", json={"context": context})

    assert response.status_code == expected
    binding.execute_action.assert_not_called()
    binding.execute_query.assert_not_called()


def test_an_extension_action_in_x_runs() -> None:
    client, binding = _extension_client(_limited())

    response = client.post(
        "/api/extensions/acme/action/sync", json={"context": {"project_id": PROJECT_X}}
    )

    assert response.status_code == 200
    binding.execute_action.assert_called_once()


def test_an_extension_action_without_a_project_runs_for_a_person_without_a_limit() -> None:
    client, binding = _extension_client(_unlimited())

    response = client.post("/api/extensions/acme/action/sync", json={})

    assert response.status_code == 200
    binding.execute_action.assert_called_once()


# --- minting a token ------------------------------------------------------------------


def _limited_caller(**extra: Any) -> dict[str, Any]:
    return {
        "id": PERSON,
        "roles": ["operator"],
        "auth_source": "local",
        "allowed_project_ids": [PROJECT_X, PROJECT_Y],
        "project_permissions": {
            PROJECT_X: sorted(OPERATOR_PERMISSIONS),
            PROJECT_Y: sorted(VIEWER_PERMISSIONS),
        },
        **extra,
    }


def test_a_limited_person_mints_a_token_that_follows_their_limit() -> None:
    from core.application.auth.principal import enforce_token_scope_within_caller_authority

    # No project list: the token reaches what its owner reaches, on every request.
    enforce_token_scope_within_caller_authority(
        _limited_caller(),
        requested_scopes=["read:*"],
        requested_project_ids=[],
        requested_workflow_ids=[],
    )
    enforce_token_scope_within_caller_authority(
        _limited_caller(),
        requested_scopes=["read:*"],
        requested_project_ids=[PROJECT_X],
        requested_workflow_ids=[],
    )


def test_a_limited_person_cannot_name_a_project_outside_their_limit() -> None:
    from core.application.auth.principal import (
        TokenAuthorityError,
        enforce_token_scope_within_caller_authority,
    )

    with pytest.raises(TokenAuthorityError):
        enforce_token_scope_within_caller_authority(
            _limited_caller(),
            requested_scopes=["read:*"],
            requested_project_ids=[PROJECT_X, PROJECT_Z],
            requested_workflow_ids=[],
        )


def test_a_token_of_a_limited_person_still_mints_only_inside_its_own_list() -> None:
    from core.application.auth.principal import (
        TokenAuthorityError,
        enforce_token_scope_within_caller_authority,
    )

    token = _limited_caller(auth_source="pat", scopes=["read:*"])
    with pytest.raises(TokenAuthorityError):
        enforce_token_scope_within_caller_authority(
            token,
            requested_scopes=["read:*"],
            requested_project_ids=[],
            requested_workflow_ids=[],
        )


# --- the jobs WebSocket ----------------------------------------------------------------


def _ws_authenticate(user: dict[str, Any], limit_fn: Any) -> dict[str, Any]:
    import asyncio
    from types import SimpleNamespace

    from core.application.auth.principal import ResolvedPrincipal
    from core.infrastructure.runtime.serve_request_runtime import ws_authenticate

    async def _no_sleep(_seconds: float) -> None:
        return None

    return asyncio.run(
        ws_authenticate(
            SimpleNamespace(query_params={"token": "session-token"}, cookies={}),
            local_auth_enabled_fn=lambda: True,
            external_auth_enabled=False,
            auth_cookie_name="session",
            get_logger_only_fn=lambda: object(),
            auth_tables_available_fn=lambda _bl: True,
            get_user_by_session_token_fn=lambda _bl, _token: dict(user),
            normalize_live_principal_fn=lambda raw, default_auth_source: ResolvedPrincipal(
                **{"auth_source": default_auth_source, **raw}
            ),
            resolved_principal_type=ResolvedPrincipal,
            err_authentication_required="Authentication required",
            sleep_fn=_no_sleep,
            limit_principal_projects_fn=limit_fn,
        )
    )


def _ws_limit(user: dict[str, Any], bl: Any) -> Any:
    from core.application.auth.project_scope_limit import limit_principal_projects
    from core.application.auth.scoped_assignments import ScopeLimit

    limit = ScopeLimit(
        project_roles={PROJECT_X: frozenset({"operator"}), PROJECT_Y: frozenset({"viewer"})}
    )
    return limit_principal_projects(user, bl, lambda _bl, _account, _user: limit)


def test_the_jobs_websocket_limits_a_limited_persons_projects() -> None:
    user = _ws_authenticate({"id": PERSON, "roles": ["operator"]}, _ws_limit)

    assert user["allowed_project_ids"] == sorted([PROJECT_X, PROJECT_Y])


def test_the_jobs_websocket_leaves_an_admin_unlimited() -> None:
    user = _ws_authenticate(
        {"account_id": DEFAULT_ACCOUNT_ID, "id": PERSON, "roles": ["admin"]}, _ws_limit
    )

    assert user["allowed_project_ids"] == []


def test_the_jobs_websocket_refuses_when_the_limit_cannot_be_read() -> None:
    def _broken(_user: dict[str, Any], _bl: Any) -> None:
        raise RuntimeError("relation scoped_role_users does not exist")

    with pytest.raises(PermissionError):
        _ws_authenticate({"id": PERSON, "roles": ["operator"]}, _broken)


def test_the_jobs_websocket_production_wiring_applies_the_limit() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    source = (root / "core/entrypoints/api/route_registration.py").read_text(encoding="utf-8")

    assert "limit_principal_projects_fn=lambda user, bl: limit_principal_projects(" in source
    assert "user, bl, stored_scope_limit" in source
