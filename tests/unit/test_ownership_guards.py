"""Ownership guard regression tests — account A cannot access account B data."""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.application.auth.config import DEFAULT_TENANT_ID
from core.application.management.command_models import (
    ProjectCreateInput,
    WorkflowReactionsUpdateInput,
)
from core.application.management.command_services import EventCommandService, ProjectCommandService
from core.application.management.errors import ServiceForbiddenError, ServiceNotFoundError
from core.application.management.guards import (
    require_account_access,
    require_project_access,
    require_workflow_access,
)
from core.application.management.query_services import (
    EventQueryService,
    JobQueryService,
    ProjectQueryService,
)
from core.application.management.scope import ScopeContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scope(account_id: str, **kwargs: Any) -> ScopeContext:
    if "allowed_project_ids" in kwargs:
        kwargs["allowed_project_ids"] = frozenset(
            str(project_id) for project_id in kwargs["allowed_project_ids"]
        )
    return ScopeContext(account_id=account_id, **kwargs)


ACCOUNT_A = "acc_alpha"
ACCOUNT_B = "acc_beta"

PROJECT_A = {"id": 1, "name": "proj-a", "account_id": ACCOUNT_A}
PROJECT_B = {"id": 2, "name": "proj-b", "account_id": ACCOUNT_B}

JOB_A = {
    "id": 10,
    "project_id": 1,
    "status": "in_progress",
    "thread_id": "t-10",
    "task_summary": "task a",
    "started_at": None,
    "finished_at": None,
    "duration_ms": 0,
    "project_name": "proj-a",
}

JOB_B = {
    "id": 20,
    "project_id": 2,
    "status": "in_progress",
    "thread_id": "t-20",
    "task_summary": "task b",
    "started_at": None,
    "finished_at": None,
    "duration_ms": 0,
    "project_name": "proj-b",
}


def _run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, object] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:
            result["error"] = exc

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]  # type: ignore[misc]
    return result["value"]


def _mock_logger(
    projects: dict[int, dict] | None = None, jobs: dict[int, dict] | None = None
) -> MagicMock:
    bl = MagicMock()
    _projects = projects or {}
    _jobs = jobs or {}

    def get_project(pid: int) -> dict | None:
        return _projects.get(int(pid))

    def get_job(jid: int) -> dict | None:
        return _jobs.get(int(jid))

    def list_projects(
        *,
        limit: int = 25,
        offset: int = 0,
        search: str | None = None,
        account_id: str | None = None,
    ) -> list:
        rows = list(_projects.values())
        if account_id:
            rows = [p for p in rows if p.get("account_id") == account_id]
        return rows[:limit]

    def count_projects(*, search: str | None = None, account_id: str | None = None) -> int:
        rows = list(_projects.values())
        if account_id:
            rows = [p for p in rows if p.get("account_id") == account_id]
        return len(rows)

    def get_projects(*, account_id: str | None = None) -> list:
        rows = list(_projects.values())
        if account_id:
            rows = [p for p in rows if p.get("account_id") == account_id]
        return rows

    def get_awaiting_approval_jobs(*, account_id: str | None = None) -> list:
        rows = list(_jobs.values())
        if account_id:
            rows = [
                j
                for j in rows
                if _projects.get(j.get("project_id"), {}).get("account_id") == account_id
            ]
        return [j for j in rows if j.get("status") == "awaiting_approval"]

    bl.get_project = get_project
    bl.get_job = get_job
    bl.list_projects = list_projects
    bl.count_projects = count_projects
    bl.get_projects = get_projects
    bl.get_awaiting_approval_jobs = get_awaiting_approval_jobs
    bl.get_job_cost_summary.return_value = {
        "tokens_prompt": 0,
        "tokens_completion": 0,
        "tokens_total": 0,
        "cost_usd": 0,
        "llm_calls": 0,
    }
    return bl


# ---------------------------------------------------------------------------
# Guard unit tests
# ---------------------------------------------------------------------------


class TestGuards:
    def test_require_account_access_allows_same_account(self):
        require_account_access(_scope(ACCOUNT_A), PROJECT_A)

    def test_require_account_access_denies_cross_account(self):
        _raises_input_173_1 = _scope(ACCOUNT_A)
        with pytest.raises(ServiceForbiddenError):
            require_account_access(_raises_input_173_1, PROJECT_B)

    def test_require_project_access_denies_cross_account(self):
        _raises_input_177_1 = _scope(ACCOUNT_A)
        with pytest.raises(ServiceForbiddenError):
            require_project_access(_raises_input_177_1, PROJECT_B)

    def test_require_account_access_denies_record_without_account_id(self):
        """Records without account_id key are inaccessible under fail-closed scoping."""
        missing_account_label = "missing-account-record"
        record_no_key = {"id": 99, "name": missing_account_label}
        _raises_input_184_1 = _scope("acc_default")
        with pytest.raises(ServiceForbiddenError):
            require_account_access(_raises_input_184_1, record_no_key)
        _raises_input_186_1 = _scope(ACCOUNT_A)
        with pytest.raises(ServiceForbiddenError):
            require_account_access(_raises_input_186_1, record_no_key)

    def test_require_account_access_denies_record_with_none_account_id(self):
        """Records with account_id=None are inaccessible."""
        missing_account_label = "missing-account-record"
        record_none = {"id": 99, "name": missing_account_label, "account_id": None}
        _raises_input_193_1 = _scope("acc_default")
        with pytest.raises(ServiceForbiddenError):
            require_account_access(_raises_input_193_1, record_none)

    def test_require_account_access_denies_non_dict_record(self):
        """Non-dict records are inaccessible."""
        _raises_input_198_1 = _scope("acc_default")
        with pytest.raises(ServiceForbiddenError):
            require_account_access(_raises_input_198_1, "not_a_dict")


