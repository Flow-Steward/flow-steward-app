from __future__ import annotations

import json
from inspect import signature
from types import SimpleNamespace
from typing import ClassVar

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from core.entrypoints.mcp import server_api_tool, server_workflow_hook_tools
from core.entrypoints.mcp.server_api_client import AppApiError


class _RecordingClient:
    responses: ClassVar[list[object]] = []
    requests: ClassVar[list[dict[str, object]]] = []

    def __init__(self) -> None:
        pass

    def request(self, method: str, path: str, **kwargs: object) -> object:
        self.requests.append({"method": method, "path": path, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _queue_responses(*responses: object) -> None:
    _RecordingClient.responses.extend(responses)


@pytest.fixture(autouse=True)
def _caller_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_RecordingClient, "responses", [])
    monkeypatch.setattr(_RecordingClient, "requests", [])
    monkeypatch.setattr(server_api_tool, "AppApiClient", _RecordingClient)
    monkeypatch.setattr(server_api_tool, "_caller_bearer", lambda: "caller-token")
    monkeypatch.setattr(
        server_api_tool, "uuid4", lambda: SimpleNamespace(hex="generated-correlation")
    )


def test_mutation_forwards_caller_correlation_and_idempotency() -> None:
    _queue_responses({"data": {"job_id": "job-1"}})
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(headers={"X-Correlation-ID": "corr-1"})
        )
    )

    result = json.loads(
        server_api_tool.api_tool_response(
            "job_cancel",
            {
                "job_id": "job-1",
                "comment": "stop",
                "idempotency_key": "idem-1",
                "ctx": ctx,
            },
        )
    )

    assert result["meta"]["correlation_id"] == "corr-1"
    assert _RecordingClient.requests == [
        {
            "method": "POST",
            "path": "/api/jobs/job-1/cancel",
            "bearer": "caller-token",
            "params": None,
            "json_body": {
                "comment": "stop",
                "idempotency_key": "idem-1",
            },
            "correlation_id": "corr-1",
            "idempotency_key": "idem-1",
        }
    ]


@pytest.mark.parametrize(
    "files",
    [
        None,
        [
            {
                "input_name": "source_file",
                "filename": "actual.csv",
                "mime_type": "text/csv",
                "content_base64": "YQ==",
            }
        ],
    ],
)
def test_workflow_trigger_direct_launch_does_not_use_submit_task_api(files) -> None:
    _queue_responses(
        {"projects": [{"id": "project-1", "name": "alpha"}]},
        {"job_id": "job-1", "status": "pending"},
    )
    result = json.loads(
        server_api_tool.api_tool_response(
            "workflow_trigger",
            {
                "project_name": "alpha",
                "workflow_id": "stock-import",
                "payload": {"supplier": "acme"},
                "client_context": {"session": "voice-session-1"},
                "test_mode": False,
                "files": files,
            },
        )
    )
    assert result["data"]["job_id"] == "job-1"
    assert _RecordingClient.requests[1]["path"] == "/api/workflow-runs"
    assert _RecordingClient.requests[1]["bearer"] == "caller-token"
    assert _RecordingClient.requests[1]["json_body"] == {
        "project_id": "project-1",
        "workflow_id": "stock-import",
        "inputs": {"supplier": "acme"},
        "client_context": {"session": "voice-session-1"},
        "test_mode": False,
        **({"files": files} if files else {}),
    }


@pytest.mark.parametrize("target", [{}, {"entry_key": "stock", "workflow_id": "stock-import"}])
def test_workflow_trigger_rejects_missing_or_ambiguous_target_without_api_call(target) -> None:
    with pytest.raises(ToolError, match="validation_failed"):
        server_api_tool.api_tool_response("workflow_trigger", target)
    assert _RecordingClient.requests == []


def test_approval_idempotency_is_header_only() -> None:
    _queue_responses({"status": "accepted"})

    server_api_tool.api_tool_response(
        "job_approval_decide",
        {
            "thread_id": "thread-1",
            "approval_id": "approval-1",
            "approved": True,
            "idempotency_key": "idem-1",
            "comment": "ok",
            "action": "approve",
            "selected_stories": [],
        },
    )

    assert _RecordingClient.requests[0]["idempotency_key"] == "idem-1"
    assert "idempotency_key" not in _RecordingClient.requests[0]["json_body"]


