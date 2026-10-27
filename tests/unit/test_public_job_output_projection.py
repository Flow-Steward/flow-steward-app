"""Declared results survive compact status/detail/MCP without loading run history."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.application.management.errors import ServiceForbiddenError, ServiceUnavailableError
from core.application.management.query_models_jobs import JobDetailContractView, JobStatusView
from core.application.management.query_services_job_core_mixin import JobQueryServiceCoreMixin
from core.application.management.scope import ScopeContext
from core.application.reports.job_detail_contract import build_job_detail_contract
from core.application.reports.job_result_bundle import build_job_result_bundle

SCOPE = ScopeContext(account_id="acc_alpha", allowed_workflow_ids=frozenset({"workflow-a"}))
VALUES = ["Hello-World", False, 0, None, {"nested": [False, 0, None]}]


def _service(value, *, status="completed", artifacts=None, resolver=None):
    job = {
        "id": "job-1",
        "account_id": "acc_alpha",
        "project_id": "1",
        "selected_workflow": "workflow-a",
        "selected_workflow_display_name": "Public workflow",
        "status": status,
        "task_summary": "Typed result",
        "started_at": None,
        "finished_at": None,
        "meta": {
            "workflow_outputs": {"outputs": {"result": value}, "artifacts": artifacts or {}},
            "private_internal": "must-not-escape",
        },
    }
    reads = []

    def read_job(_job_id):
        reads.append("job")
        return job

    def read_project(_project_id):
        reads.append("project")
        return {"id": "1", "account_id": "acc_alpha"}

    logger = SimpleNamespace(
        get_job=read_job, get_project=read_project, get_job_cost_summary=lambda _: {}
    )
    service = JobQueryServiceCoreMixin(
        get_logger=lambda: logger,
        serialize_attention_job_row=dict,
        serialize_awaiting_approval_job_row=dict,
        job_attention_group=lambda *_a, **_k: "done",
        build_runtime_payload_for_api=AsyncMock(return_value={}),
        resolve_artifact=resolver,
    )
    return service, reads


@pytest.mark.parametrize("value", VALUES)
def test_authorized_status_carries_typed_declared_result_without_snapshot(value):
    service, reads = _service(value)
    result = asyncio.run(
        service.get_job_status("job-1", scope=SCOPE, include_workflow_result=True)
    ).dump()
    assert result.get("workflow_result") == {"outputs": {"result": value}, "artifacts": {}}
    assert type(result["workflow_result"]["outputs"]["result"]) is type(value)
    assert reads == ["job", "project"]
    assert "must-not-escape" not in str(result)


@pytest.mark.parametrize(
    "scope",
    [
        ScopeContext(account_id="acc_beta"),
        ScopeContext(account_id="acc_alpha", allowed_project_ids=frozenset({"2"})),
        ScopeContext(account_id="acc_alpha", allowed_workflow_ids=frozenset({"other-workflow"})),
    ],
)
def test_result_is_not_exposed_outside_account_project_or_workflow(scope):
    service, _ = _service("private-result")
    status_call = service.get_job_status("job-1", scope=scope, include_workflow_result=True)
    with pytest.raises(ServiceForbiddenError):
        asyncio.run(status_call)


@pytest.mark.parametrize("status", ["in_progress", "awaiting_timer", "failed", "cancelled"])
def test_unsuccessful_or_pending_job_does_not_expose_success_result(status):
    service, _ = _service("unfinished-result", status=status)
    result = asyncio.run(
        service.get_job_status("job-1", scope=SCOPE, include_workflow_result=True)
    ).dump()
    assert result.get("workflow_result") is None


def test_result_artifacts_reuse_job_ownership_filter():
    resolved = []

    def resolve(handle):
        resolved.append(handle)
        return {
            "artifact_id": handle,
            "project_id": "1",
            "run_id": "job:job-1" if handle == "art-own" else "job:other",
            "filename": "result.csv",
            "content_type": "text/csv",
            "object_key": "private-storage-key",
        }

    service, _ = _service(
        False,
        artifacts={"file": "artifact:art-own", "foreign": "artifact:art-foreign"},
        resolver=resolve,
    )
    result = asyncio.run(
        service.get_job_status("job-1", scope=SCOPE, include_workflow_result=True)
    ).dump()
    public = result.get("workflow_result")
    assert public is not None
    assert set(public["artifacts"]) == {"file"}
    assert public["artifacts"]["file"]["artifact_id"] == "art-own"
    assert "private-storage-key" not in str(public)
    assert set(resolved) == {"art-own", "art-foreign"}


class _CompactSource:
    def __init__(self, value):
        self.value = value
        self.calls = []

    async def get_job_status(self, job_id, *, scope, include_workflow_result=False):
        self.calls.append("status")
        assert scope is SCOPE
        assert include_workflow_result is True
        return JobStatusView(
            id=job_id,
            status="completed",
            project_id="1",
            task_summary="Typed",
            workflow_result={"outputs": {"result": self.value}, "artifacts": {}},
        )

    def get_workflow_run(self, *_args, **_kwargs):
        raise AssertionError("compact must not read checkpoint/snapshot/steps")

    def get_logs_page(self, *_args, **_kwargs):
        raise AssertionError("compact must not read full history")


@pytest.mark.parametrize("value", VALUES)
def test_exact_compact_zero_step_contract_preserves_public_result(value):
    source = _CompactSource(value)
    result = asyncio.run(
        build_job_detail_contract(
            job_id="job-1",
            scope=SCOPE,
            job_query_service=source,
            approval_query_service=None,
            get_thread_state_values=None,
            execution_step_limit=0,
            include_workflow_run=False,
        )
    )
    assert result.get("workflow_result") == {"outputs": {"result": value}, "artifacts": {}}
    assert result["workflow_run"] is None
    assert result["sections"]["execution_steps"] == []
    assert source.calls == ["status"]
    assert (
        JobDetailContractView.model_validate(result).dump()["workflow_result"]
        == result["workflow_result"]
    )


@pytest.mark.parametrize("value", VALUES)
def test_mcp_result_bundle_forwards_typed_public_result_without_run_query(value):
    source = _CompactSource(value)
    source.list_messages = lambda *_a, **_k: {"messages": []}
    source.list_logs = lambda *_a, **_k: {"logs": []}
    source.list_events = lambda *_a, **_k: {"events": []}
    result = asyncio.run(
        build_job_result_bundle(job_query_service=source, job_id="job-1", scope=SCOPE)
    )
    assert result.get("workflow_result") == {"outputs": {"result": value}, "artifacts": {}}
    assert source.calls == ["status"]


def test_public_result_redacts_nested_credentials_without_widening_token_exceptions():
    value = {"label": "safe-label", "token": 0, "nested": {"password": "x"}}
    service, _ = _service(value)
    result = asyncio.run(
        service.get_job_status("job-1", scope=SCOPE, include_workflow_result=True)
    ).dump()
    public = result.get("workflow_result")
    assert public is not None
    assert public["outputs"]["result"]["label"] == "safe-label"
    assert public["outputs"]["result"]["token"] == "[REDACTED]"
    assert public["outputs"]["result"]["nested"]["password"] == "[REDACTED]"


def test_result_artifacts_fail_closed_when_storage_lookup_is_unavailable():
    service, _ = _service(False, artifacts={"file": "artifact:art-own"})
    status_call = service.get_job_status("job-1", scope=SCOPE, include_workflow_result=True)
    with pytest.raises(ServiceUnavailableError, match="Public workflow result is unavailable"):
        asyncio.run(status_call)


def test_workflow_access_is_checked_before_artifact_resolution():
    def forbidden_resolver(_handle):
        raise AssertionError("unauthorized caller must not resolve artifact metadata")

    service, _ = _service(
        False, artifacts={"file": "artifact:art-own"}, resolver=forbidden_resolver
    )
    other = ScopeContext(account_id="acc_alpha", allowed_workflow_ids=frozenset({"other"}))
    status_call = service.get_job_status("job-1", scope=other, include_workflow_result=True)
    with pytest.raises(ServiceForbiddenError):
        asyncio.run(status_call)


def test_default_status_polling_does_not_project_results_or_resolve_artifacts():
    def forbidden_resolver(_handle):
        raise AssertionError("status polling must not resolve result artifacts")

    service, reads = _service(
        "private-result", artifacts={"file": "artifact:art-own"}, resolver=forbidden_resolver
    )
    result = asyncio.run(service.get_job_status("job-1", scope=SCOPE)).dump()
    assert result.get("workflow_result") is None
    assert reads == ["job", "project"]