# ---------------------------------------------------------------------------
# ProjectQueryService scoping
# ---------------------------------------------------------------------------


class TestProjectListScoping:
    def _service(self, bl: Any) -> ProjectQueryService:
        return ProjectQueryService(
            get_logger=lambda: bl,
            serialize_projects_grid_row=lambda p: p,
            get_queue_depth=lambda pid: 0,
            get_ready_queue_depth=lambda: 0,
        )

    def test_list_projects_returns_only_own_account(self):
        bl = _mock_logger(projects={1: PROJECT_A, 2: PROJECT_B})
        svc = self._service(bl)
        result = svc.list_projects(scope=_scope(ACCOUNT_A))
        ids = [p.id for p in result.projects]
        assert "1" in ids
        assert "2" not in ids

    def test_list_projects_account_b_sees_only_own(self):
        bl = _mock_logger(projects={1: PROJECT_A, 2: PROJECT_B})
        svc = self._service(bl)
        result = svc.list_projects(scope=_scope(ACCOUNT_B))
        ids = [p.id for p in result.projects]
        assert "2" in ids
        assert "1" not in ids

    def test_list_projects_respects_allowed_project_ids(self):
        project_a_extra = {"id": 3, "name": "proj-a-extra", "account_id": ACCOUNT_A}
        bl = _mock_logger(projects={1: PROJECT_A, 3: project_a_extra})
        svc = self._service(bl)
        result = svc.list_projects(scope=_scope(ACCOUNT_A, allowed_project_ids=frozenset({"1"})))
        ids = [p.id for p in result.projects]
        assert ids == ["1"]
        assert result.total == 1


# ---------------------------------------------------------------------------
# Project get scoping
# ---------------------------------------------------------------------------


class TestProjectGetScoping:
    def _service(self, bl: Any) -> ProjectQueryService:
        return ProjectQueryService(
            get_logger=lambda: bl,
            serialize_projects_grid_row=lambda p: p,
            get_queue_depth=lambda pid: 0,
            get_ready_queue_depth=lambda: 0,
        )

    def test_get_project_detail_allows_own_account(self):
        bl = _mock_logger(projects={1: PROJECT_A})
        svc = self._service(bl)
        result = svc.get_project_detail(1, scope=_scope(ACCOUNT_A))
        assert result.project["id"] == 1

    def test_get_project_detail_denies_cross_account(self):
        bl = _mock_logger(projects={1: PROJECT_A})
        svc = self._service(bl)
        _raises_input_296_1 = _scope(ACCOUNT_B)
        with pytest.raises(ServiceForbiddenError):
            svc.get_project_detail(1, scope=_raises_input_296_1)


# ---------------------------------------------------------------------------
# Job get scoping
# ---------------------------------------------------------------------------


class TestJobGetScoping:
    def _service(self, bl: Any) -> JobQueryService:
        return JobQueryService(
            get_logger=lambda: bl,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, **kw: "needs_attention",
            build_runtime_payload_for_api=AsyncMock(return_value={}),
        )

    def test_get_job_status_allows_own_account(self):
        bl = _mock_logger(projects={1: PROJECT_A}, jobs={10: JOB_A})
        svc = self._service(bl)
        result = _run_async(svc.get_job_status(10, scope=_scope(ACCOUNT_A)))
        assert result.id == "10"

    def test_get_job_status_denies_cross_account(self):
        bl = _mock_logger(projects={1: PROJECT_A}, jobs={10: JOB_A})
        svc = self._service(bl)
        _raises_input_324_1 = svc.get_job_status(10, scope=_scope(ACCOUNT_B))
        with pytest.raises(ServiceForbiddenError):
            _run_async(_raises_input_324_1)

    def test_get_job_not_found(self):
        bl = _mock_logger()
        svc = self._service(bl)
        _raises_input_330_1 = svc.get_job_status(999, scope=_scope(ACCOUNT_A))
        with pytest.raises(ServiceNotFoundError):
            _run_async(_raises_input_330_1)


# ---------------------------------------------------------------------------
# Awaiting-approval job list scoping
# ---------------------------------------------------------------------------