def test_approval_omits_optional_selected_stories_when_absent() -> None:
    _queue_responses({"status": "accepted"})

    server_api_tool.api_tool_response(
        "job_approval_decide",
        {
            "thread_id": "thread-1",
            "approval_id": "approval-1",
            "approved": True,
            "idempotency_key": "idem-1",
            "comment": "ok",
            "action": "approve",
            "selected_stories": None,
        },
    )

    assert "selected_stories" not in _RecordingClient.requests[0]["json_body"]


def test_app_api_authorization_error_remains_fail_closed() -> None:
    _queue_responses(AppApiError(403, {"detail": "scope denied"}))

    with pytest.raises(ToolError) as raised:
        server_api_tool.api_tool_response("project_list", {})

    envelope = json.loads(str(raised.value))
    assert envelope == {
        "ok": False,
        "tool": "project_list",
        "error": {
            "code": "forbidden",
            "message": "scope denied",
            "status_code": 403,
        },
    }


@pytest.mark.parametrize(
    ("tool", "hook_id", "path"),
    [
        ("workflow_hooks_list", None, "/api/projects/project-1/workflows/invoice-flow/hooks"),
        (
            "workflow_hook_get",
            "before_invoice",
            "/api/projects/project-1/workflows/invoice-flow/hooks/before_invoice",
        ),
    ],
)
def test_public_hook_reads_keep_parent_route_caller_and_binding_payload(tool, hook_id, path):
    public = getattr(server_workflow_hook_tools, tool)
    expected_parameters = ["project_name", "workflow_id"]
    if hook_id is not None:
        expected_parameters.append("hook_id")
    assert list(signature(public).parameters) == expected_parameters
    binding = {
        "revision_id": "revision-1",
        "target_workflow_id": "hidden-child",
        "input_mapping": {"company": "company"},
        "output_mapping": {"company_name": "company"},
    }
    hook = {
        "hook_id": "before_invoice",
        "contract": {"stage_contract": {"summary": "Before invoice"}},
        "binding_revision": 1,
        "binding": binding,
    }
    payload = (
        {"project_id": "project-1", "workflow_id": "invoice-flow", "hooks": [hook]}
        if hook_id is None
        else {"project_id": "project-1", "workflow_id": "invoice-flow", **hook}
    )
    _queue_responses({"projects": [{"id": "project-1", "name": "alpha"}]}, payload)

    kwargs = {"project_name": "alpha", "workflow_id": "invoice-flow"}
    if hook_id is not None:
        kwargs["hook_id"] = hook_id
    result = json.loads(public(**kwargs))

    assert result == {
        "ok": True,
        "tool": tool,
        "data": payload,
        "meta": {"schema_version": "v1", "correlation_id": "generated-correlation"},
    }
    assert _RecordingClient.requests == [
        {
            "method": "GET",
            "path": "/api/projects",
            "bearer": "caller-token",
            "params": {"page": 1, "per_page": 200, "search": "alpha"},
        },
        {
            "method": "GET",
            "path": path,
            "bearer": "caller-token",
            "params": {},
            "json_body": None,
            "correlation_id": "generated-correlation",
            "idempotency_key": "",
        },
    ]


def test_target_404_does_not_turn_hook_binding_into_ordinary_workflow_access():
    _queue_responses(
        {"projects": [{"id": "project-1", "name": "alpha"}]},
        AppApiError(404, {"error": "Workflow not linked to project"}),
    )
    with pytest.raises(ToolError) as raised:
        server_api_tool.api_tool_response(
            "workflow_contract",
            {"project_name": "alpha", "workflow_id": "hidden-child"},
        )
    assert json.loads(str(raised.value)) == {
        "ok": False,
        "tool": "workflow_contract",
        "error": {
            "code": "not_found",
            "message": "Workflow not linked to project",
            "status_code": 404,
        },
    }
    assert _RecordingClient.requests[1]["path"] == (
        "/api/projects/project-1/workflows/hidden-child/contract"
    )