class TestAwaitingApprovalScoping:
    def _service(self, bl: Any) -> JobQueryService:
        return JobQueryService(
            get_logger=lambda: bl,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, **kw: "needs_attention",
            build_runtime_payload_for_api=AsyncMock(return_value={}),
        )

    def test_awaiting_approval_filtered_by_account(self):
        approval_job_a = {
            **JOB_A,
            "status": "awaiting_approval",
            "account_id": ACCOUNT_A,
            "tenant_id": DEFAULT_TENANT_ID,
        }
        approval_job_b = {
            **JOB_B,
            "status": "awaiting_approval",
            "account_id": ACCOUNT_B,
            "tenant_id": DEFAULT_TENANT_ID,
        }
        bl = _mock_logger(
            projects={1: PROJECT_A, 2: PROJECT_B},
            jobs={10: approval_job_a, 20: approval_job_b},
        )
        bl.get_awaiting_approval_jobs = MagicMock(
            return_value=[
                approval_job_a,
                approval_job_b,
                {**approval_job_a, "id": 30, "tenant_id": "foreign-tenant"},
                {**JOB_A, "id": 40, "status": "awaiting_approval"},
            ]
        )
        svc = self._service(bl)
        result = svc.list_awaiting_approval_jobs(scope=_scope(ACCOUNT_A))
        ids = [j.id for j in result]
        assert ids == ["10"]
        bl.get_awaiting_approval_jobs.assert_called_once_with(account_id=ACCOUNT_A)


# ---------------------------------------------------------------------------
# Project mutation scoping
# ---------------------------------------------------------------------------


class TestProjectMutationScoping:
    def _service(self, bl: Any) -> ProjectCommandService:
        return ProjectCommandService(
            get_logger=lambda: bl,
        )

    def test_delete_project_denies_cross_account(self):
        bl = _mock_logger(projects={1: PROJECT_A})
        bl.delete_project = MagicMock()
        svc = self._service(bl)
        _raises_input_378_1 = _scope(ACCOUNT_B)
        with pytest.raises(ServiceForbiddenError):
            svc.delete_project(1, scope=_raises_input_378_1)
        bl.delete_project.assert_not_called()

    def test_delete_project_allows_own_account(self):
        bl = _mock_logger(projects={1: PROJECT_A})
        mock_cur = MagicMock()
        bl.conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        bl.conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        svc = self._service(bl)
        result = svc.delete_project(1, scope=_scope(ACCOUNT_A))
        assert result.project_id == "1"
        mock_cur.execute.assert_called_once()


# ---------------------------------------------------------------------------
# Create project stamps account_id
# ---------------------------------------------------------------------------


class TestCreateProjectAccountStamping:
    def test_create_project_passes_scope_account_id(self):
        bl = MagicMock()
        bl.add_project.return_value = 99
        bl.get_project.return_value = {"id": 99, "name": "new-proj", "account_id": ACCOUNT_A}
        svc = ProjectCommandService(get_logger=lambda: bl)
        svc.create_project(
            ProjectCreateInput(name="new-proj", workspace_id="workspace-1"),
            scope=_scope(ACCOUNT_A),
        )
        call_kwargs = bl.add_project.call_args
        assert call_kwargs.kwargs.get("account_id") == ACCOUNT_A or (
            len(call_kwargs.args) > 0 and ACCOUNT_A in str(call_kwargs)
        )


# ---------------------------------------------------------------------------
# Membership guard tests (allowed_project_ids / allowed_workflow_ids)
# ---------------------------------------------------------------------------


class TestMembershipGuards:
    def test_require_project_access_allows_when_no_membership_restriction(self):
        """Empty allowed_project_ids means unrestricted within the account."""
        require_project_access(_scope(ACCOUNT_A), PROJECT_A)

    def test_require_project_access_allows_listed_project(self):
        scope = _scope(ACCOUNT_A, allowed_project_ids=frozenset({1, 3}))
        require_project_access(scope, PROJECT_A)

    def test_require_project_access_denies_unlisted_project(self):
        scope = _scope(ACCOUNT_A, allowed_project_ids=frozenset({3, 5}))
        with pytest.raises(ServiceForbiddenError):
            require_project_access(scope, PROJECT_A)

    def test_require_workflow_access_allows_when_no_restriction(self):
        require_workflow_access(_scope(ACCOUNT_A), "weekly_digest")

    def test_require_workflow_access_allows_listed_workflow(self):
        scope = _scope(
            ACCOUNT_A, allowed_workflow_ids=frozenset({"weekly_digest", "weekly_report"})
        )
        require_workflow_access(scope, "weekly_digest")

    def test_require_workflow_access_denies_unlisted_workflow(self):
        scope = _scope(ACCOUNT_A, allowed_workflow_ids=frozenset({"weekly_report"}))
        with pytest.raises(ServiceForbiddenError):
            require_workflow_access(scope, "weekly_digest")

    def test_require_workflow_access_denies_missing_identity_for_restricted_scope(self):
        scope = _scope(ACCOUNT_A, allowed_workflow_ids=frozenset({"weekly_report"}))
        with pytest.raises(ServiceForbiddenError):
            require_workflow_access(scope, "")

    def test_membership_still_requires_account_ownership(self):
        """Even with project in allowed list, wrong account is denied."""
        scope = _scope(ACCOUNT_B, allowed_project_ids=frozenset({1}))
        with pytest.raises(ServiceForbiddenError):
            require_project_access(scope, PROJECT_A)