@pytest.mark.parametrize(
    ("tool", "arguments", "method", "path"),
    [
        (
            "capability_search",
            {"query": "send", "kinds": ["tool", "workflow"], "limit": 2},
            "GET",
            "/capabilities",
        ),
        (
            "capability_describe",
            {"ref": "tool:publish", "operation_id": "send"},
            "GET",
            "/capability",
        ),
        ("workflow_examples_search", {"query": "send", "limit": 3}, "GET", "/examples"),
        (
            "workflow_draft_validate",
            {
                "bundle_yaml": "unchanged YAML",
                "resolutions": [{"ref": "x", "action": "map_existing", "target_id": "y"}],
            },
            "POST",
            "/validate",
        ),
        (
            "workflow_draft_save",
            {"bundle_yaml": "unchanged YAML", "resolutions": []},
            "POST",
            "/drafts",
        ),
    ],
)
def test_authoring_project_tools_forward_original_bearer_and_public_payload(
    tool, arguments, method, path
):
    _queue_responses({"projects": [{"id": "project-1", "name": "alpha"}]}, {"workflow_id": "new"})
    result = json.loads(
        server_api_tool.api_tool_response(tool, {"project_name": "alpha", **arguments})
    )
    assert result["ok"] is True
    assert result["data"] == {"workflow_id": "new"}
    assert _RecordingClient.requests[0] == {
        "method": "GET",
        "path": "/api/projects",
        "bearer": "caller-token",
        "params": {"page": 1, "per_page": 200, "search": "alpha"},
    }
    assert _RecordingClient.requests[1] == {
        "method": method,
        "path": "/api/projects/project-1/workflow-authoring" + path,
        "bearer": "caller-token",
        "params": arguments if method == "GET" else None,
        "json_body": arguments if method == "POST" else None,
        "correlation_id": "generated-correlation",
        "idempotency_key": "",
    }


def test_authoring_schema_requires_no_project_resolution():
    _queue_responses({"format": "platform.workflow_bundle.v2"})
    server_api_tool.api_tool_response("workflow_authoring_schema", {"step_kind": "connector"})
    assert len(_RecordingClient.requests) == 1
    assert _RecordingClient.requests[0]["path"] == "/api/workflow-authoring/schema"
    assert _RecordingClient.requests[0]["params"] == {"step_kind": "connector"}


def test_authoring_error_preserves_safe_repair_issues_and_drops_unrelated_payload():
    issue = {
        "code": "bundle_yaml_invalid",
        "path": "bundle_yaml",
        "message": "Invalid YAML",
        "repair_guidance": "Repair bundle",
        "blocking": True,
        "severity": "error",
    }
    _queue_responses(
        AppApiError(
            422,
            {
                "code": "bundle_yaml_invalid",
                "message": "Invalid YAML",
                "issues": [issue],
                "bundle_yaml": "sensitive_source_excerpt",
                "trace": "internal_trace",
            },
        )
    )
    with pytest.raises(ToolError) as raised:
        server_api_tool.api_tool_response(
            "workflow_draft_save", {"project_id": "project-1", "bundle_yaml": "bad"}
        )
    envelope = json.loads(str(raised.value))
    assert envelope["error"]["code"] == "bundle_yaml_invalid"
    assert envelope["error"]["issues"] == [issue]
    assert "sensitive_source_excerpt" not in str(raised.value)
    assert "internal_trace" not in str(raised.value)


def test_authoring_conflict_retains_stable_workflow_identity():
    _queue_responses(AppApiError(409, {"error": "workflow_id_conflict", "workflow_id": "new"}))
    with pytest.raises(ToolError) as raised:
        server_api_tool.api_tool_response(
            "workflow_draft_save", {"project_id": "project-1", "bundle_yaml": "bad"}
        )
    assert json.loads(str(raised.value))["error"] == {
        "code": "workflow_id_conflict",
        "message": "workflow_id_conflict",
        "status_code": 409,
        "workflow_id": "new",
    }


def test_authoring_search_optional_kinds_none_is_not_an_empty_http_query_value():
    _queue_responses({"projects": [{"id": "project-1", "name": "alpha"}]}, {"capabilities": []})
    server_api_tool.api_tool_response(
        "capability_search", {"project_name": "alpha", "query": "send", "kinds": None, "limit": 12}
    )
    assert _RecordingClient.requests[1]["params"] == {"query": "send", "limit": 12}


def test_authoring_project_name_lookup_preserves_existing_project_read_requirement():
    _queue_responses(AppApiError(403, {"error": "Missing required token scope"}))
    with pytest.raises(ToolError) as raised:
        server_api_tool.api_tool_response(
            "workflow_draft_validate", {"project_name": "alpha", "bundle_yaml": "bad"}
        )
    assert json.loads(str(raised.value))["error"]["status_code"] == 403
    assert len(_RecordingClient.requests) == 1
    assert _RecordingClient.requests[0]["path"] == "/api/projects"
    assert _RecordingClient.requests[0]["bearer"] == "caller-token"


def test_authoring_conflict_retains_domain_code_from_real_service_envelope():
    from core.application.management.errors import ServiceConflictError
    from core.infrastructure.runtime import serve_request_runtime

    response = serve_request_runtime.service_error_response(
        ServiceConflictError(
            "Workflow already exists",
            payload={"error": "workflow_id_conflict", "workflow_id": "new"},
        ),
        error_payload=serve_request_runtime.build_error_payload_factory(
            err_request_failed="Request failed", default_correlation_id_fn=lambda: "test"
        ),
        request_correlation_id=lambda *_args, **_kwargs: "test",
        error_code_for_status=serve_request_runtime.error_code_for_status,
        err_request_failed="Request failed",
    )
    _queue_responses(AppApiError(response.status_code, json.loads(response.body)))
    with pytest.raises(ToolError) as raised:
        server_api_tool.api_tool_response(
            "workflow_draft_save", {"project_id": "project-1", "bundle_yaml": "bad"}
        )
    error = json.loads(str(raised.value))["error"]
    assert error["status_code"] == 409
    assert error["code"] == "workflow_id_conflict"
    assert error["workflow_id"] == "new"


def test_existing_tools_keep_generic_envelope_for_the_same_domain_payload():
    _queue_responses(
        AppApiError(
            409,
            {
                "code": "conflict",
                "error": "workflow_id_conflict",
                "workflow_id": "new",
                "message": "workflow_id_conflict",
                "details": {"context": {"workflow_id": "new"}},
                "status": "error",
                "correlation_id": "test",
            },
        )
    )
    with pytest.raises(ToolError) as raised:
        server_api_tool.api_tool_response("project_list", {})
    assert json.loads(str(raised.value))["error"] == {
        "code": "conflict",
        "message": "workflow_id_conflict",
        "status_code": 409,
    }


@pytest.mark.parametrize(
    "filters",
    [
        {
            "project_id": "project-1",
            "workflow_id": "release",
            "status": "failed",
            "group_by": "attention",
            "search": "blocked",
            "page": 3,
            "per_page": 12,
        },
        {
            "project_id": "",
            "workflow_id": "",
            "status": "",
            "group_by": "",
            "search": "",
            "page": 1,
            "per_page": 25,
        },
    ],
)
def test_job_list_forwards_filters_and_caller(filters):
    from core.entrypoints.mcp.server_job_tools import job_list

    _queue_responses({"jobs": [], "total": 0})
    result = json.loads(job_list(**filters))
    assert result["data"] == {"jobs": [], "total": 0}
    request = _RecordingClient.requests[0]
    assert request["path"] == "/api/jobs"
    assert request["method"] == "GET"
    assert request["bearer"] == "caller-token"
    assert request["params"] == filters


def test_pending_approvals_returns_queue_with_caller():
    from core.entrypoints.mcp.server_job_tools import job_approvals_pending

    _queue_responses([{"id": "job-1"}])
    assert json.loads(job_approvals_pending())["data"] == [{"id": "job-1"}]
    assert _RecordingClient.requests[0]["path"] == "/api/jobs/awaiting-approval"
    assert _RecordingClient.requests[0]["bearer"] == "caller-token"


@pytest.mark.parametrize("tool", ["job_list", "job_approvals_pending"])
@pytest.mark.parametrize("status", [403, 503])
def test_aggregate_job_errors_are_not_empty_queues(tool, status):
    _queue_responses(AppApiError(status, {"error": "unavailable"}))
    with pytest.raises(ToolError) as raised:
        server_api_tool.api_tool_response(tool, {})
    assert json.loads(str(raised.value))["error"]["status_code"] == status