# ---------------------------------------------------------------------------
# Workflow reactions scoping
# ---------------------------------------------------------------------------


class TestWorkflowReactionsScoping:
    def _query_service(self, bl: Any) -> EventQueryService:
        return EventQueryService(
            get_logger=lambda: bl,
            event_project_name=lambda bl, proj_id, cache: "",
            resolve_domain_event_by_ref=lambda bl, ref: None,
            serialize_domain_event_summary=lambda row, pname: row,
            serialize_domain_event_datetime=lambda v: None,
            to_json_obj=lambda v: v,
            workflow_exists=lambda wid: True,
        )

    def _command_service(self, bl: Any) -> EventCommandService:
        return EventCommandService(
            get_logger=lambda: bl,
            resolve_domain_event_by_ref=lambda bl, ref: None,
            to_json_obj=lambda v: v,
            serialize_domain_event_datetime=lambda v: None,
            workflow_exists=lambda wid: True,
        )

    def test_list_reactions_passes_account_id_to_logger(self):
        bl = MagicMock()
        bl.list_workflow_reactions.return_value = []
        svc = self._query_service(bl)
        svc.list_workflow_reactions("weekly_digest", scope=_scope(ACCOUNT_A))
        bl.list_workflow_reactions.assert_called_once_with(
            "weekly_digest",
            enabled_only=False,
            account_id=ACCOUNT_A,
        )

    def test_list_reactions_denied_by_workflow_membership(self):
        bl = MagicMock()
        bl.list_workflow_reactions.return_value = []
        svc = self._query_service(bl)
        scope = _scope(ACCOUNT_A, allowed_workflow_ids=frozenset({"weekly_report"}))
        with pytest.raises(ServiceForbiddenError):
            svc.list_workflow_reactions("weekly_digest", scope=scope)

    def test_update_reactions_passes_account_id_to_logger(self):
        bl = MagicMock()
        bl.list_workflow_reactions.return_value = []
        bl.replace_scoped_workflow_reactions.return_value = ("workflow", "", [])
        svc = self._command_service(bl)
        svc.update_workflow_reactions(
            "weekly_digest",
            WorkflowReactionsUpdateInput(reactions=[]),
            scope=_scope(ACCOUNT_A),
        )
        bl.replace_scoped_workflow_reactions.assert_called_once_with(
            "weekly_digest",
            [],
            scope_kind="workflow",
            scope_id="",
            account_id=ACCOUNT_A,
        )
        bl.list_workflow_reactions.assert_not_called()

    @pytest.mark.parametrize(
        "action",
        [
            "resume_job",
            "resume_orchestrator",
            "approve_job",
            "reject_job",
            "cancel_job",
            "retry_step",
        ],
    )
    def test_update_privileged_reactions_requires_operator_before_storage(self, action):
        bl = MagicMock()
        command = WorkflowReactionsUpdateInput(
            reactions=[
                {
                    "reaction_key": "privileged",
                    "event_type": "job.completed",
                    "action": {"type": action},
                }
            ]
        )
        svc = self._command_service(bl)
        scope = _scope(ACCOUNT_A, principal_type="viewer")
        with pytest.raises(ServiceForbiddenError, match="Operator role required"):
            svc.update_workflow_reactions("weekly_digest", command, scope=scope)
        bl.replace_scoped_workflow_reactions.assert_not_called()

    def test_update_reactions_denied_by_project_account_before_storage(self):
        bl = MagicMock()
        bl.get_project.return_value = PROJECT_B
        svc = self._command_service(bl)
        command = WorkflowReactionsUpdateInput(reactions=[])
        scope = _scope(ACCOUNT_A, project_id=2)
        with pytest.raises(ServiceForbiddenError):
            svc.update_workflow_reactions("weekly_digest", command, scope=scope)
        bl.replace_scoped_workflow_reactions.assert_not_called()

    def test_update_reactions_denied_by_workflow_membership(self):
        bl = MagicMock()
        svc = self._command_service(bl)
        scope = _scope(ACCOUNT_A, allowed_workflow_ids=frozenset({"weekly_report"}))
        _raises_input_524_1 = WorkflowReactionsUpdateInput(reactions=[])
        with pytest.raises(ServiceForbiddenError):
            svc.update_workflow_reactions("weekly_digest", _raises_input_524_1, scope=scope)


# ---------------------------------------------------------------------------
# Integration-style scoping: project list → get → job (end-to-end deny/allow)
# ---------------------------------------------------------------------------