def test_report_adapter_preserves_workflow_and_dates():
    arguments = {
        "report_type": "project_stats",
        "params": {
            "workflow_id": "release",
            "project_id": "p",
            "start_date": "2026-04-01",
            "end_date": "2026-04-03",
        },
        "persist": False,
    }
    _queue_responses({"payload": {}})
    server_api_tool.api_tool_response("report_generate", arguments)
    assert _RecordingClient.requests[0]["json_body"] == arguments


def test_apply_changes_posts_one_change_set_to_the_workflow_changes_route():
    operations = [{"op": "update_step_config", "step_id": "a", "patch": {"display_name": "B"}}]
    _queue_responses({"workflow_id": "wf_1", "revision": 4})

    result = json.loads(
        server_api_tool.api_tool_response(
            "workflow_apply_changes",
            {"workflow_id": "wf_1", "expected_revision": 3, "operations": operations},
        )
    )

    assert result["data"] == {"workflow_id": "wf_1", "revision": 4}
    assert _RecordingClient.requests == [
        {
            "method": "POST",
            "path": "/api/workflows/wf_1/changes",
            "bearer": "caller-token",
            "params": None,
            "json_body": {"expected_revision": 3, "operations": operations},
            "correlation_id": "generated-correlation",
            "idempotency_key": "",
        }
    ]


def test_group_conversion_posts_preview_and_apply_to_the_same_authenticated_api():
    for dry_run in (True, False):
        _queue_responses({"can_convert": True, "revision": 3})
        arguments = {
            "workflow_id": "wf_1",
            "group_id": "loop_a",
            "child_workflow_id": "child",
            "display_name": "Child",
            "expected_revision": 3,
            "dry_run": dry_run,
            "preview_digest": "reviewed-digest",
        }
        result = json.loads(server_api_tool.api_tool_response("workflow_convert_group", arguments))
        assert result["data"]["can_convert"]
        request = _RecordingClient.requests[-1]
        assert request["path"] == "/api/workflows/wf_1/groups/loop_a/convert-to-child"
        assert request["bearer"] == "caller-token"
        assert request["json_body"]["dry_run"] == dry_run
        assert request["json_body"]["preview_digest"] == "reviewed-digest"
        assert "group_id" not in request["json_body"]


@pytest.mark.parametrize(
    ("tool", "arguments", "path"),
    [
        (
            "workflow_apply_changes",
            {
                "workflow_id": "extension.inst.prepare",
                "expected_revision": 1,
                "operations": [{"op": "create_step", "step": {"step_id": "forged"}}],
            },
            "/api/workflows/extension.inst.prepare/changes",
        ),
        (
            "workflow_convert_group",
            {
                "workflow_id": "extension.inst.prepare",
                "group_id": "group_a",
                "child_workflow_id": "forged_child",
                "display_name": "Forged child",
                "expected_revision": 1,
                "dry_run": False,
                "preview_digest": "digest",
            },
            "/api/workflows/extension.inst.prepare/groups/group_a/convert-to-child",
        ),
    ],
)
def test_direct_mcp_source_mutators_forward_the_caller_and_keep_api_refusal_safe(
    tool, arguments, path
):
    _queue_responses(
        AppApiError(
            403,
            {
                "detail": {
                    "error": "workflow_internal_structure_protected",
                    "message": "Installed extension workflow is read-only.",
                    "errors": [{"internal_context": "synthetic diagnostic must not leave the API"}],
                }
            },
        )
    )

    with pytest.raises(ToolError) as raised:
        server_api_tool.api_tool_response(tool, arguments)

    envelope = json.loads(str(raised.value))
    assert envelope["ok"] is False
    assert envelope["error"]["status_code"] == 403
    assert "synthetic diagnostic must not leave the API" not in str(envelope)
    assert _RecordingClient.requests[0]["method"] == "POST"
    assert _RecordingClient.requests[0]["path"] == path
    assert _RecordingClient.requests[0]["bearer"] == "caller-token"