class TestIntegrationDenyAllow:
    """Cross-account access denied at every layer in a realistic multi-step flow."""

    def _project_svc(self, bl: Any) -> ProjectQueryService:
        return ProjectQueryService(
            get_logger=lambda: bl,
            serialize_projects_grid_row=lambda p: p,
            get_queue_depth=lambda pid: 0,
            get_ready_queue_depth=lambda: 0,
        )

    def _job_svc(self, bl: Any) -> JobQueryService:
        return JobQueryService(
            get_logger=lambda: bl,
            serialize_attention_job_row=lambda row: row,
            serialize_awaiting_approval_job_row=lambda row: row,
            job_attention_group=lambda status, **kw: "needs_attention",
            build_runtime_payload_for_api=AsyncMock(return_value={}),
        )

    def _project_cmd_svc(self, bl: Any) -> ProjectCommandService:
        return ProjectCommandService(get_logger=lambda: bl)

    def test_account_b_cannot_list_account_a_projects(self):
        bl = _mock_logger(projects={1: PROJECT_A})
        result = self._project_svc(bl).list_projects(scope=_scope(ACCOUNT_B))
        assert len(result.projects) == 0

    def test_account_b_cannot_get_account_a_project_detail(self):
        bl = _mock_logger(projects={1: PROJECT_A})
        _raises_callable_568_1 = self._project_svc(bl).get_project_detail
        _raises_input_568_2 = _scope(ACCOUNT_B)
        with pytest.raises(ServiceForbiddenError):
            _raises_callable_568_1(1, scope=_raises_input_568_2)

    def test_account_b_cannot_get_account_a_job(self):
        bl = _mock_logger(projects={1: PROJECT_A}, jobs={10: JOB_A})
        _raises_input_573_1 = self._job_svc(bl).get_job_status(10, scope=_scope(ACCOUNT_B))
        with pytest.raises(ServiceForbiddenError):
            _run_async(_raises_input_573_1)

    def test_account_b_cannot_delete_account_a_project(self):
        bl = _mock_logger(projects={1: PROJECT_A})
        bl.delete_project = MagicMock()
        _raises_callable_579_1 = self._project_cmd_svc(bl).delete_project
        _raises_input_579_2 = _scope(ACCOUNT_B)
        with pytest.raises(ServiceForbiddenError):
            _raises_callable_579_1(1, scope=_raises_input_579_2)
        bl.delete_project.assert_not_called()

    def test_account_a_can_list_and_get_own_data(self):
        bl = _mock_logger(projects={1: PROJECT_A, 2: PROJECT_B}, jobs={10: JOB_A})
        scope_a = _scope(ACCOUNT_A)
        projects = self._project_svc(bl).list_projects(scope=scope_a)
        assert len(projects.projects) == 1
        detail = self._project_svc(bl).get_project_detail(1, scope=scope_a)
        assert detail.project["id"] == 1
        job = _run_async(self._job_svc(bl).get_job_status(10, scope=scope_a))
        assert job.id == "10"

    def test_service_account_with_restricted_project_ids_denied(self):
        """Service account in account A but only allowed project 3 — cannot read project 1."""
        bl = _mock_logger(projects={1: PROJECT_A})
        scope = _scope(ACCOUNT_A, allowed_project_ids=frozenset({3}))
        _raises_callable_597_1 = self._project_svc(bl).get_project_detail
        with pytest.raises(ServiceForbiddenError):
            _raises_callable_597_1(1, scope=scope)

    def test_service_account_with_matching_project_ids_allowed(self):
        bl = _mock_logger(projects={1: PROJECT_A})
        scope = _scope(ACCOUNT_A, allowed_project_ids=frozenset({1, 3}))
        detail = self._project_svc(bl).get_project_detail(1, scope=scope)
        assert detail.project["id"] == 1


# ---------------------------------------------------------------------------
# Restricted-token provider/notification guard pattern
# ---------------------------------------------------------------------------


class TestRestrictedTokenProjectGuards:
    """Verify require_project_access blocks restricted tokens for
    provider and notification routes (previously ungated)."""

    def test_restricted_token_denied_on_other_project(self):
        """Token with allowed_project_ids=[1] cannot access project 2."""
        scope = _scope(ACCOUNT_A, allowed_project_ids=frozenset({1}))
        project_2 = {"id": 2, "name": "proj-2", "account_id": ACCOUNT_A}
        with pytest.raises(ServiceForbiddenError):
            require_project_access(scope, project_2)

    def test_restricted_token_allowed_on_own_project(self):
        """Token with allowed_project_ids=[1] can access project 1."""
        scope = _scope(ACCOUNT_A, allowed_project_ids=frozenset({1}))
        require_project_access(scope, PROJECT_A)

    def test_unrestricted_token_allowed_on_any_project_in_account(self):
        """Empty allowed_project_ids means unrestricted within account."""
        scope = _scope(ACCOUNT_A)
        for pid in [1, 99, 200]:
            require_project_access(scope, {"id": pid, "account_id": ACCOUNT_A})

    def test_restricted_token_still_blocked_cross_account(self):
        """Even if project id is in allowed list, wrong account is denied."""
        scope = _scope(ACCOUNT_B, allowed_project_ids=frozenset({1}))
        with pytest.raises(ServiceForbiddenError):
            require_project_access(scope, PROJECT_A)

    def test_principal_to_scope_preserves_restrictions(self):
        """ResolvedPrincipal → scope_from_principal → guard pipeline."""
        from core.application.auth.principal import ResolvedPrincipal
        from core.application.management.scope import scope_from_principal

        p = ResolvedPrincipal(
            username="svc-ci",
            auth_source="machine",
            principal_type="service_account",
            account_id=ACCOUNT_A,
            allowed_project_ids=["1"],
        )
        scope = scope_from_principal(p.to_scope_dict())
        require_project_access(scope, PROJECT_A)
        with pytest.raises(ServiceForbiddenError):
            require_project_access(scope, {"id": 99, "account_id": ACCOUNT_A})


# ---------------------------------------------------------------------------
# P0-1: Cross-account deny integration tests
# ---------------------------------------------------------------------------


class TestCrossAccountDeny:
    """Verify that cross-account access is impossible across all principal types."""

    def test_session_principal_cannot_access_other_account_project(self):
        from core.application.auth.principal import ResolvedPrincipal
        from core.application.management.scope import scope_from_principal

        session_user = ResolvedPrincipal(
            id="1",
            username="alice",
            auth_source="local",
            principal_type="user",
            account_id=ACCOUNT_A,
        )
        scope = scope_from_principal(session_user.to_scope_dict())
        with pytest.raises(ServiceForbiddenError):
            require_project_access(scope, PROJECT_B)

    def test_pat_principal_cannot_access_other_account(self):
        from core.application.auth.principal import ResolvedPrincipal
        from core.application.management.scope import scope_from_principal

        pat_user = ResolvedPrincipal(
            id="2",
            username="bob",
            auth_source="pat",
            principal_type="user",
            account_id=ACCOUNT_A,
        )
        scope = scope_from_principal(pat_user.to_scope_dict())
        with pytest.raises(ServiceForbiddenError):
            require_account_access(scope, {"id": 10, "account_id": ACCOUNT_B})

    def test_machine_principal_cannot_access_other_account_jobs(self):
        from core.application.auth.principal import ResolvedPrincipal
        from core.application.management.scope import scope_from_principal

        machine = ResolvedPrincipal(
            username="svc-deploy",
            auth_source="machine",
            principal_type="service_account",
            account_id=ACCOUNT_A,
        )
        scope = scope_from_principal(machine.to_scope_dict())
        job_record = {"id": 1, "account_id": ACCOUNT_B, "project_id": 2}
        with pytest.raises(ServiceForbiddenError):
            require_account_access(scope, job_record)

    def test_workflow_scope_does_not_bypass_account_boundary(self):
        scope = _scope(ACCOUNT_A, allowed_workflow_ids=frozenset({"wf_deploy"}))
        with pytest.raises(ServiceForbiddenError):
            require_project_access(scope, PROJECT_B)

    def test_empty_account_id_on_record_always_denied(self):
        scope = _scope(ACCOUNT_A)
        for bad_account in [None, "", "  ", 0, False]:
            record = {"id": 1, "account_id": bad_account}
            with pytest.raises(ServiceForbiddenError):
                require_account_access(scope, record)


# ---------------------------------------------------------------------------
# P0-1: Fail-closed — records without account_id key are inaccessible
# ---------------------------------------------------------------------------


class TestMissingAccountIdKeyFailClosed:
    """Records without account_id key are denied for every principal."""

    def test_record_missing_account_id_key_denied_by_default_account(self):
        """acc_default cannot access rows missing explicit account ownership."""
        missing_account_label = "legacy-record"
        record = {"id": 99, "name": missing_account_label}
        _raises_input_736_1 = _scope("acc_default")
        with pytest.raises(ServiceForbiddenError):
            require_account_access(_raises_input_736_1, record)

    def test_record_missing_account_id_key_denied_by_non_default_account(self):
        """Non-default accounts cannot access rows missing explicit account ownership."""
        missing_account_label = "legacy-record"
        record = {"id": 99, "name": missing_account_label}
        for acc in [ACCOUNT_A, ACCOUNT_B]:
            _raises_input_744_1 = _scope(acc)
            with pytest.raises(ServiceForbiddenError):
                require_account_access(_raises_input_744_1, record)

    def test_project_access_with_explicit_null_account_id_is_denied(self):
        """Explicit null account_id is fail-closed (different from absent key)."""
        record = {"id": 1, "name": "proj-null-account", "account_id": None}
        _raises_input_750_1 = _scope(ACCOUNT_A)
        with pytest.raises(ServiceForbiddenError):
            require_project_access(_raises_input_750_1, record)
        _raises_input_752_1 = _scope("acc_default")
        with pytest.raises(ServiceForbiddenError):
            require_project_access(_raises_input_752_1, record)


# ---------------------------------------------------------------------------
# P0-1: Cross-account deny for project connections (endpoint guard layer)
# ---------------------------------------------------------------------------