def test_apply_changes_keeps_the_refusal_issues_and_the_revision_conflict():
    issue = {
        "code": "workflow.change.reference_unavailable",
        "path": "operations[0].step.agent_id",
        "message": "The referenced agent 'x' is not available",
    }
    _queue_responses(
        AppApiError(400, {"detail": {"error": "Contract validation failed", "issues": [issue]}}),
        AppApiError(
            409,
            {
                "detail": {
                    "code": "workflow_revision_conflict",
                    "message": "Reload before saving.",
                    "workflow_id": "wf_1",
                    "expected_revision": 3,
                    "revision": 5,
                }
            },
        ),
    )
    arguments = {"workflow_id": "wf_1", "expected_revision": 3, "operations": [{"op": "x"}]}

    with pytest.raises(ToolError) as refused:
        server_api_tool.api_tool_response("workflow_apply_changes", dict(arguments))
    with pytest.raises(ToolError) as conflict:
        server_api_tool.api_tool_response("workflow_apply_changes", dict(arguments))

    assert json.loads(str(refused.value))["error"]["issues"] == [issue]
    assert json.loads(str(conflict.value))["error"] == {
        "code": "workflow_revision_conflict",
        "message": "Reload before saving.",
        "status_code": 409,
        "workflow_id": "wf_1",
        "expected_revision": 3,
        "revision": 5,
    }


def test_workflow_definition_reads_the_registry_route_with_the_callers_bearer():
    _queue_responses({"workflow_id": "wf_1", "revision": 5, "steps": [], "groups": []})

    result = json.loads(
        server_api_tool.api_tool_response("workflow_definition", {"workflow_id": "wf_1"})
    )

    assert result["data"]["revision"] == 5
    assert _RecordingClient.requests[0]["method"] == "GET"
    assert _RecordingClient.requests[0]["path"] == "/api/workflows/wf_1"
    assert _RecordingClient.requests[0]["bearer"] == "caller-token"


def test_job_status_uses_scoped_polling_endpoint_instead_of_ui_detail():
    _queue_responses({"id": "job-1", "status": "awaiting_timer", "current_step_id": "pause"})
    result = json.loads(server_api_tool.api_tool_response("job_status", {"job_id": "job-1"}))
    assert result["data"]["status"] == "awaiting_timer"
    assert _RecordingClient.requests[0]["path"] == "/api/jobs/job-1/status"
    assert _RecordingClient.requests[0]["bearer"] == "caller-token"


@pytest.mark.parametrize("tool", ["workflow_validate", "workflow_suggestions"])
def test_workflow_global_inspection_preserves_caller_without_project_lookup(tool) -> None:
    payload = {"workflow_id": "wf.global", "issues": []}
    _queue_responses(payload)

    result = json.loads(server_api_tool.api_tool_response(tool, {"workflow_id": "wf.global"}))

    assert result["data"] == payload
    assert _RecordingClient.requests == [
        {
            "method": "GET",
            "path": f"/api/workflows/wf.global/{tool.removeprefix('workflow_')}",
            "bearer": "caller-token",
            "params": {},
            "json_body": None,
            "correlation_id": "generated-correlation",
            "idempotency_key": "",
        }
    ]


@pytest.mark.parametrize("tool", ["workflow_validate", "workflow_suggestions"])
def test_workflow_global_inspection_does_not_bypass_api_workflow_restrictions(tool) -> None:
    _queue_responses(AppApiError(403, {"error": "Workflow is outside caller scope"}))

    with pytest.raises(ToolError, match="forbidden"):
        server_api_tool.api_tool_response(tool, {"workflow_id": "wf.denied"})

    assert len(_RecordingClient.requests) == 1
    assert _RecordingClient.requests[0]["bearer"] == "caller-token"
    assert _RecordingClient.requests[0]["path"] == (
        f"/api/workflows/wf.denied/{tool.removeprefix('workflow_')}"
    )