class TestProjectConnectionsOwnershipGuard:
    """The project connections guard must fetch the real project and enforce account boundary."""

    def test_require_project_access_cross_account_denied(self):
        """Account B cannot access a project belonging to Account A."""
        scope = _scope(ACCOUNT_B)
        with pytest.raises(ServiceForbiddenError):
            require_project_access(scope, PROJECT_A)

    def test_require_project_access_restricted_token_cross_project_denied(self):
        """Restricted token (allowed_project_ids={2}) cannot access project 1 in same account."""
        scope = _scope(ACCOUNT_A, allowed_project_ids=frozenset({2}))
        with pytest.raises(ServiceForbiddenError):
            require_project_access(scope, PROJECT_A)

    def test_require_project_access_own_account_allowed(self):
        """Account A can access its own project with the full project dict."""
        scope = _scope(ACCOUNT_A)
        require_project_access(scope, PROJECT_A)  # must not raise

    def test_require_project_access_real_project_dict_enforces_ownership(self):
        """The guard must receive a real project dict with account_id to enforce ownership.

        After the project connections fix, endpoints fetch the real project from DB
        (which has account_id) rather than passing a synthetic {'id': N} dict.
        This test verifies the guard works correctly with the full project record.
        """
        scope = _scope(ACCOUNT_A)
        # The endpoint now passes the real project dict — this should pass:
        require_project_access(scope, PROJECT_A)
        # And cross-account must still be denied:
        with pytest.raises(ServiceForbiddenError):
            require_project_access(scope, PROJECT_B)


# ---------------------------------------------------------------------------
# P1-1: SSE stream project-scope access control
# ---------------------------------------------------------------------------


class TestSSEProjectScopeGuard:
    """Verify events_stream enforces project membership before streaming."""

    def test_cross_account_project_filter_returns_403(self):
        """A user from account A requesting project_id belonging to account B gets 403."""
        import asyncio
        from unittest.mock import MagicMock, patch

        import core.entrypoints.api.dependencies as route_deps
        from core.entrypoints.api.events_stream import events_stream

        mock_request = MagicMock()
        mock_request.headers = {}
        mock_request.state.auth_user = {"account_id": ACCOUNT_A, "username": "alice"}

        project_b_record = {"id": 2, "name": "proj-b", "account_id": ACCOUNT_B}

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = project_b_record

        with patch.object(route_deps, "_get_logger_only", return_value=mock_bl):
            resp = asyncio.new_event_loop().run_until_complete(
                events_stream(mock_request, cursor=0, project_id=2, event_type=None)
            )

        assert resp.status_code == 403

    def test_own_project_filter_returns_streaming_response(self):
        """A user requesting their own project_id gets a StreamingResponse."""
        import asyncio
        from unittest.mock import MagicMock, patch

        from fastapi.responses import StreamingResponse

        import core.entrypoints.api.dependencies as route_deps
        from core.entrypoints.api.events_stream import events_stream

        mock_request = MagicMock()
        mock_request.headers = {}
        mock_request.state.auth_user = {"account_id": ACCOUNT_A, "username": "alice"}

        project_a_record = {"id": 1, "name": "proj-a", "account_id": ACCOUNT_A}

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = project_a_record
        mock_bl.list_events_since.return_value = []

        with patch.object(route_deps, "_get_logger_only", return_value=mock_bl):
            resp = asyncio.new_event_loop().run_until_complete(
                events_stream(mock_request, cursor=0, project_id=1, event_type=None)
            )

        assert isinstance(resp, StreamingResponse)

    def test_restricted_token_cannot_stream_unlisted_project(self):
        """A restricted token (allowed_project_ids={3}) cannot stream project 1."""
        import asyncio
        from unittest.mock import MagicMock, patch

        import core.entrypoints.api.dependencies as route_deps
        from core.entrypoints.api.events_stream import events_stream

        mock_request = MagicMock()
        mock_request.headers = {}
        mock_request.state.auth_user = {
            "account_id": ACCOUNT_A,
            "username": "svc",
            "allowed_project_ids": [3],
        }

        project_a_record = {"id": 1, "name": "proj-a", "account_id": ACCOUNT_A}

        mock_bl = MagicMock()
        mock_bl.get_project.return_value = project_a_record
        mock_bl.list_events_since.return_value = []

        with patch.object(route_deps, "_get_logger_only", return_value=mock_bl):
            resp = asyncio.new_event_loop().run_until_complete(
                events_stream(mock_request, cursor=0, project_id=1, event_type=None)
            )

        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# P0-3: Security scan block propagation
# ---------------------------------------------------------------------------

try:
    from langchain_core.messages import AIMessage as _AIMessage  # noqa: F401

    _HAS_LANGCHAIN_CORE = True
except ImportError:
    _HAS_LANGCHAIN_CORE = False


@pytest.mark.skipif(not _HAS_LANGCHAIN_CORE, reason="langchain_core not installed")
class TestScanOutboundBlock:
    """_scan_outbound_response must raise RuntimeError (not swallow) on block action."""

    def test_block_decision_raises(self):
        from unittest.mock import MagicMock, patch

        from langchain_core.messages import AIMessage

        from core.application.orchestration.agents.common import _scan_outbound_response
        from core.infrastructure.security.models import EnforcementDecision

        block_decision = EnforcementDecision(
            action="block",
            redacted_text="[BLOCKED]",
        )

        with (
            patch(
                "core.infrastructure.security.integration.scan_outbound",
                return_value=block_decision,
            ),
            patch(
                "core.application.management.scope.scope_from_state",
                return_value=MagicMock(account_id="acc_a"),
            ),
        ):
            state = {"project_id": 1, "job_id": 10}
            response = AIMessage(content="SECRET_KEY=abc123")
            with pytest.raises(RuntimeError, match="SEC-009"):
                _scan_outbound_response(state, response)

    def test_redact_decision_modifies_content(self):
        from unittest.mock import MagicMock, patch

        from langchain_core.messages import AIMessage

        from core.application.orchestration.agents.common import _scan_outbound_response
        from core.infrastructure.security.models import EnforcementDecision

        redact_decision = EnforcementDecision(
            action="redact",
            redacted_text="SECRET_KEY=[REDACTED]",
        )

        with (
            patch(
                "core.infrastructure.security.integration.scan_outbound",
                return_value=redact_decision,
            ),
            patch(
                "core.application.management.scope.scope_from_state",
                return_value=MagicMock(account_id="acc_a"),
            ),
        ):
            state = {"project_id": 1, "job_id": 10}
            response = AIMessage(content="SECRET_KEY=abc123")
            result = _scan_outbound_response(state, response)
            assert result.content == "SECRET_KEY=[REDACTED]"

    def test_allow_decision_leaves_content_unchanged(self):
        from unittest.mock import MagicMock, patch

        from langchain_core.messages import AIMessage

        from core.application.orchestration.agents.common import _scan_outbound_response
        from core.infrastructure.security.models import EnforcementDecision

        allow_decision = EnforcementDecision(action="allow", redacted_text=None)

        with (
            patch(
                "core.infrastructure.security.integration.scan_outbound",
                return_value=allow_decision,
            ),
            patch(
                "core.application.management.scope.scope_from_state",
                return_value=MagicMock(account_id="acc_a"),
            ),
        ):
            state = {"project_id": 1, "job_id": 10}
            original = "Hello, world!"
            response = AIMessage(content=original)
            result = _scan_outbound_response(state, response)
            assert result.content == original


# ---------------------------------------------------------------------------
# P0-3: account_id propagated to log_scan_event
# ---------------------------------------------------------------------------


class TestScanEventAccountIdPropagation:
    """scan_outbound / scan_inbound must forward account_id to the event logger."""

    def test_scan_outbound_passes_account_id_to_log_decision(self):
        from unittest.mock import MagicMock, patch

        from core.infrastructure.security.integration import scan_outbound

        finding_mock = MagicMock()
        scan_decision = MagicMock()
        scan_decision.action = "warn"
        scan_decision.findings = [finding_mock]
        scan_decision.redacted_text = None
        scan_decision.scan_report = None

        mock_orch = MagicMock()
        mock_orch.scan_output.return_value = scan_decision

        mock_logger_inst = MagicMock()

        # get_security_logger is imported lazily inside _log_decision
        with (
            patch("core.infrastructure.security.integration.security_enabled", return_value=True),
            patch(
                "core.infrastructure.security.integration.SecurityOrchestrator",
                return_value=mock_orch,
            ),
            patch(
                "core.infrastructure.security.integration._maybe_downgrade",
                return_value=scan_decision,
            ),
            patch(
                "core.infrastructure.security.event_logger.get_security_logger",
                return_value=mock_logger_inst,
            ),
        ):
            scan_outbound("hello", project_id=5, job_id=42, account_id="acc_alpha")

        mock_logger_inst.log_scan_event.assert_called_once()
        call_kwargs = mock_logger_inst.log_scan_event.call_args.kwargs
        assert call_kwargs.get("account_id") == "acc_alpha"
        assert call_kwargs.get("project_id") == 5
        assert call_kwargs.get("job_id") == 42

    def test_scan_inbound_passes_account_id_to_log_decision(self):
        from unittest.mock import MagicMock, patch

        from core.infrastructure.security.integration import scan_inbound

        finding_mock = MagicMock()
        scan_decision = MagicMock()
        scan_decision.action = "warn"
        scan_decision.findings = [finding_mock]
        scan_decision.redacted_text = None
        scan_decision.scan_report = None

        mock_orch = MagicMock()
        mock_orch.scan_input.return_value = scan_decision

        mock_logger_inst = MagicMock()

        with (
            patch("core.infrastructure.security.integration.security_enabled", return_value=True),
            patch(
                "core.infrastructure.security.integration.SecurityOrchestrator",
                return_value=mock_orch,
            ),
            patch(
                "core.infrastructure.security.integration._maybe_downgrade",
                return_value=scan_decision,
            ),
            patch(
                "core.infrastructure.security.event_logger.get_security_logger",
                return_value=mock_logger_inst,
            ),
        ):
            scan_inbound("injected content", project_id=3, job_id=7, account_id="acc_beta")

        mock_logger_inst.log_scan_event.assert_called_once()
        call_kwargs = mock_logger_inst.log_scan_event.call_args.kwargs
        assert call_kwargs.get("account_id") == "acc_beta"