def test_portability_tools_call_the_project_workflow_routes_with_the_caller_bearer():
    projects = {"projects": [{"id": "project-1", "name": "alpha"}]}
    _queue_responses(
        projects,
        {"workflow_id": "wf_1", "bundle_yaml": "kind: bundle\n"},
        projects,
        {"workflow_id": "wf_1", "total_runs": 3},
        projects,
        {"imported": ["wf_1"]},
        projects,
        {"imported": ["wf_copy"]},
    )
    resolutions = [{"dependency_key": "agent:writer", "action": "map_existing"}]
    calls = (
        ("workflow_export", {"workflow_id": "wf_1", "include_children": True}),
        ("workflow_stats", {"workflow_id": "wf_1"}),
        (
            "workflow_import",
            {"bundle_yaml": "kind: bundle\n", "target_workflow_id": "", "resolutions": None},
        ),
        (
            "workflow_import",
            {
                "bundle_yaml": "kind: bundle\n",
                "target_workflow_id": "wf_copy",
                "resolutions": resolutions,
            },
        ),
    )
    for tool, arguments in calls:
        result = json.loads(
            server_api_tool.api_tool_response(tool, {"project_name": "alpha", **arguments})
        )
        assert result["ok"] is True, tool

    routed = [
        {key: request[key] for key in ("method", "path", "bearer", "params", "json_body")}
        for request in _RecordingClient.requests[1::2]
    ]
    assert routed == [
        {
            "method": "GET",
            "path": "/api/projects/project-1/workflows/wf_1/export",
            "bearer": "caller-token",
            "params": {"include_children": True},
            "json_body": None,
        },
        {
            "method": "GET",
            "path": "/api/projects/project-1/workflows/wf_1/stats",
            "bearer": "caller-token",
            "params": {},
            "json_body": None,
        },
        {
            "method": "POST",
            "path": "/api/projects/project-1/workflow-imports",
            "bearer": "caller-token",
            "params": None,
            # An absent optional field stays absent; the route's model owns its default.
            "json_body": {"bundle_yaml": "kind: bundle\n", "target_workflow_id": ""},
        },
        {
            "method": "POST",
            "path": "/api/projects/project-1/workflow-imports",
            "bearer": "caller-token",
            "params": None,
            "json_body": {
                "bundle_yaml": "kind: bundle\n",
                "target_workflow_id": "wf_copy",
                "resolutions": resolutions,
            },
        },
    ]


def test_a_blocked_import_names_what_to_resolve_and_how():
    # The route's own error body: blocking dependencies under details.context.errors.
    blocked = {
        "dependency_key": "workflow:mcp_paid_emails",
        "dependency_type": "workflow",
        "ref": "mcp_paid_emails",
        "status": "conflict",
        "blocking": True,
        "message": "Workflow id already exists. Choose a new target workflow id.",
        "resolution_actions": ["rename_workflow", "abort"],
        "metadata": {"code": "workflow_id_conflict", "internal": {"secret": "x"}},
    }
    _queue_responses(
        {"projects": [{"id": "project-1", "name": "alpha"}]},
        AppApiError(
            400,
            {
                "status": "error",
                "code": "invalid_request",
                "message": "Workflow import has unresolved blocking dependencies.",
                "details": {"context": {"errors": [blocked]}},
            },
        ),
    )
    with pytest.raises(ToolError) as raised:
        server_api_tool.api_tool_response(
            "workflow_import", {"project_name": "alpha", "bundle_yaml": "kind: bundle\n"}
        )
    error = json.loads(str(raised.value))["error"]
    assert error["blocking_dependencies"] == [
        {
            "dependency_key": "workflow:mcp_paid_emails",
            "dependency_type": "workflow",
            "ref": "mcp_paid_emails",
            "status": "conflict",
            "message": "Workflow id already exists. Choose a new target workflow id.",
            "resolution_actions": ["rename_workflow", "abort"],
        }
    ]


def test_authoring_schema_preserves_additive_discovery_payload():
    guidance = {
        "contract_level": "partial_guidance_and_examples",
        "models": {"group": {"persisted_location": "workflow.groups", "description": "Once"}},
        "examples": [{"id": "group_sequence", "bundle_yaml": "unchanged YAML"}],
        "change_set": {"schema_scope": "envelope_only_operations_are_open_objects"},
    }
    _queue_responses({"format": "platform.workflow_bundle.v2", "definition_guidance": guidance})
    result = json.loads(
        server_api_tool.api_tool_response(
            "workflow_authoring_schema",
            {"step_kind": "set_variable"},
        )
    )
    assert result["ok"] is True
    assert result["data"]["definition_guidance"] == guidance
    assert len(_RecordingClient.requests) == 1
    request = _RecordingClient.requests[0]
    assert request["method"] == "GET"
    assert request["path"] == "/api/workflow-authoring/schema"
    assert request["bearer"] == "caller-token"
    assert request["params"] == {"step_kind": "set_variable"}
