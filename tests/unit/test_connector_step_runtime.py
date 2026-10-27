"""Unit tests for connector step runtime dispatch."""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.application.orchestration import (
    connector_events,
    connector_grants,
    connector_tool_runtime,
)
from core.application.orchestration.run_cursor import ForEachFrame
from tests._step_run_state import step_run_state


def _run_state(steps, *, active=0, **state):
    """A run whose cursor is on ``steps[active]`` of a primed pinned definition."""
    named = [{"step_id": f"step_{index + 1}", **step} for index, step in enumerate(steps)]
    others = [step for index, step in enumerate(named) if index != active]
    return step_run_state(named[active], *others, **state)


def test_connector_projects_explicit_extension_response_retry_signal_to_top_level() -> None:
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    nested = {
        "handled": True,
        "ok": False,
        "runtime": "extension_action",
        "operation_kind": "source",
        "invocation": {
            "status": "failed",
            "response": {
                "operation_kind": "action",
                "request_semantics": "mutation",
                "failure_class": "provider",
                "retryable": True,
                "retry_after_seconds": 9,
                "definitely_no_external_effect": True,
                "external_effect_status": "failed",
                "provider_error_code": "rate_limited",
                "http_status": 429,
            },
        },
    }
    with (
        patch(
            "core.application.orchestration.connector_step_runtime._dispatch_tool_runtime",
            return_value=None,
        ),
        patch(
            "core.application.orchestration.connector_step_runtime._dispatch_extension_runtime",
            return_value=nested,
        ),
    ):
        result = execute_connector_step(
            step={"connector_id": "connector.fixture", "override": {"connector": {}}},
            state={},
        )

    assert result["operation_kind"] == "source"
    assert result["request_semantics"] == "safe_read"
    assert result["failure_class"] == "provider"
    assert result["retryable"] is True
    assert result["retry_after_seconds"] == 9.0
    assert result["definitely_no_external_effect"] is True
    assert result["external_effect_status"] == "failed"
    assert result["provider_error_code"] == "rate_limited"
    assert result["http_status"] == 429


def test_runtime_identity_uses_trusted_queued_context_when_top_level_scope_is_absent():
    from core.application.orchestration.connector_events import _runtime_ids_from_state
    from core.application.orchestration.state import OrchestratorState

    assert "trusted_runtime_context" in OrchestratorState.__annotations__

    state = _run_state(
        [{"step_id": "send-email"}],
        job_id=None,
        workflow_id=None,
        account_id=None,
        project_id=None,
        workspace_id=None,
        trusted_runtime_context={
            "job_id": "job-queued",
            "workflow_id": "wf-queued",
            "account_id": "account-queued",
            "tenant_id": "tenant-queued",
            "project_id": "project-queued",
            "workspace_id": "workspace-queued",
        },
        project_config={
            "account_id": "account-queued",
            "tenant_id": "tenant-queued",
            "project_id": "project-queued",
            "workspace_id": "workspace-queued",
            "workflow_id": "wf-queued",
        },
    )

    assert _runtime_ids_from_state(state) == {
        "job_id": "job-queued",
        "workflow_id": "wf-queued",
        "account_id": "account-queued",
        "tenant_id": "tenant-queued",
        "project_id": "project-queued",
        "workspace_id": "workspace-queued",
        "step_id": "send-email",
        "step_execution_id": state["run_cursor"]["step_execution_id"],
        "attempt": 1,
        "group_id": "",
        "for_each_id": "",
        "loop": {},
        "test_mode": False,
    }


def test_runtime_identity_rejects_untrusted_test_mode_escalation():
    from core.application.orchestration.connector_events import _runtime_ids_from_state

    runtime = _runtime_ids_from_state(
        {
            "test_mode": True,
            "meta": {"test_mode": True},
            "job_meta": {"test_mode": True},
            "trusted_runtime_context": {
                "account_id": "account-alpha",
                "project_id": "project-alpha",
                "test_mode": False,
            },
        }
    )

    assert runtime["test_mode"] is False


def test_external_effect_runtime_identity_fails_closed_with_bounded_missing_scope():
    from core.application.orchestration.connector_effects import (
        _external_effect_runtime_identity_error,
    )

    extension = {
        "compiled_manifest": {
            "external_effects": [
                {
                    "operation_id": "send_email",
                    "effect_kind": "message_delivery",
                    "channel": "email",
                }
            ]
        }
    }
    runtime_context = {
        "job_id": "job-queued",
        "workflow_id": "wf-queued",
        "account_id": "account-queued",
        "project_id": "",
        "workspace_id": "workspace-queued",
        "step_id": "send-email",
    }

    assert _external_effect_runtime_identity_error(
        extension=extension,
        operation_id="send_email",
        runtime_context=runtime_context,
    ) == {
        "code": "external_effect_runtime_identity_missing",
        "missing_fields": ["project_id"],
        "message": "External effect requires trusted runtime identity: project_id",
    }
    runtime_context["project_id"] = "project-queued"
    assert (
        _external_effect_runtime_identity_error(
            extension=extension,
            operation_id="send_email",
            runtime_context=runtime_context,
        )
        is None
    )


def test_shared_connector_store_owns_extensions_and_project_connections():
    from core.application.orchestration.connector_events import _shared_logger

    store = object()
    with patch(
        "core.infrastructure.db.platform_stores.PlatformIntegrationStore.get_shared_instance",
        return_value=store,
    ):
        assert _shared_logger() is store


def test_execute_connector_step_dispatches_tool_manifest_runtime():
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    manifest = SimpleNamespace(
        enabled=True,
        interface=SimpleNamespace(operations=[{"name": "run_report"}], function_name=""),
    )
    bl = MagicMock()
    registry = SimpleNamespace(
        tools=SimpleNamespace(get_tool=lambda tool_id: manifest if tool_id == "semrush" else None)
    )
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.application.orchestration.connector_tool_runtime._shared_logger", return_value=bl
        ),
        patch(
            "core.application.orchestration.tools.tool_resolver.invoke_manifest_tool",
            return_value={"report_id": "rep_123"},
        ) as invoke_tool,
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "connector.semrush",
                "input_mapping_jsonb": {
                    "domain": "state.inputs.domain",
                    "limit": "literal:25",
                },
            },
            state=_run_state(
                [{"step_id": "step-semrush"}],
                job_id=77,
                workflow_id="wf.reporting",
                account_id="acc_alpha",
                tenant_id="tenant_alpha",
                project_id="project_alpha",
                workspace_id="workspace_alpha",
                inputs={"domain": "example.com"},
            ),
        )

    assert result["ok"] is True
    assert result["runtime"] == "tool_manifest"
    assert result["tool_id"] == "semrush"
    assert result["function_name"] == "run_report"
    assert result["input_payload"]["domain"] == "example.com"
    assert result["input_payload"]["limit"] == 25
    invoke_tool.assert_called_once()
    runtime_context = invoke_tool.call_args.kwargs["runtime_context"]
    assert runtime_context["job_id"] == 77
    assert runtime_context["workflow_id"] == "wf.reporting"
    assert runtime_context["account_id"] == "acc_alpha"
    assert runtime_context["tenant_id"] == "tenant_alpha"
    assert runtime_context["project_id"] == "project_alpha"
    assert runtime_context["workspace_id"] == "workspace_alpha"
    assert "phase_id" not in runtime_context
    assert runtime_context["step_id"] == "step-semrush"
    assert runtime_context["source"] == "connector_step_runtime"
    assert runtime_context["logger"] is bl
    assert bl.log_event.call_count == 2
    tool_call = bl.log_event.call_args_list[0].kwargs
    tool_result = bl.log_event.call_args_list[1].kwargs
    # Outside a group the call id's scope part is the workflow itself.
    expected_call_id = "workflow:77:workflow:step-semrush:semrush:run_report"
    assert tool_call["event_type"] == "tool_call"
    assert tool_call["agent"] == "workflow_runtime"
    assert tool_call["phase"] == "step-semrush"
    assert tool_call["detail"]["tool_call_id"] == expected_call_id
    assert tool_call["detail"]["source"] == "connector_step_runtime"
    assert "example.com" in tool_call["detail"]["args_excerpt"]
    assert tool_call["detail"]["step_id"] == "step-semrush"
    assert tool_result["event_type"] == "tool_result"
    assert tool_result["detail"]["tool_call_id"] == expected_call_id
    assert tool_result["detail"]["is_error"] is False
    assert "rep_123" in tool_result["detail"]["result_excerpt"]


def test_mcp_tool_events_persist_metadata_without_argument_or_result_content():
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    argument_secret = (
        "Bearer malicious-argument-secret"  # pragma: allowlist secret - redaction sentinel
    )
    api_secret = "api_key=malicious-api-secret"  # pragma: allowlist secret - redaction sentinel
    result_secret = (
        "access_token=malicious-result-secret"  # pragma: allowlist secret - redaction sentinel
    )
    manifest = SimpleNamespace(
        enabled=True,
        implementation=SimpleNamespace(type="mcp"),
        interface=SimpleNamespace(operations=[{"name": "catalog.read"}], function_name=""),
    )
    bl = MagicMock()
    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: manifest))
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.application.orchestration.connector_tool_runtime._shared_logger", return_value=bl
        ),
        patch(
            "core.application.orchestration.tools.tool_resolver.invoke_manifest_tool",
            return_value={"body": result_secret},
        ),
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "connector.catalog",
                "input_mapping_jsonb": {
                    "authorization": argument_secret,
                    "credential": api_secret,
                },
            },
            state={"job_id": "job-sensitive"},
        )

    assert result["ok"] is True
    assert bl.log_event.call_count == 2
    tool_call = bl.log_event.call_args_list[0].kwargs
    tool_result = bl.log_event.call_args_list[1].kwargs
    assert tool_call["event_type"] == "tool_call"
    assert tool_call["detail"]["tool_name"] == "catalog"
    assert tool_call["detail"]["function_name"] == "catalog.read"
    assert "args_excerpt" not in tool_call["detail"]
    assert tool_result["event_type"] == "tool_result"
    assert tool_result["detail"]["tool_name"] == "catalog"
    assert tool_result["detail"]["function_name"] == "catalog.read"
    assert tool_result["detail"]["is_error"] is False
    assert "result_excerpt" not in tool_result["detail"]
    persisted = repr([call.kwargs for call in bl.log_event.call_args_list])
    for secret in (argument_secret, api_secret, result_secret):
        assert secret not in persisted


def test_mcp_tool_error_event_persists_metadata_without_error_or_argument_content():
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    argument_secret = "Bearer malicious-argument-secret"  # pragma: allowlist secret
    error_secret = (
        "client_secret=malicious-error-secret"  # pragma: allowlist secret - redaction sentinel
    )
    manifest = SimpleNamespace(
        enabled=True,
        implementation=SimpleNamespace(type="mcp"),
        interface=SimpleNamespace(operations=[{"name": "catalog.read"}], function_name=""),
    )
    bl = MagicMock()
    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: manifest))
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.application.orchestration.connector_tool_runtime._shared_logger", return_value=bl
        ),
        patch(
            "core.application.orchestration.tools.tool_resolver.invoke_manifest_tool",
            side_effect=RuntimeError(error_secret),
        ),
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "connector.catalog",
                "input_mapping_jsonb": {"authorization": argument_secret},
            },
            state={"job_id": "job-sensitive"},
        )

    assert result["ok"] is False
    assert bl.log_event.call_count == 2
    tool_call = bl.log_event.call_args_list[0].kwargs
    tool_result = bl.log_event.call_args_list[1].kwargs
    assert tool_call["event_type"] == "tool_call"
    assert tool_call["detail"]["tool_name"] == "catalog"
    assert tool_call["detail"]["function_name"] == "catalog.read"
    assert "args_excerpt" not in tool_call["detail"]
    assert tool_result["event_type"] == "tool_result"
    assert tool_result["detail"]["tool_name"] == "catalog"
    assert tool_result["detail"]["function_name"] == "catalog.read"
    assert tool_result["detail"]["is_error"] is True
    assert "result_excerpt" not in tool_result["detail"]
    persisted = repr([call.kwargs for call in bl.log_event.call_args_list])
    for secret in (argument_secret, error_secret):
        assert secret not in persisted


def test_tool_manifest_dispatch_keeps_trusted_identity_separate_from_tool_arguments():
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    manifest = SimpleNamespace(
        enabled=True,
        interface=SimpleNamespace(operations=[{"name": "run_report"}], function_name=""),
    )
    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: manifest))
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.application.orchestration.connector_tool_runtime._shared_logger",
            return_value=None,
        ),
        patch(
            "core.application.orchestration.tools.tool_resolver.invoke_manifest_tool",
            return_value={"ok": True},
        ) as invoke_tool,
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "connector.semrush",
                "input_mapping_jsonb": {
                    "account_id": "state.inputs.account_id",
                    "job_id": "state.inputs.job_id",
                },
            },
            state={
                "trusted_runtime_context": {
                    "account_id": "acc_trusted",
                    "job_id": "job-trusted",
                    "workflow_id": "wf-trusted",
                },
                "account_id": "acc-top-level",
                "job_id": "job-top-level",
                "inputs": {
                    "account_id": "acc-attacker",
                    "job_id": "job-attacker",
                },
            },
        )

    assert result["ok"] is True
    assert invoke_tool.call_args.kwargs["arguments"] == {
        "account_id": "acc-attacker",
        "job_id": "job-attacker",
    }
    runtime_context = invoke_tool.call_args.kwargs["runtime_context"]
    assert runtime_context["account_id"] == "acc_trusted"
    assert runtime_context["job_id"] == "job-trusted"
    assert runtime_context["workflow_id"] == "wf-trusted"


@pytest.mark.parametrize("artifact_policy_operation_id", ["invoke_tool", ""])
def test_extension_tool_manifest_uses_grant_aware_extension_action_runtime(
    artifact_policy_operation_id,
):
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    implementation = SimpleNamespace(
        type="extension",
        to_dict=lambda: {
            "type": "extension",
            "extension_key": "acme.showcase",
            "action": "invoke_tool",
        },
    )
    manifest = SimpleNamespace(
        enabled=True,
        implementation=implementation,
        interface=SimpleNamespace(
            operations=[
                {
                    "name": "demo_store_product_catalog_artifact",
                    "artifact_policy_operation_id": artifact_policy_operation_id,
                }
            ],
            function_name="",
        ),
    )
    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: manifest))
    extension_result = {
        "handled": True,
        "ok": True,
        "runtime": "extension_action",
        "invocation": {"response": {"result": {"artifact_id": "artifact-1"}}},
    }

    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.application.orchestration.connector_tool_runtime._dispatch_extension_runtime",
            return_value=extension_result,
        ) as dispatch_extension,
        patch(
            "core.application.orchestration.tools.tool_resolver.invoke_manifest_tool"
        ) as invoke_tool,
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "extension.install-1.sample_catalog_tools",
                "connector_install_id": "install-1",
                "override": {"connector": {"function_name": "demo_store_product_catalog_artifact"}},
                "input_mapping_jsonb": {"rows": "state.rows"},
            },
            state={
                "account_id": "acc-1",
                "project_id": "project-1",
                "rows": [{"sku": "one"}],
            },
        )

    assert result["ok"] is True
    invoke_tool.assert_not_called()
    dispatch_extension.assert_called_once()
    call = dispatch_extension.call_args.kwargs
    assert call["extension_id"] == "acme.showcase"
    assert call["connector_install_id"] == "install-1"
    assert call["action_id"] == "invoke_tool"
    assert call["artifact_policy_operation_id"] == artifact_policy_operation_id
    assert call["input_payload"] == {
        "tool_name": "demo_store_product_catalog_artifact",
        "arguments": {"rows": [{"sku": "one"}]},
    }


def test_execute_connector_step_maps_outputs_into_workflow_data():
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    manifest = SimpleNamespace(
        enabled=True,
        interface=SimpleNamespace(operations=[{"name": "run_report"}], function_name=""),
    )
    registry = SimpleNamespace(
        tools=SimpleNamespace(get_tool=lambda tool_id: manifest if tool_id == "semrush" else None)
    )
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.application.orchestration.connector_tool_runtime._shared_logger",
            return_value=None,
        ),
        patch(
            "core.application.orchestration.tools.tool_resolver.invoke_manifest_tool",
            return_value={"rows": [{"sku": "A1"}]},
        ),
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "connector.semrush",
                "output_mapping_jsonb": {"rows": "state.catalog_rows"},
            },
            state={},
        )

    assert result["ok"] is True
    assert result["state_patch"]["catalog_rows"] == [{"sku": "A1"}]
    assert result["state_patch"]["workflow_data"]["catalog_rows"] == [{"sku": "A1"}]


def test_execute_connector_step_maps_projected_mcp_whole_object_result() -> None:
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    actual = {
        "spreadsheetId": "sheet-1",
        "properties": {"title": "Release audit"},
        "sheets": [{"properties": {"sheetId": 0, "title": "Sheet1"}}],
        "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/sheet-1",
        "revisionId": "revision-7",
    }
    manifest = SimpleNamespace(
        enabled=True,
        interface=SimpleNamespace(
            operations=[
                {
                    "name": "get_spreadsheet",
                    "output_schema": {
                        "type": "object",
                        "properties": {"result": {"type": "object", "additionalProperties": {}}},
                        "required": ["result"],
                    },
                }
            ],
            function_name="",
        ),
    )
    registry = SimpleNamespace(
        tools=SimpleNamespace(
            get_tool=lambda tool_id: manifest if tool_id == "sheets.get_spreadsheet" else None
        )
    )
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.application.orchestration.connector_tool_runtime._shared_logger",
            return_value=None,
        ),
        patch(
            "core.application.orchestration.tools.tool_resolver.invoke_manifest_tool",
            return_value={"result": actual},
        ),
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "connector.sheets.get_spreadsheet",
                "output_mapping_jsonb": {"result": "state.sheet_metadata"},
            },
            state={},
        )

    assert result["ok"] is True
    assert result["state_patch"]["sheet_metadata"] == actual
    assert result.get("unmapped_outputs") in (None, [])


def test_execute_connector_step_runs_standard_tool_action_and_maps_output():
    from core.application.orchestration.connector_step_runtime import execute_connector_step
    from core.application.registry.builtin_standard_tools import builtin_standard_tool_manifests
    from core.application.registry.tool_manifest import ToolManifest

    manifest = ToolManifest.model_validate(builtin_standard_tool_manifests()["std.numbers"])
    registry = SimpleNamespace(
        tools=SimpleNamespace(
            get_tool=lambda tool_id: manifest if tool_id == "std.numbers" else None
        )
    )
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.application.orchestration.connector_tool_runtime._shared_logger",
            return_value=None,
        ),
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "connector.std.numbers",
                "override": {
                    "connector": {
                        "operation_id": "round",
                        "input_mapping": {
                            "value": "literal:12.345",
                            "decimals": "literal:2",
                        },
                    }
                },
                "output_mapping_jsonb": {"result": "state.rounded_value"},
            },
            state={},
        )

    assert result["ok"] is True, result
    assert result["runtime"] == "tool_manifest"
    assert result["tool_id"] == "std.numbers"
    assert result["function_name"] == "round"
    assert result["result"]["result"] == 12.35
    assert result["state_patch"]["rounded_value"] == 12.35
    assert result["state_patch"]["workflow_data"]["rounded_value"] == 12.35


@pytest.mark.parametrize(
    ("tool_id", "operation_id", "input_mapping", "state", "expected"),
    [
        (
            "std.numbers",
            "round",
            {"value": "literal:12.345", "decimals": "literal:2"},
            {},
            12.35,
        ),
        ("std.text", "upper", {"value": "literal:Hello"}, {}, "HELLO"),
        ("std.text", "lower", {"value": "literal:Hello"}, {}, "hello"),
        (
            "std.text",
            "html_to_text",
            {"value": "state.html"},
            {"html": "<p>&nbsp;</p><script>ignored()</script><p>Two words</p>"},
            "Two words",
        ),
        ("std.text", "length", {"value": "state.items"}, {"items": ["a", "b"]}, 2),
        (
            "std.json",
            "stringify",
            {"value": "state.payload"},
            {"payload": {"b": 2, "a": 1}},
            '{"a": 1, "b": 2}',
        ),
        (
            "std.json",
            "pick",
            {"value": "state.payload", "path": "literal:items.0.name"},
            {"payload": {"items": [{"name": "first"}]}},
            "first",
        ),
    ],
)
def test_execute_connector_step_runs_all_standard_tool_actions(
    tool_id,
    operation_id,
    input_mapping,
    state,
    expected,
):
    from core.application.orchestration.connector_step_runtime import execute_connector_step
    from core.application.registry.builtin_standard_tools import builtin_standard_tool_manifests
    from core.application.registry.tool_manifest import ToolManifest

    manifests = {
        key: ToolManifest.model_validate(payload)
        for key, payload in builtin_standard_tool_manifests().items()
    }
    registry = SimpleNamespace(
        tools=SimpleNamespace(get_tool=lambda requested_tool_id: manifests.get(requested_tool_id))
    )
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.application.orchestration.connector_tool_runtime._shared_logger",
            return_value=None,
        ),
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": f"connector.{tool_id}",
                "override": {
                    "connector": {
                        "operation_id": operation_id,
                        "input_mapping": input_mapping,
                    }
                },
                "output_mapping_jsonb": {"result": "state.standard_result"},
            },
            state=state,
        )

    assert result["ok"] is True, result
    assert result["runtime"] == "tool_manifest"
    assert result["tool_id"] == tool_id
    assert result["function_name"] == operation_id
    assert result["result"]["result"] == expected
    assert result["state_patch"]["standard_result"] == expected


def test_test_mode_runs_normalized_builtin_tool_without_extension_fallback():
    """A known dotted tool ID must not be reclassified as an extension probe."""
    from core.application.orchestration.connector_step_runtime import execute_connector_step
    from core.application.registry.builtin_standard_tools import builtin_standard_tool_manifests
    from core.application.registry.tool_manifest import ToolManifest

    manifest = ToolManifest.model_validate(builtin_standard_tool_manifests()["std.text"])
    registry = SimpleNamespace(
        tools=SimpleNamespace(
            get_tool=lambda tool_id: manifest if tool_id == "std.text" else None,
            list_all=lambda: [manifest],
        )
    )
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.application.orchestration.connector_tool_runtime._shared_logger",
            return_value=None,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(None, None),
        ),
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "std.text",
                "override": {
                    "connector": {
                        "operation_id": "length",
                        "input_mapping": {"value": "state.items"},
                    }
                },
                "output_mapping_jsonb": {"result": "state.standard_result"},
            },
            state={
                "items": ["a", "b"],
                "account_id": "account-alpha",
                "project_id": "project-alpha",
                "workflow_id": "workflow-alpha",
                "job_id": "job-alpha",
                "test_mode": True,
                "trusted_runtime_context": {
                    "account_id": "account-alpha",
                    "project_id": "project-alpha",
                    "workflow_id": "workflow-alpha",
                    "job_id": "job-alpha",
                    "test_mode": True,
                },
            },
        )

    assert result["ok"] is True, result
    assert result["runtime"] == "tool_manifest"
    assert result["tool_id"] == "std.text"
    assert result["function_name"] == "length"
    assert result["state_patch"]["standard_result"] == 2


def test_execute_connector_step_tool_manifest_emits_error_result_event():
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    manifest = SimpleNamespace(
        enabled=True,
        interface=SimpleNamespace(operations=[{"name": "run_report"}], function_name=""),
    )
    bl = MagicMock()
    registry = SimpleNamespace(
        tools=SimpleNamespace(get_tool=lambda tool_id: manifest if tool_id == "semrush" else None)
    )
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.application.orchestration.connector_tool_runtime._shared_logger", return_value=bl
        ),
        patch(
            "core.application.orchestration.tools.tool_resolver.invoke_manifest_tool",
            side_effect=RuntimeError("permission denied"),
        ),
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "connector.semrush",
                "input_mapping_jsonb": {"domain": "example.com"},
            },
            state=_run_state([{"step_id": "step-semrush"}], job_id=77),
        )

    assert result["ok"] is False
    assert bl.log_event.call_count == 2
    tool_result = bl.log_event.call_args_list[1].kwargs
    assert tool_result["event_type"] == "tool_result"
    assert tool_result["detail"]["is_error"] is True
    assert tool_result["detail"]["function_name"] == "run_report"
    assert (
        tool_result["detail"]["tool_call_id"]
        == "workflow:77:workflow:step-semrush:semrush:run_report"
    )
    assert "permission denied" in tool_result["detail"]["result_excerpt"]


def test_execute_connector_step_dispatches_extension_action_runtime():
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(
                {
                    "runtime": {"contract_version": "extension_host_v2"},
                    "extension_key": "provider",
                    "action_definitions": [{"action_id": "send"}],
                },
                None,
            ),
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            return_value={"status": "succeeded", "response": {"ok": True}},
        ) as run_action,
        patch(
            "core.application.orchestration.connector_extension_runtime.time.time",
            return_value=100.0,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_invocation_audit.log_host_extension_invocation"
        ) as log_invocation,
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "provider.send",
                "connector_install_id": "pi_provider_1",
                "input_mapping_jsonb": {"record_id": "record-a"},
            },
            state=_run_state(
                [{"step_id": "step-send"}],
                job_id=13,
                workflow_id="wf.notify",
                account_id="acc_alpha",
                tenant_id="tenant_alpha",
                project_id="project_alpha",
                workspace_id="workspace_alpha",
            ),
        )

    assert result["ok"] is True
    assert result["runtime"] == "extension_action"
    assert result["extension_id"] == "provider"
    assert result["action_id"] == "send"
    run_action.assert_called_once()
    assert run_action.call_args.kwargs["timeout_seconds"] == 300.0
    runtime_context = run_action.call_args.kwargs["runtime_context"]
    assert runtime_context["job_id"] == 13
    assert runtime_context["workflow_id"] == "wf.notify"
    assert runtime_context["account_id"] == "acc_alpha"
    assert runtime_context["project_id"] == "project_alpha"
    assert "phase_id" not in runtime_context
    assert runtime_context["step_id"] == "step-send"
    assert runtime_context["connector_install_id"] == "pi_provider_1"
    assert run_action.call_args.kwargs["scope"].account_id == "acc_alpha"
    assert run_action.call_args.kwargs["scope"].project_id == "project_alpha"
    log_kwargs = log_invocation.call_args.kwargs
    assert log_kwargs["action_id"] == "send"
    assert log_kwargs["request_context"]["source"] == "connector_step_runtime"
    assert log_kwargs["request_context"]["connector_install_id"] == "pi_provider_1"


def test_execute_connector_step_does_not_spawn_after_operation_deadline():
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "provider",
        "action_definitions": [{"action_id": "fetch", "kind": "source"}],
        "compiled_manifest": {"operation_manifest": [{"operation_id": "fetch", "kind": "source"}]},
    }
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(extension, None),
        ),
        patch(
            "core.application.orchestration.connector_extension_runtime.time.time",
            return_value=100.0,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action"
        ) as run_action,
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "provider.fetch",
                "connector_install_id": "pi_provider_1",
            },
            state={
                "account_id": "acc_alpha",
                "project_id": "project_alpha",
                "_operation_deadline_epoch_seconds": 99.0,
            },
        )

    assert result["ok"] is False
    assert result["detail"] == "operation_deadline_exhausted"
    assert result["retryable"] is False
    assert result["definitely_no_external_effect"] is True
    run_action.assert_not_called()


@pytest.mark.parametrize("implementation_type", ["python", "mcp"])
@pytest.mark.parametrize("tool_failure", [False, True], ids=["result", "exception"])
def test_execute_connector_step_blocks_reflected_workflow_secrets_from_tools(
    tool_failure,
    implementation_type,
):
    from core.application.orchestration import connector_step_runtime as runtime
    from core.application.registry.workflow_secret_defaults import secret_ref_fingerprint
    from core.infrastructure.security.secret_reference import SecretReference, is_secret_reference

    secret_ref = "wf:notify:credential"
    secret_value = "reflected-tool-secret-sentinel"
    manifest = SimpleNamespace(
        enabled=True,
        interface=SimpleNamespace(operations=[{"name": "send"}], function_name=""),
        implementation={"type": implementation_type},
    )
    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: manifest))
    bl = MagicMock()

    def invoke_tool(*_args, **_kwargs):
        if tool_failure:
            raise RuntimeError(f"provider rejected credential={secret_value}")
        return {"ok": True, "provider_debug": secret_value}

    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch.object(connector_tool_runtime, "_shared_logger", return_value=bl),
        patch.object(runtime, "_read_secret_ciphertext", return_value="encrypted-value"),
        patch.object(runtime, "_decrypt_secret_value", return_value=secret_value),
        patch(
            "core.application.orchestration.tools.tool_resolver.invoke_manifest_tool",
            side_effect=invoke_tool,
        ) as invoke,
    ):
        state = {
            "job_id": 13,
            "workflow_id": "wf.notify",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "runtime_context": {"account_id": "acc_alpha"},
            "_workflow_secret_ref_fingerprints": [secret_ref_fingerprint(secret_ref)],
        }
        result = runtime.execute_connector_step(
            step={
                "step_id": "send",
                "step_kind": "connector",
                "connector_id": "provider",
                "input_mapping_jsonb": {
                    "credential": SecretReference(name="credential", ref_key=secret_ref).to_wire(),
                },
                "output_mapping_jsonb": {"provider_debug": "state.provider_debug"},
            },
            state=state,
        )

    invoke.assert_called_once()
    assert result["ok"] is False
    assert result["detail"] == "extension_secret_exposure_blocked"
    assert result["result"] == {}
    assert is_secret_reference(result["input_payload"]["credential"])
    assert "state_patch" not in result
    assert "mapped_outputs" not in result
    assert "provider_debug" not in state
    assert secret_value not in repr(result)
    assert secret_value not in repr(bl.log_event.call_args_list)
    result_event = bl.log_event.call_args_list[-1].kwargs
    assert result_event["detail"]["is_error"] is True
    if implementation_type == "mcp":
        assert "result_excerpt" not in result_event["detail"]
    else:
        assert "extension_secret_exposure_blocked" in result_event["detail"]["result_excerpt"]


def test_execute_connector_step_prepares_and_finalizes_artifact_dataset_grants(
    tmp_path,
    monkeypatch,
):
    import json
    from urllib.parse import unquote, urlparse

    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    monkeypatch.setenv("FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS", "1")
    storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifact_store"))
    plane = ArtifactDatasetPlane(storage=storage)
    artifact_handle, _artifact_meta = plane.create_artifact(
        owner_scope={"account_id": "acc_alpha", "project_id": "project_alpha"},
        run_id="run_1",
        step_id="step_sync",
        filename="input.txt",
        content_type="text/plain",
        body=b"hello",
    )
    dataset_handle, _dataset_meta = plane.create_dataset(
        owner_scope={"account_id": "acc_alpha", "project_id": "project_alpha"},
        run_id="run_1",
        step_id="step_sync",
        schema={"columns": [{"name": "sku", "type": "text"}]},
        rows=[{"sku": "A1"}],
    )
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "supplier",
        "action_definitions": [{"action_id": "sync"}],
        "compiled_manifest": {
            "artifact_policies": [
                {
                    "operation_id": "sync",
                    "inputs": [
                        {
                            "kind": "artifact",
                            "binding_key": "source_file",
                            "field": "artifact_handle",
                        },
                        {
                            "kind": "dataset",
                            "binding_key": "source_rows",
                            "field": "dataset_handle",
                        },
                    ],
                    "outputs": [
                        {
                            "kind": "artifact",
                            "binding_key": "processed_file",
                            "filename": "processed.txt",
                            "content_type": "text/plain",
                            "max_size_bytes": 64,
                        },
                        {
                            "kind": "dataset",
                            "binding_key": "processed_rows",
                            "schema": {"columns": [{"name": "sku", "type": "text"}]},
                            "max_size_bytes": 128,
                        },
                    ],
                }
            ]
        },
    }

    def _write_file_url(url: str, body: bytes) -> None:
        path = Path(unquote(urlparse(url).path))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    def _run_action(*args, **kwargs):
        for descriptor in kwargs["artifact_outputs"]:
            _write_file_url(descriptor["access"]["upload_url"], b"HELLO")
        for descriptor in kwargs["dataset_outputs"]:
            _write_file_url(
                descriptor["access"]["upload_url"],
                json.dumps([{"sku": "A1"}], separators=(",", ":")).encode("utf-8"),
            )
        return {"status": "succeeded", "response": {"ok": True}}

    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(extension, None),
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            side_effect=_run_action,
        ) as run_action,
        patch(
            "core.infrastructure.extension_runtime.extension_invocation_audit.log_host_extension_invocation"
        ),
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "supplier.sync",
                "input_mapping_jsonb": {
                    "artifact_handle": f"artifact:{artifact_handle.artifact_id}",
                    "dataset_handle": f"dataset:{dataset_handle.dataset_id}",
                },
            },
            state=_run_state(
                [{"step_id": "step-sync"}],
                job_id="job_1",
                workflow_id="wf.supplier",
                account_id="acc_alpha",
                project_id="project_alpha",
                _artifact_plane=plane,
            ),
        )

    assert result["ok"] is True
    kwargs = run_action.call_args.kwargs
    assert kwargs["artifact_inputs"][0]["binding_key"] == "source_file"
    assert kwargs["dataset_inputs"][0]["binding_key"] == "source_rows"
    assert kwargs["artifact_outputs"][0]["binding_key"] == "processed_file"
    assert kwargs["dataset_outputs"][0]["binding_key"] == "processed_rows"
    assert result["grant_outputs"]["processed_file"].startswith("artifact:")
    assert result["grant_outputs"]["processed_rows"].startswith("dataset:")
    assert result["produced_artifacts"][0]["size_bytes"] == 5
    assert result["produced_artifacts"][0]["sha256"]
    assert result["produced_datasets"][0]["row_count"] == 1
    assert result["artifact_handle"] == result["grant_outputs"]["processed_file"]
    assert result["dataset_handle"] == result["grant_outputs"]["processed_rows"]


def test_wrapper_tool_selects_artifact_policy_per_function(tmp_path, monkeypatch):
    from urllib.parse import unquote, urlparse

    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    monkeypatch.setenv("FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS", "1")
    plane = ArtifactDatasetPlane(
        storage=LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifact_store"))
    )
    implementation = SimpleNamespace(
        type="extension",
        to_dict=lambda: {
            "type": "extension",
            "extension_key": "acme.shared-wrapper",
            "action": "shared_wrapper",
        },
    )
    manifest = SimpleNamespace(
        enabled=True,
        implementation=implementation,
        interface=SimpleNamespace(
            operations=[
                {"name": "measure_text"},
                {
                    "name": "store_catalog",
                    "artifact_policy_operation_id": "shared_wrapper",
                },
            ],
            function_name="",
        ),
    )
    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: manifest))
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "acme.shared-wrapper",
        "action_definitions": [{"action_id": "shared_wrapper"}],
        "compiled_manifest": {
            "artifact_policies": [
                {
                    "operation_id": "shared_wrapper",
                    "outputs": [
                        {
                            "kind": "artifact",
                            "binding_key": "catalog_artifact",
                            "filename": "catalog.csv",
                            "content_type": "text/csv",
                        }
                    ],
                }
            ]
        },
    }
    runtime_calls = []

    def _run_action(*args, **kwargs):
        runtime_calls.append(kwargs)
        if kwargs["input_payload"]["arguments"].get("upload"):
            descriptor = kwargs["artifact_outputs"][0]
            path = Path(unquote(urlparse(descriptor["access"]["upload_url"]).path))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"sku\nA1\n")
        return {"status": "succeeded", "response": {"ok": True}}

    def _execute(function_name: str, *, upload: bool = False):
        return execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "extension.install-1.shared_tools",
                "connector_install_id": "install-1",
                "override": {"connector": {"function_name": function_name}},
                "input_mapping_jsonb": {"upload": f"literal:{str(upload).lower()}"},
            },
            state=_run_state(
                [{"step_id": "step-1"}],
                job_id="job-1",
                workflow_id="workflow-1",
                account_id="account-1",
                project_id="project-1",
                _artifact_plane=plane,
            ),
        )

    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(extension, None),
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            side_effect=_run_action,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_invocation_audit.log_host_extension_invocation"
        ),
    ):
        unannotated = _execute("measure_text")
        annotated = _execute("store_catalog", upload=True)
        missing_upload = _execute("store_catalog")

    assert unannotated["ok"] is True
    assert runtime_calls[0]["action_id"] == "shared_wrapper"
    assert runtime_calls[0]["input_payload"] == {
        "tool_name": "measure_text",
        "arguments": {"upload": False},
    }
    assert runtime_calls[0]["artifact_outputs"] is None
    assert unannotated["grant_outputs"] == {}

    assert annotated["ok"] is True
    assert runtime_calls[1]["action_id"] == "shared_wrapper"
    assert runtime_calls[1]["artifact_outputs"][0]["binding_key"] == "catalog_artifact"
    assert annotated["grant_outputs"]["catalog_artifact"].startswith("artifact:")
    assert annotated["produced_artifacts"][0]["size_bytes"] == 7

    assert missing_upload["ok"] is False
    assert runtime_calls[2]["artifact_outputs"][0]["binding_key"] == "catalog_artifact"
    assert missing_upload["detail"] == "artifact_upload_missing"
    assert missing_upload["grant_outputs"] == {}


def test_artifact_policy_input_grants_separate_public_handle_fields_from_binding_keys():
    from core.application.orchestration.connector_grants import _grant_requests_from_policy

    policy = {
        "inputs": [
            {
                "kind": "artifact",
                "binding_key": "source_file_grant",
                "from_input": "artifact_handle",
            },
            {
                "kind": "dataset",
                "binding_key": "source_rows_grant",
                "from_input": "dataset_handle",
                "required": False,
            },
        ]
    }

    requests = _grant_requests_from_policy(
        policy,
        input_payload={
            "artifact_handle": "artifact:file-1",
            "dataset_handle": "dataset:rows-1",
        },
        project_state={},
    )
    inline_requests = _grant_requests_from_policy(
        policy,
        input_payload={"artifact_handle": "artifact:file-1", "inline_rows": [["sku"], ["A1"]]},
        project_state={},
    )

    assert requests["artifact_inputs"] == [
        {"artifact_handle": "artifact:file-1", "binding_key": "source_file_grant"}
    ]
    assert requests["dataset_inputs"] == [
        {"dataset_handle": "dataset:rows-1", "binding_key": "source_rows_grant"}
    ]
    assert inline_requests["dataset_inputs"] == []


def test_artifact_policy_required_public_handle_field_stays_fail_closed():
    from core.application.orchestration.connector_grants import _grant_requests_from_policy

    policy = {
        "inputs": [
            {
                "kind": "artifact",
                "binding_key": "source_file_grant",
                "from_input": "artifact_handle",
            }
        ]
    }

    with pytest.raises(ValueError, match="missing_artifact_input:source_file_grant"):
        _grant_requests_from_policy(
            policy,
            input_payload={},
            project_state={},
        )


def test_optional_dataset_policy_allows_inline_rows_to_reach_extension_action(
    tmp_path,
    monkeypatch,
):
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    monkeypatch.setenv("FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS", "1")
    plane = ArtifactDatasetPlane(
        storage=LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifact_store"))
    )
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "table_writer",
        "action_definitions": [{"action_id": "write_rows"}],
        "compiled_manifest": {
            "artifact_policies": [
                {
                    "operation_id": "write_rows",
                    "inputs": [
                        {
                            "kind": "dataset",
                            "binding_key": "source_rows_grant",
                            "from_input": "dataset_handle",
                            "required": False,
                        }
                    ],
                }
            ]
        },
    }
    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(extension, None),
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            return_value={"status": "succeeded", "response": {"ok": True}},
        ) as run_action,
        patch(
            "core.infrastructure.extension_runtime.extension_invocation_audit.log_host_extension_invocation"
        ),
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "table_writer.write_rows",
                "input_mapping_jsonb": {"inline_rows": [["sku"], ["A1"]]},
            },
            state={
                "job_id": "job_1",
                "workflow_id": "wf.table_writer",
                "account_id": "acc_alpha",
                "project_id": "project_alpha",
                "_artifact_plane": plane,
            },
        )

    assert result["ok"] is True
    assert run_action.call_args.kwargs["dataset_inputs"] == []


def test_execute_connector_step_hides_output_handles_when_grant_finalization_fails(
    tmp_path,
    monkeypatch,
):
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    monkeypatch.setenv("FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS", "1")
    plane = ArtifactDatasetPlane(
        storage=LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifact_store"))
    )
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "supplier",
        "action_definitions": [{"action_id": "sync"}],
        "compiled_manifest": {
            "artifact_policies": [
                {
                    "operation_id": "sync",
                    "outputs": [
                        {
                            "kind": "artifact",
                            "binding_key": "processed_file",
                            "filename": "processed.txt",
                            "content_type": "text/plain",
                        }
                    ],
                }
            ]
        },
    }
    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(extension, None),
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            return_value={"status": "succeeded", "response": {"ok": True}},
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_invocation_audit.log_host_extension_invocation"
        ),
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "supplier.sync",
                "input_mapping_jsonb": {},
            },
            state={
                "job_id": "job_1",
                "workflow_id": "wf.supplier",
                "account_id": "acc_alpha",
                "project_id": "project_alpha",
                "_artifact_plane": plane,
            },
        )

    assert result["ok"] is False
    assert result["detail"] == "artifact_upload_missing"
    assert result["produced_artifacts"] == []
    assert result["grant_outputs"] == {}
    assert "artifact_handle" not in result
    assert plane._artifacts == {}


def test_execute_connector_step_cleans_output_specs_when_extension_action_fails(
    tmp_path,
    monkeypatch,
):
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    monkeypatch.setenv("FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS", "1")
    plane = ArtifactDatasetPlane(
        storage=LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifact_store"))
    )
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "supplier",
        "action_definitions": [{"action_id": "sync"}],
        "compiled_manifest": {
            "artifact_policies": [
                {
                    "operation_id": "sync",
                    "outputs": [
                        {
                            "kind": "artifact",
                            "binding_key": "processed_file",
                            "filename": "processed.txt",
                            "content_type": "text/plain",
                            "retention_class": "persistent",
                        }
                    ],
                }
            ]
        },
    }
    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(extension, None),
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            return_value={"status": "failed", "detail": "upstream_failed"},
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_invocation_audit.log_host_extension_invocation"
        ),
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "supplier.sync",
                "input_mapping_jsonb": {},
            },
            state={
                "job_id": "job_1",
                "workflow_id": "wf.supplier",
                "account_id": "acc_alpha",
                "project_id": "project_alpha",
                "_artifact_plane": plane,
            },
        )

    assert result["ok"] is False
    assert result["detail"] == "upstream_failed"
    assert result["produced_artifacts"] == []
    assert result["grant_outputs"] == {}
    assert plane._artifacts == {}


def test_execute_connector_step_live_artifact_producer_then_consumer_chain(
    tmp_path,
    monkeypatch,
):
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    monkeypatch.setenv("FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS", "1")
    script = tmp_path / "main.py"
    script.write_text(
        "import json, pathlib, sys, urllib.parse\n"
        "payload = json.loads(sys.stdin.read() or '{}')\n"
        "action_id = (payload.get('action') or {}).get('action_id')\n"
        "if action_id == 'produce':\n"
        "    url = payload['artifacts']['outputs'][0]['access']['upload_url']\n"
        "    path = pathlib.Path(urllib.parse.unquote(urllib.parse.urlparse(url).path))\n"
        "    path.parent.mkdir(parents=True, exist_ok=True)\n"
        "    path.write_bytes(b'chain-body')\n"
        "    print(json.dumps({'ok': True, 'produced': True}))\n"
        "elif action_id == 'consume':\n"
        "    url = payload['artifacts']['inputs'][0]['access']['download_url']\n"
        "    path = pathlib.Path(urllib.parse.unquote(urllib.parse.urlparse(url).path))\n"
        "    print(json.dumps({'ok': True, 'seen': path.read_bytes().decode('utf-8')}))\n"
        "else:\n"
        "    print(json.dumps({'ok': False, 'detail': 'unknown action'}))\n",
        encoding="utf-8",
    )
    plane = ArtifactDatasetPlane(
        storage=LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifact_store"))
    )
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "acme.chain",
        "bundle_root": str(tmp_path),
        "entrypoint": {"command": ["python3", "main.py"]},
        "action_definitions": [
            {"action_id": "produce", "handler": {"mode": "extension"}},
            {"action_id": "consume", "handler": {"mode": "extension"}},
        ],
        "compiled_manifest": {
            "artifact_policies": [
                {
                    "operation_id": "produce",
                    "outputs": [
                        {
                            "kind": "artifact",
                            "binding_key": "produced_file",
                            "filename": "chain.txt",
                            "content_type": "text/plain",
                        }
                    ],
                },
                {
                    "operation_id": "consume",
                    "inputs": [
                        {
                            "kind": "artifact",
                            "binding_key": "source_file",
                            "field": "artifact_handle",
                        }
                    ],
                },
            ]
        },
    }
    state = {
        "job_id": "job_chain",
        "workflow_id": "wf.chain",
        "account_id": "acc_alpha",
        "project_id": "project_alpha",
        "_artifact_plane": plane,
    }
    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(extension, MagicMock()),
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_invocation_audit.log_host_extension_invocation"
        ),
    ):
        produced = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "acme.chain.produce",
                "input_mapping_jsonb": {},
                "output_mapping_jsonb": {"artifact_handle": "state.produced_artifact"},
            },
            state=state,
        )
        state.update(produced.get("state_patch") or {})
        consumed = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "acme.chain.consume",
                "input_mapping_jsonb": {"artifact_handle": "state.produced_artifact"},
                "output_mapping_jsonb": {"seen": "state.consumer_seen"},
            },
            state=state,
        )

    assert produced["ok"] is True
    assert produced["artifact_handle"].startswith("artifact:")
    assert state["produced_artifact"] == produced["artifact_handle"]
    assert consumed["ok"] is True
    assert consumed["state_patch"]["consumer_seen"] == "chain-body"


def test_execute_connector_step_denies_dataset_outside_workflow_project(
    tmp_path,
    monkeypatch,
):
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    monkeypatch.setenv("FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS", "1")
    storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifact_store"))
    plane = ArtifactDatasetPlane(storage=storage)
    dataset_handle, _dataset_meta = plane.create_dataset(
        owner_scope={"account_id": "acc_alpha", "project_id": "project_beta"},
        run_id="run_1",
        step_id="step_sync",
        schema={"columns": [{"name": "sku", "type": "text"}]},
        rows=[{"sku": "B1"}],
    )
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "supplier",
        "action_definitions": [{"action_id": "sync"}],
        "compiled_manifest": {
            "artifact_policies": [
                {
                    "operation_id": "sync",
                    "inputs": [
                        {
                            "kind": "dataset",
                            "binding_key": "source_rows",
                            "field": "dataset_handle",
                        }
                    ],
                }
            ]
        },
    }
    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(extension, None),
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action"
        ) as run_action,
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "supplier.sync",
                "input_mapping_jsonb": {
                    "dataset_handle": f"dataset:{dataset_handle.dataset_id}",
                },
            },
            state={
                "job_id": "job_1",
                "workflow_id": "wf.supplier",
                "account_id": "acc_alpha",
                "project_id": "project_alpha",
                "_artifact_plane": plane,
            },
        )

    assert result["ok"] is False
    assert result["detail"].startswith("grant_preparation_failed:")
    assert "Dataset access denied" in result["detail"]
    run_action.assert_not_called()


def test_execute_connector_step_resolves_connection_target_and_persists_session_update():
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    bl = MagicMock()
    connection_row = {
        "connection_id": "conn_provider_dev",
        "account_id": "acc_alpha",
        "project_id": "project_alpha",
        "display_name": "Provider Dev",
        "connection_type": "provider",
        "environment": "dev",
        "credential_revision": 4,
        "enabled": True,
        "extension_id": "acme.provider",
        "extension_install_id": "pi_provider_1",
        "connection_config": {"base_url": "https://api.example.test"},
        "secret_refs": {"refs": {"api_token": "project_connection:conn_provider_dev:api_token"}},
    }
    bl.get_project_connection.return_value = connection_row
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(
                {
                    "runtime": {"contract_version": "extension_host_v2"},
                    "extension_key": "acme.provider",
                    "action_definitions": [{"action_id": "send"}],
                },
                bl,
            ),
        ),
        patch(
            "core.application.management.project_connection_secrets.resolve_connection_secrets",
            return_value={"api_token": "secret-token"},
        ) as resolve_secrets,
        patch(
            "core.application.management.project_connection_sessions.get_connection_session",
            return_value={
                "payload": {"access_token": "old-token"},
                "expires_at": 1234.0,
                "refresh_after": 1200.0,
            },
        ) as get_session,
        patch(
            "core.application.management.project_connection_sessions.upsert_connection_session"
        ) as upsert_session,
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            return_value={
                "status": "succeeded",
                "response": {
                    "ok": True,
                    "session_update": {
                        "payload": {"access_token": "new-token"},
                        "expires_at": 2234.0,
                        "refresh_after": 2200.0,
                    },
                },
            },
        ) as run_action,
        patch(
            "core.infrastructure.extension_runtime.extension_invocation_audit.log_host_extension_invocation"
        ) as log_invocation,
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "acme.provider.send",
                "connector_install_id": "pi_provider_1",
                "connection_binding_jsonb": {
                    "connection_id": "conn_provider_dev",
                    "session_scope": {"scopes": ["send"]},
                    "subject_key": "workspace-alpha",
                },
                "input_mapping_jsonb": {"record_id": "record-a"},
            },
            state={
                "job_id": 13,
                "workflow_id": "wf.notify",
                "account_id": "acc_alpha",
                "project_id": "project_alpha",
            },
        )

    assert result["ok"] is True
    resolve_secrets.assert_called_once_with(
        bl,
        account_id="acc_alpha",
        secret_refs={"refs": {"api_token": "project_connection:conn_provider_dev:api_token"}},
    )
    get_session.assert_called_once()
    target = run_action.call_args.kwargs["target"]
    assert target["connection"]["connection_id"] == "conn_provider_dev"
    assert target["connection"]["account_id"] == "acc_alpha"
    assert target["connection"]["project_id"] == "project_alpha"
    assert target["connection"]["secrets"] == {"api_token": "secret-token"}
    assert target["connection"]["session"]["payload"] == {"access_token": "old-token"}
    upsert_session.assert_called_once_with(
        bl,
        connection=target["connection"],
        session_payload={"access_token": "new-token"},
        expires_at=2234.0,
        refresh_after=2200.0,
        scope={"scopes": ["send"]},
        subject_key="workspace-alpha",
        metadata={"source": "connector_step_runtime"},
    )
    logged_target = log_invocation.call_args.kwargs["request"]["target"]
    assert logged_target["connection"]["secrets"] == {"api_token": "<redacted>"}
    assert logged_target["connection"]["session"]["present"] is True
    assert "new-token" not in repr(result)
    assert "new-token" not in repr(log_invocation.call_args.kwargs["invocation"])
    assert "session_update" not in repr(log_invocation.call_args.kwargs["invocation"])


@pytest.mark.parametrize(
    "extension_invocation",
    [
        {
            "status": "succeeded",
            "response": {
                "ok": True,
                "result": {"provider_debug": "reflected-secret-sentinel"},
            },
        },
        {
            "status": "failed",
            "detail": "provider_request_failed",
            "response": {
                "ok": False,
                "errors": [
                    {
                        "code": "provider_request_failed",
                        "message": "credential=reflected-secret-sentinel",
                    }
                ],
            },
        },
    ],
    ids=["success-response", "failure-response"],
)
def test_execute_connector_step_blocks_reflected_connection_secrets(
    extension_invocation,
):
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    bl = MagicMock()
    bl.get_project_connection.return_value = {
        "connection_id": "conn_provider_dev",
        "account_id": "acc_alpha",
        "project_id": "project_alpha",
        "connection_type": "provider",
        "enabled": True,
        "extension_id": "acme.provider",
        "extension_install_id": "pi_provider_1",
        "secret_refs": {"refs": {"api_token": "project_connection:conn_provider_dev:api_token"}},
    }
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(
                {
                    "runtime": {"contract_version": "extension_host_v2"},
                    "extension_key": "acme.provider",
                    "action_definitions": [{"action_id": "send"}],
                },
                bl,
            ),
        ),
        patch(
            "core.application.management.project_connection_secrets.resolve_connection_secrets",
            return_value={"api_token": "reflected-secret-sentinel"},
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            return_value=extension_invocation,
        ),
        patch(
            "core.application.extensions.response_envelope.process_project_state_updates"
        ) as process_state_updates,
        patch(
            "core.application.orchestration.connector_extension_runtime._persist_connection_session_update"
        ) as persist_session_update,
        patch(
            "core.infrastructure.extension_runtime.extension_invocation_audit.log_host_extension_invocation"
        ) as log_invocation,
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "acme.provider.send",
                "connector_install_id": "pi_provider_1",
                "connection_binding_jsonb": {"connection_id": "conn_provider_dev"},
            },
            state={
                "job_id": 13,
                "workflow_id": "wf.notify",
                "account_id": "acc_alpha",
                "project_id": "project_alpha",
            },
        )

    assert result["ok"] is False
    assert result["detail"] == "extension_secret_exposure_blocked"
    assert result["invocation"] == {
        "status": "failed",
        "detail": "extension_secret_exposure_blocked",
        "response": {
            "ok": False,
            "error": (
                "The extension returned protected credential material. "
                "Contact the extension author."
            ),
            "error_code": "extension_secret_exposure_blocked",
        },
        "errors": [{"code": "extension_secret_exposure_blocked"}],
    }
    assert "reflected-secret-sentinel" not in repr(result)
    process_state_updates.assert_not_called()
    persist_session_update.assert_not_called()
    logged_invocation = log_invocation.call_args.kwargs["invocation"]
    assert logged_invocation == result["invocation"]
    assert "reflected-secret-sentinel" not in repr(log_invocation.call_args.kwargs)


@pytest.mark.parametrize(
    "extension_invocation",
    [
        {
            "status": "succeeded",
            "response": {
                "ok": True,
                "result": {"provider_debug": "reflected-workflow-secret-sentinel"},
            },
        },
        {
            "status": "failed",
            "detail": "provider_request_failed",
            "errors": [
                {
                    "code": "provider_request_failed",
                    "message": "credential=reflected-workflow-secret-sentinel",
                }
            ],
        },
    ],
    ids=["success-response", "failure-response"],
)
def test_execute_connector_step_blocks_reflected_workflow_secret_inputs(
    extension_invocation,
):
    from core.application.orchestration import connector_step_runtime as runtime
    from core.application.registry.workflow_secret_defaults import secret_ref_fingerprint
    from core.infrastructure.security.secret_reference import SecretReference, is_secret_reference

    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    secret_ref = "wf:notify:credential"
    secret_value = "reflected-workflow-secret-sentinel"
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(
                {
                    "runtime": {"contract_version": "extension_host_v2"},
                    "extension_key": "acme.provider",
                    "action_definitions": [{"action_id": "send"}],
                },
                None,
            ),
        ),
        patch.object(runtime, "_read_secret_ciphertext", return_value="encrypted-value"),
        patch.object(runtime, "_decrypt_secret_value", return_value=secret_value),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            return_value=extension_invocation,
        ) as run_action,
        patch(
            "core.application.extensions.response_envelope.process_project_state_updates"
        ) as process_state_updates,
        patch(
            "core.application.orchestration.connector_extension_runtime._persist_connection_session_update"
        ) as persist_session_update,
        patch(
            "core.infrastructure.extension_runtime.extension_invocation_audit.log_host_extension_invocation"
        ) as log_invocation,
    ):
        result = runtime.execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "acme.provider.send",
                "connector_install_id": "pi_provider_1",
                "input_mapping_jsonb": {
                    "credential": SecretReference(name="credential", ref_key=secret_ref).to_wire(),
                    "ordinary_value": "not-a-secret",
                },
            },
            state={
                "job_id": 13,
                "workflow_id": "wf.notify",
                "account_id": "acc_alpha",
                "project_id": "project_alpha",
                "runtime_context": {"account_id": "acc_alpha"},
                "_workflow_secret_ref_fingerprints": [secret_ref_fingerprint(secret_ref)],
            },
        )

    assert run_action.call_args.kwargs["input_payload"] == {
        "credential": secret_value,
        "ordinary_value": "not-a-secret",
    }
    assert result["ok"] is False
    assert result["detail"] == "extension_secret_exposure_blocked"
    assert secret_value not in repr(result)
    assert is_secret_reference(result["input_payload"]["credential"])
    process_state_updates.assert_not_called()
    persist_session_update.assert_not_called()
    assert log_invocation.call_args.kwargs["invocation"] == result["invocation"]
    assert secret_value not in repr(log_invocation.call_args.kwargs)


def test_execute_connector_step_invokes_a_real_extension_process_with_connection_ref_only(
    tmp_path,
):
    """The connector contract, against a bundle built here rather than a shipped one.

    What is under test is the platform's half: the step passes only the declared
    input, the connection's secrets reach the runtime through `target` and not
    through the payload, and the invocation log carries neither. This used to run
    the IMAP bundle and assert one of its settings fields, which pinned that
    extension's behaviour inside the platform suite.
    """
    from core.application.orchestration.connector_step_runtime import execute_connector_step
    from core.infrastructure.extension_runtime.extension_runtime import (
        run_extension_action as real_run_extension_action,
    )

    bundle_root = tmp_path / "acme_record_source"
    bundle_root.mkdir(parents=True)
    # Echoes what the host handed it, so the assertions below read the real
    # subprocess boundary rather than a stub.
    (bundle_root / "main.py").write_text(
        "import json, sys\n"
        "payload = json.loads(sys.stdin.read() or '{}')\n"
        "print(json.dumps({'ok': True, 'result': "
        "{'echoed_input': (payload.get('action') or {}).get('input') or {}}}))\n",
        encoding="utf-8",
    )
    extension_id = "acme.record-source"
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": extension_id,
        "bundle_root": str(bundle_root),
        "entrypoint": {"command": [sys.executable, "main.py"]},
        "action_definitions": [
            {
                "action_id": "discover_settings",
                "handler": {"mode": "extension"},
                "parameters": [
                    {"name": "connection_ref", "type": "connection_ref", "required": True}
                ],
            }
        ],
        "compiled_manifest": {
            "extension_id": extension_id,
            "entrypoint": {"command": [sys.executable, "main.py"]},
            "operation_manifest": [
                {
                    "operation_id": "discover_settings",
                    "kind": "source",
                    "inputs": [{"name": "connection_ref", "type": "connection_ref"}],
                }
            ],
        },
    }
    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    bl = MagicMock()
    bl.get_project_connection.return_value = {
        "connection_id": "conn-primary",
        "account_id": "acc-alpha",
        "project_id": "project-alpha",
        "display_name": "Records source",
        "connection_type": "acme_record_source",
        "credential_revision": 3,
        "enabled": True,
        "extension_id": extension_id,
        "extension_install_id": "install-primary",
        "connection_config": {"acme_host": "records.example.test"},
        "secret_refs": {  # pragma: allowlist secret
            "refs": {"password": "project_connection:conn-primary:password"}
        },
    }
    password = "CONNECTION_PASSWORD_REDACTION_SENTINEL"  # pragma: allowlist secret
    captured_runtime_call = {}

    def run_real_action(*args, **kwargs):
        captured_runtime_call["args"] = args
        captured_runtime_call["kwargs"] = kwargs
        return real_run_extension_action(*args, **kwargs)

    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(extension, bl),
        ),
        patch(
            "core.application.management.project_connection_secrets.resolve_connection_secrets",
            return_value={"password": password},
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            side_effect=run_real_action,
        ),
        patch(
            "core.application.orchestration.connector_extension_runtime.time.time",
            return_value=100.0,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_invocation_audit.log_host_extension_invocation"
        ) as log_invocation,
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": f"{extension_id}.discover_settings",
                "connector_install_id": "install-primary",
                "connection_binding_jsonb": {"connection_id": "conn-primary"},
                "input_mapping_jsonb": {"connection_ref": "literal:conn-primary"},
            },
            state={
                "job_id": "job-records",
                "workflow_id": "wf.records",
                "account_id": "acc-alpha",
                "project_id": "project-alpha",
                "_operation_deadline_epoch_seconds": 105.0,
            },
        )

    assert result["ok"] is True
    assert result["operation_kind"] == "source"
    assert result["request_semantics"] == "safe_read"
    # The bundle really ran as a subprocess and saw exactly the declared input.
    assert result["invocation"]["response"]["result"]["echoed_input"] == {
        "connection_ref": "conn-primary"
    }
    runtime_kwargs = captured_runtime_call["kwargs"]
    assert runtime_kwargs["input_payload"] == {"connection_ref": "conn-primary"}
    assert runtime_kwargs["timeout_seconds"] == 5.0
    target = runtime_kwargs["target"]
    assert target["connection"]["secrets"] == {"password": password}
    assert "password" not in runtime_kwargs["input_payload"]
    logged_request = log_invocation.call_args.kwargs["request"]
    assert logged_request["input_payload"] == {"connection_ref": "conn-primary"}
    assert logged_request["target"]["connection"]["secrets"] == {"password": "<redacted>"}
    assert password not in repr(logged_request)


@pytest.mark.parametrize(
    ("runtime_metadata", "expected_ok"),
    [("valid", True), ("missing", False), ("wrong_project", False)],
)
def test_workflow_test_run_suppresses_external_mutation_without_secrets(
    runtime_metadata: str,
    expected_ok: bool,
) -> None:
    from core.application.orchestration import connector_step_runtime as runtime
    from core.application.registry.workflow_secret_defaults import secret_ref_fingerprint
    from core.infrastructure.security.secret_reference import SecretReference

    extension_id = "example.host-fixture"
    operation_id = "publish_record"
    external_effects = [
        {
            "operation_id": operation_id,
            "effect_kind": "remote_write",
            "channel": "api",
            "idempotency": "required",
            "test_mode_behavior": "suppress",
        }
    ]
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": extension_id,
        "action_definitions": [{"action_id": operation_id, "handler": {"mode": "extension"}}],
        "compiled_manifest": {
            "external_effects": external_effects,
            "operation_manifest": [
                {
                    "operation_id": operation_id,
                    "operation_kind": "action",
                    "outputs": [
                        {"name": "external_effect_status", "value_type": "text"},
                        {"name": "definitely_no_external_effect", "value_type": "boolean"},
                    ],
                }
            ],
        },
    }
    metadata = {
        "connection_id": "conn-primary",
        "project_id": "project-alpha",
        "account_id": "account-alpha",
        "extension_id": extension_id,
        "extension_install_id": "install-primary",
        "connection_type": "remote_api",
        "enabled": True,
        "runtime_status": "ready",
    }
    if runtime_metadata == "missing":
        metadata = {}
    elif runtime_metadata == "wrong_project":
        metadata["project_id"] = "project-other"
    bl = MagicMock()
    bl.get_project_connection_runtime_metadata.return_value = metadata
    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    workflow_secret_ref = "wf:publish:title"  # pragma: allowlist secret - reference only

    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(extension, bl),
        ),
        patch(
            "core.application.management.project_connection_secrets.resolve_connection_secrets",
            side_effect=AssertionError("CONNECTION_SECRET_HYDRATION_BOUNDARY_REACHED"),
        ) as resolve_connection_secrets,
        patch.object(
            runtime,
            "_read_secret_ciphertext",
            return_value="WORKFLOW_SECRET_HYDRATION_BOUNDARY_REACHED",
        ) as read_workflow_secret,
        patch.object(runtime, "_decrypt_secret_value", return_value="decrypted-title"),
        patch(
            "core.infrastructure.extension_runtime.extension_invocation_audit.log_host_extension_invocation"
        ),
    ):
        result = runtime.execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": f"{extension_id}.{operation_id}",
                "connector_install_id": "install-primary",
                "connection_binding_jsonb": {"connection_id": "conn-primary"},
                "input_mapping_jsonb": {
                    "connection_ref": "literal:conn-primary",
                    "status": "draft",
                    "title": SecretReference(
                        name="title",
                        ref_key=workflow_secret_ref,
                    ).to_wire(),
                },
            },
            state=_run_state(
                [{"step_id": "create-draft"}],
                account_id="account-alpha",
                project_id="project-alpha",
                workspace_id="workspace-alpha",
                workflow_id="wf-publish-record",
                job_id="job-test-run",
                test_mode=True,
                trusted_runtime_context={
                    "account_id": "account-alpha",
                    "project_id": "project-alpha",
                    "workspace_id": "workspace-alpha",
                    "workflow_id": "wf-publish-record",
                    "job_id": "job-test-run",
                    "test_mode": True,
                },
                runtime_context={"account_id": "account-alpha"},
                _workflow_secret_ref_fingerprints=[secret_ref_fingerprint(workflow_secret_ref)],
            ),
        )

    assert result["ok"] is expected_ok, result
    read_workflow_secret.assert_not_called()
    resolve_connection_secrets.assert_not_called()
    assert "WORKFLOW_SECRET_HYDRATION_BOUNDARY_REACHED" not in repr(result)
    assert "CONNECTION_SECRET_HYDRATION_BOUNDARY_REACHED" not in repr(result)
    if expected_ok:
        response = result["invocation"]["response"]
        assert response["ok"] is True
        assert response["external_effect_status"] == "suppressed"
        assert response["definitely_no_external_effect"] is True
        assert response["result"] == {
            "test_mode_status": "suppressed",
            "external_effect_status": "suppressed",
            "definitely_no_external_effect": True,
        }
    else:
        assert result["errors"] == [
            {
                "code": "connection_unavailable",
                "message": "The selected extension connection is unavailable.",
            }
        ]


class _ClaimReached(Exception):
    pass


@pytest.mark.parametrize("test_mode", [True, False])
def test_step_without_install_id_adopts_the_install_resolved_for_its_extension(
    test_mode: bool,
) -> None:
    """A step that names its extension but no connector_install_id (an imported or
    API-written definition) runs against the account's one install of that extension.

    Before, a test run failed the suppressed step with connection_unavailable and a
    real run failed the claim with external_effect_install_identity_required.
    """
    from core.application.orchestration import connector_extension_runtime
    from core.application.orchestration import connector_step_runtime as runtime

    extension_id = "example.host-fixture"
    operation_id = "publish_record"
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": extension_id,
        "extension_install_id": "install-primary",
        "install_id": "install-primary",
        "action_definitions": [{"action_id": operation_id, "handler": {"mode": "extension"}}],
        "compiled_manifest": {
            "external_effects": [
                {
                    "operation_id": operation_id,
                    "effect_kind": "remote_write",
                    "channel": "api",
                    "idempotency": "required",
                    "test_mode_behavior": "suppress",
                }
            ],
            "operation_manifest": [{"operation_id": operation_id, "operation_kind": "action"}],
        },
    }
    bl = MagicMock()
    bl.get_project_connection_runtime_metadata.return_value = {
        "connection_id": "conn-primary",
        "project_id": "project-alpha",
        "account_id": "account-alpha",
        "extension_id": extension_id,
        "extension_install_id": "install-primary",
        "connection_type": "remote_api",
        "enabled": True,
        "runtime_status": "ready",
    }
    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    runtime_ids = {
        "account_id": "account-alpha",
        "project_id": "project-alpha",
        "workspace_id": "workspace-alpha",
        "workflow_id": "wf-publish-record",
        "job_id": "job-run",
        "test_mode": test_mode,
    }

    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(extension, bl),
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_invocation_audit.log_host_extension_invocation"
        ),
        patch.object(
            connector_extension_runtime,
            "_build_runtime_connection_target",
            return_value=({"connection_id": "conn-primary"}, {"connection_id": "conn-primary"}),
        ) as build_connection,
        patch.object(
            connector_extension_runtime,
            "_load_extension_project_state_for_target",
            return_value={},
        ),
        patch.object(
            connector_extension_runtime,
            "_claim_external_effect_for_action",
            side_effect=_ClaimReached,
        ) as claim,
    ):
        step = {
            "step_kind": "connector",
            "connector_id": f"{extension_id}.{operation_id}",
            "connection_binding_jsonb": {"connection_id": "conn-primary"},
            "input_mapping_jsonb": {"status": "literal:draft"},
        }
        state = _run_state(
            [{"step_id": "publish"}],
            **runtime_ids,
            trusted_runtime_context=dict(runtime_ids),
            runtime_context={"account_id": "account-alpha"},
        )
        if test_mode:
            result = runtime.execute_connector_step(step=step, state=state)
        else:
            with pytest.raises(_ClaimReached):
                runtime.execute_connector_step(step=step, state=state)

    if test_mode:
        assert result["ok"] is True, result
        assert result["detail"] == "test_mode_suppressed"
        claim.assert_not_called()
    else:
        assert build_connection.call_args.kwargs["connector_install_id"] == "install-primary"
        assert claim.call_args.kwargs["extension_install_id"] == "install-primary"


def test_execute_connector_step_fails_closed_when_oauth_secret_resolution_fails():
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    bl = MagicMock()
    bl.get_project_connection.return_value = {
        "connection_id": "conn_oauth_dev",
        "account_id": "acc_alpha",
        "project_id": "project_alpha",
        "credential_revision": 2,
        "enabled": True,
        "extension_id": "provider",
        "extension_install_id": "pi_provider_1",
        "secret_refs": {"refs": {"access_token": "project_connection:conn_oauth_dev:access_token"}},
    }
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(
                {
                    "runtime": {"contract_version": "extension_host_v2"},
                    "extension_key": "provider",
                    "action_definitions": [{"action_id": "send"}],
                },
                bl,
            ),
        ),
        patch(
            "core.application.management.project_connection_secrets.resolve_connection_secrets",
            return_value={"access_token": "expired-token"},
        ),
        patch(
            "core.application.auth.oauth_client.resolve_oauth_secrets_for_runtime",
            side_effect=RuntimeError("refresh failed"),
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action"
        ) as run_action,
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "provider.send",
                "connector_install_id": "pi_provider_1",
                "connection_binding_jsonb": {"connection_id": "conn_oauth_dev"},
            },
            state={
                "account_id": "acc_alpha",
                "project_id": "project_alpha",
            },
        )

    assert result["handled"] is True
    assert result["ok"] is False
    assert result["runtime"] == "extension_action"
    assert (
        result["detail"]
        == "connection_resolution_failed:connection_oauth_resolution_failed:conn_oauth_dev:refresh failed"
    )
    run_action.assert_not_called()


def test_resolved_disabled_install_fails_before_connection_grants_or_subprocess():
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))

    class _DisabledInstallLogger:
        def __init__(self) -> None:
            self.runtime_gate_calls: list[tuple[str, str]] = []

        def get_extension_by_install_id_for_runtime_gate(
            self,
            install_id: str,
            *,
            account_id: str,
        ) -> dict:
            self.runtime_gate_calls.append((install_id, account_id))
            return {
                "runtime": {"contract_version": "extension_host_v2"},
                "extension_id": "fixture.extension-oauth",
                "extension_key": "fixture.extension-oauth",
                "extension_install_id": "pi_fixture_disabled",
                "install_id": "pi_fixture_disabled",
                "enabled": False,
                "action_definitions": [{"action_id": "trash_messages"}],
            }

    bl = _DisabledInstallLogger()
    step = {
        "step_kind": "connector",
        "connector_id": "fixture.extension-oauth.trash_messages",
        "connector_install_id": "pi_fixture_disabled",
        "connection_binding_jsonb": {"connection_id": "fixture-finance"},
        "input_mapping_jsonb": {"message_ids": 'literal:["message-1"]'},
    }
    original_step = deepcopy(step)

    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.application.orchestration.connector_extension_runtime._shared_logger",
            return_value=bl,
        ),
        patch(
            "core.application.management.project_connection_secrets.resolve_connection_secrets"
        ) as resolve_connection_secrets,
        patch(
            "core.application.auth.oauth_client.resolve_oauth_secrets_for_runtime"
        ) as resolve_oauth,
        patch(
            "core.application.management.project_connection_sessions.get_connection_session"
        ) as get_session,
        patch(
            "core.application.orchestration.connector_extension_runtime._prepare_grants_for_extension_action"
        ) as prepare_grants,
        patch(
            "core.application.orchestration.connector_extension_runtime._claim_external_effect_for_action"
        ) as claim_effect,
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action"
        ) as run_action,
    ):
        result = execute_connector_step(
            step=step,
            state={"account_id": "acc_alpha", "project_id": "project_alpha"},
        )

    assert result["handled"] is True
    assert result["ok"] is False
    assert result["detail"] == "extension_disabled"
    assert result["errors"] == [
        {
            "code": "extension_disabled",
            "message": "This extension is disabled—ask an admin to enable it.",
        }
    ]
    assert bl.runtime_gate_calls == [("pi_fixture_disabled", "acc_alpha")]
    assert step == original_step
    resolve_connection_secrets.assert_not_called()
    resolve_oauth.assert_not_called()
    get_session.assert_not_called()
    prepare_grants.assert_not_called()
    claim_effect.assert_not_called()
    run_action.assert_not_called()


def test_two_oauth_workflows_receive_only_their_selected_connection_token():
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    bl = MagicMock()
    connections = {
        "fixture-finance": {
            "connection_id": "fixture-finance",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "credential_revision": 2,
            "enabled": True,
            "extension_id": "fixture.extension-oauth",
            "extension_install_id": "install-fixture",
            "connection_type": "fixture_oauth_account",
            "connection_config": {
                "oauth": {
                    "lifecycle_status": "connected",
                    "connected_email": "finance@example.test",
                }
            },
            "runtime_status": "ready",
            "secret_refs": {"refs": {"access_token": "finance-ref"}},
        },
        "fixture-support": {
            "connection_id": "fixture-support",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "credential_revision": 2,
            "enabled": True,
            "extension_id": "fixture.extension-oauth",
            "extension_install_id": "install-fixture",
            "connection_type": "fixture_oauth_account",
            "connection_config": {
                "oauth": {
                    "lifecycle_status": "connected",
                    "connected_email": "support@example.test",
                }
            },
            "runtime_status": "ready",
            "secret_refs": {"refs": {"access_token": "support-ref"}},
        },
    }

    def get_connection(**kwargs):
        return connections.get(kwargs["connection_id"])

    bl.get_project_connection.side_effect = get_connection
    captured_targets: list[dict] = []
    captured_inputs: list[dict] = []

    def run_action(*_args, **kwargs):
        captured_targets.append(deepcopy(kwargs["target"]))
        captured_inputs.append(deepcopy(kwargs["input_payload"]))
        return {"status": "succeeded", "response": {"ok": True, "result": {}}}

    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "fixture.extension-oauth",
        "action_definitions": [{"action_id": "search_messages"}],
        "operation_manifest": [
            {
                "operation_id": "search_messages",
                "connection_type_ids": ["fixture_oauth_account"],
            }
        ],
    }
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(extension, bl),
        ),
        patch(
            "core.application.management.project_connection_secrets.resolve_connection_secrets",
            return_value={},
        ),
        patch(
            "core.application.auth.oauth_client.resolve_oauth_secrets_for_runtime",
            side_effect=lambda _bl, *, connection: {
                "access_token": f"token-for-{connection['connection_id']}"
            },
        ),
        patch(
            "core.application.management.project_connection_sessions.get_connection_session",
            return_value=None,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            side_effect=run_action,
        ),
    ):
        for connection_id in connections:
            result = execute_connector_step(
                step={
                    "step_kind": "connector",
                    "connector_id": "fixture.extension-oauth.search_messages",
                    "connector_install_id": "install-fixture",
                    "connection_binding_jsonb": {"connection_id": connection_id},
                    "input_mapping_jsonb": {"label_id": "literal:INBOX"},
                },
                state={
                    "account_id": "acc_alpha",
                    "project_id": "project_alpha",
                    "workflow_id": f"workflow-{connection_id}",
                },
            )
            assert result["ok"] is True

    assert [target["connection"]["connection_id"] for target in captured_targets] == [
        "fixture-finance",
        "fixture-support",
    ]
    assert [target["connection"]["secrets"] for target in captured_targets] == [
        {"access_token": "token-for-fixture-finance"},
        {"access_token": "token-for-fixture-support"},
    ]
    assert [target["connection"]["connection_id"] for target in captured_targets] == [
        payload["connection_ref"] for payload in captured_inputs
    ]


def test_oauth_runtime_rejects_connection_bound_to_another_install_before_secrets() -> None:
    from core.application.orchestration.connector_connection import (
        ConnectionResolutionError,
        _build_runtime_connection_target,
    )

    bl = MagicMock()
    bl.get_project_connection.return_value = {
        "connection_id": "fixture-finance",
        "account_id": "acc_alpha",
        "project_id": "project_alpha",
        "extension_id": "fixture.extension-oauth",
        "extension_install_id": "install-fixture-other",
        "connection_kind": "extension",
        "enabled": True,
    }
    with (
        patch(
            "core.application.management.project_connection_secrets.resolve_connection_secrets"
        ) as resolve_secrets,
        patch(
            "core.application.auth.oauth_client.resolve_oauth_secrets_for_runtime"
        ) as resolve_oauth,
        pytest.raises(ConnectionResolutionError, match="outside runtime scope"),
    ):
        _build_runtime_connection_target(
            bl,
            connection_binding={"connection_id": "fixture-finance"},
            runtime_context={"account_id": "acc_alpha", "project_id": "project_alpha"},
            extension_id="fixture.extension-oauth",
            connector_install_id="install-fixture-current",
        )

    resolve_secrets.assert_not_called()
    resolve_oauth.assert_not_called()


@pytest.mark.parametrize("connection_type", ["ftp", "sftp"])
def test_runtime_accepts_every_declared_connection_type(connection_type: str) -> None:
    from core.application.orchestration.connector_connection import _build_runtime_connection_target

    bl = MagicMock(spec=[])
    bl.get_project_connection = MagicMock(
        return_value={
            "connection_id": "file-transfer",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "extension_id": "acme.file-transfer",
            "extension_install_id": "install-file-transfer",
            "connection_kind": "extension",
            "connection_type": connection_type,
            "enabled": True,
        }
    )

    target, _logged = _build_runtime_connection_target(
        bl,
        connection_binding={"connection_id": "file-transfer"},
        runtime_context={"account_id": "acc_alpha", "project_id": "project_alpha"},
        extension_id="acme.file-transfer",
        connector_install_id="install-file-transfer",
        required_connection_type_ids=frozenset({"ftp", "sftp"}),
    )

    assert target["connection_type"] == connection_type


def test_runtime_rejects_connection_type_outside_declared_set() -> None:
    from core.application.orchestration.connector_connection import (
        ConnectionResolutionError,
        _build_runtime_connection_target,
    )

    bl = MagicMock(spec=[])
    bl.get_project_connection = MagicMock(
        return_value={
            "connection_id": "file-transfer",
            "account_id": "acc_alpha",
            "project_id": "project_alpha",
            "extension_id": "acme.file-transfer",
            "extension_install_id": "install-file-transfer",
            "connection_kind": "extension",
            "connection_type": "acme_cms_site",
            "enabled": True,
        }
    )

    _raises_input_2857_1 = frozenset({"ftp", "sftp"})
    with pytest.raises(ConnectionResolutionError, match="connection_runtime_requirement_failed"):
        _build_runtime_connection_target(
            bl,
            connection_binding={"connection_id": "file-transfer"},
            runtime_context={"account_id": "acc_alpha", "project_id": "project_alpha"},
            extension_id="acme.file-transfer",
            connector_install_id="install-file-transfer",
            required_connection_type_ids=_raises_input_2857_1,
        )


def test_execute_connector_step_fails_closed_when_explicit_connection_is_missing():
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    bl = MagicMock()
    bl.get_project_connection.return_value = None
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(
                {
                    "runtime": {"contract_version": "extension_host_v2"},
                    "extension_key": "provider",
                    "action_definitions": [{"action_id": "send"}],
                },
                bl,
            ),
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action"
        ) as run_action,
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "provider.send",
                "connector_install_id": "pi_provider_1",
                "connection_binding_jsonb": {"connection_id": "conn_missing"},
            },
            state={
                "account_id": "acc_alpha",
                "project_id": "project_alpha",
            },
        )

    assert result["handled"] is True
    assert result["ok"] is False
    assert result["runtime"] == "extension_action"
    assert result["detail"].startswith("connection_resolution_failed:connection_id 'conn_missing'")
    run_action.assert_not_called()


def test_execute_connector_step_fails_closed_when_connection_secrets_fail():
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    bl = MagicMock()
    bl.get_project_connection.return_value = {
        "connection_id": "conn_provider_dev",
        "account_id": "acc_alpha",
        "project_id": "project_alpha",
        "credential_revision": 2,
        "enabled": True,
        "extension_id": "provider",
        "extension_install_id": "pi_provider_1",
        "secret_refs": {"refs": {"api_token": "project_connection:conn_provider_dev:api_token"}},
    }
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(
                {
                    "runtime": {"contract_version": "extension_host_v2"},
                    "extension_key": "provider",
                    "action_definitions": [{"action_id": "send"}],
                },
                bl,
            ),
        ),
        patch(
            "core.application.management.project_connection_secrets.resolve_connection_secrets",
            side_effect=RuntimeError("decrypt failed"),
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action"
        ) as run_action,
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "provider.send",
                "connector_install_id": "pi_provider_1",
                "connection_binding_jsonb": {"connection_id": "conn_provider_dev"},
            },
            state={
                "account_id": "acc_alpha",
                "project_id": "project_alpha",
            },
        )

    assert result["handled"] is True
    assert result["ok"] is False
    assert result["runtime"] == "extension_action"
    assert (
        result["detail"]
        == "connection_resolution_failed:connection_secret_resolution_failed:conn_provider_dev:decrypt failed"
    )
    run_action.assert_not_called()


def test_execute_connector_step_rejects_extension_action_without_account_scope():
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record"
        ) as resolve_extension,
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action"
        ) as run_action,
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "provider.send",
                "input_mapping_jsonb": {"record_id": "record-a"},
            },
            state={},
        )

    assert result["ok"] is False
    assert result["runtime"] == "extension_action"
    assert "requires account-scoped runtime context" in result["detail"]
    resolve_extension.assert_not_called()
    run_action.assert_not_called()


def test_execute_connector_step_does_not_override_extension_id_with_connector_install_id():
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(
                {
                    "runtime": {"contract_version": "extension_host_v2"},
                    "extension_key": "provider",
                    "action_definitions": [{"action_id": "send"}],
                },
                None,
            ),
        ) as resolve_extension,
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            return_value={"status": "succeeded", "response": {"ok": True}},
        ),
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "provider.send",
                "connector_install_id": "pi_test_install",
                "input_mapping_jsonb": {"record_id": "record-a"},
            },
            state={"account_id": "acc_alpha", "project_id": "project_alpha"},
        )

    assert result["ok"] is True
    assert resolve_extension.call_args.args[0] == "pi_test_install"
    assert resolve_extension.call_args.kwargs["account_id"] == "acc_alpha"


def test_execute_connector_step_uses_connector_install_id_as_resolution_fallback():
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))

    def _resolve(extension_ref, *, get_logger_only, account_id=None):
        assert account_id == "acc_alpha"
        if extension_ref == "pi_test_install":
            return (
                {
                    "runtime": {"contract_version": "extension_host_v2"},
                    "extension_key": "provider",
                    "action_definitions": [{"action_id": "send"}],
                },
                None,
            )
        return None, None

    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            side_effect=_resolve,
        ) as resolve_extension,
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            return_value={"status": "succeeded", "response": {"ok": True}},
        ) as run_action,
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "provider.send",
                "connector_install_id": "pi_test_install",
                "input_mapping_jsonb": {"record_id": "record-a"},
            },
            state={"account_id": "acc_alpha", "project_id": "project_alpha"},
        )

    assert result["ok"] is True
    assert [call.args[0] for call in resolve_extension.call_args_list] == ["pi_test_install"]
    run_action.assert_called_once()


def test_execute_extension_connector_step_rejects_missing_persisted_install_identity():
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record"
        ) as resolve_extension,
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action"
        ) as run_action,
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "extension.provider",
                "override": {"connector": {"operation_id": "send"}},
                "connection_binding_jsonb": {"connection_id": "conn-provider"},
            },
            state={"account_id": "acc_alpha", "project_id": "project_alpha"},
        )

    assert result["ok"] is False
    assert result["errors"] == [
        {
            "code": "extension_install_missing",
            "message": "Select the installed extension action again before running this step.",
        }
    ]
    resolve_extension.assert_not_called()
    run_action.assert_not_called()


def test_execute_connector_step_passes_external_effect_id_to_real_runtime(
    tmp_path,
    monkeypatch,
):
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    script = tmp_path / "main.py"
    script.write_text(
        "import json, sys\n"
        "payload = json.loads(sys.stdin.read() or '{}')\n"
        "runtime_context = payload.get('runtime_context') or {}\n"
        "print(json.dumps({'ok': True, 'external_effect_id': runtime_context.get('external_effect_id')}))\n",
        encoding="utf-8",
    )
    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    bl = MagicMock()
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "example.host-fixture",
        "bundle_root": str(tmp_path),
        "entrypoint": {"command": ["python3", "main.py"]},
        "action_definitions": [{"action_id": "publish_record", "handler": {"mode": "extension"}}],
        "compiled_manifest": {
            "external_effects": [
                {
                    "operation_id": "publish_record",
                    "effect_kind": "message_delivery",
                    "channel": "api",
                    "idempotency": "required",
                    "test_mode_behavior": "suppress",
                }
            ],
            "operation_manifest": [
                {
                    "operation_id": "publish_record",
                    "outputs": [{"name": "receipt", "value_type": "artifact_handle"}],
                }
            ],
        },
    }

    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(extension, bl),
        ),
        patch(
            "core.application.extensions.external_effects.cleanup_stale_external_effects",
            return_value={},
        ),
        patch(
            "core.application.extensions.external_effects.claim_external_effect",
            return_value={
                "effect": {"effect_id": "eff_spawn_1", "status": "claimed"},
                "should_spawn": True,
                "suppressed": False,
                "deduped": False,
            },
        ),
        patch(
            "core.application.extensions.external_effects.mark_external_effect_running",
            return_value={"effect_id": "eff_spawn_1", "status": "running"},
        ),
        patch(
            "core.application.extensions.external_effects.confirm_external_effect_from_invocation",
            return_value={
                "effect_id": "eff_spawn_1",
                "status": "succeeded",
                "__confirmed_replay_invocation": {
                    "status": "succeeded",
                    "response": {"ok": True},
                },
            },
        ) as confirm,
        patch(
            "core.infrastructure.extension_runtime.extension_invocation_audit.log_host_extension_invocation"
        ),
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "example.host-fixture.publish_record",
                "connector_install_id": "pi_effect_1",
                "input_mapping_jsonb": {"record_id": "record-a", "title": "Hello"},
            },
            state=_run_state(
                [{"step_id": "publish-record"}],
                account_id="acc_alpha",
                project_id="project_alpha",
                workflow_id="wf_1",
                job_id="job_1",
                workspace_id="workspace_alpha",
            ),
        )

    assert result["ok"] is True
    assert result["invocation"]["response"] == {"ok": True}
    assert confirm.call_args.kwargs["invocation"]["response"]["external_effect_id"] == "eff_spawn_1"
    confirm.assert_called_once()


def test_workflow_external_effect_uses_stable_keyed_intent_across_payload_change(
    monkeypatch,
):
    from core.application.orchestration.connector_effects import _claim_external_effect_for_action

    monkeypatch.setenv("ENCRYPTION_KEY", "workflow-intent-test-root")
    runtime_context = {
        "account_id": "acc-alpha",
        "project_id": "project-alpha",
        "workspace_id": "workspace-alpha",
        "workflow_id": "workflow-1",
        "job_id": "job-1",
        "group_id": "recipients",
        "step_id": "publish-record",
        "step_execution_id": "0b7a2f7e-2a49-4e0c-8f6d-7f0f6a1d2c3b",
        "attempt": 1,
        "loop": {"index": 2},
    }
    with patch(
        "core.application.extensions.external_effects.claim_external_effect_for_action",
        return_value={"declared": True, "should_spawn": True, "effect": {}},
    ) as claim:
        for record_id in ("record-a", "record-b"):
            _claim_external_effect_for_action(
                object(),
                extension={"extension_key": "example.host-fixture"},
                extension_id="example.host-fixture",
                extension_install_id="install-1",
                connection_id="connection-1",
                action_id="publish_record",
                input_payload={"record_id": record_id},
                runtime_context=runtime_context,
            )
        _claim_external_effect_for_action(
            object(),
            extension={"extension_key": "example.host-fixture"},
            extension_id="example.host-fixture",
            extension_install_id="install-1",
            connection_id="connection-1",
            action_id="publish_record",
            input_payload={"record_id": "record-b"},
            runtime_context={**runtime_context, "job_id": "job-2"},
        )
        # A retry or a rework entry of the same step and loop item claims the same effect.
        _claim_external_effect_for_action(
            object(),
            extension={"extension_key": "example.host-fixture"},
            extension_id="example.host-fixture",
            extension_install_id="install-1",
            connection_id="connection-1",
            action_id="publish_record",
            input_payload={"record_id": "record-a"},
            runtime_context={
                **runtime_context,
                "step_execution_id": "5d4c3b2a-1f0e-4d9c-8b7a-6f5e4d3c2b1a",
                "attempt": 3,
            },
        )
        # Another group scope is another effect.
        _claim_external_effect_for_action(
            object(),
            extension={"extension_key": "example.host-fixture"},
            extension_id="example.host-fixture",
            extension_install_id="install-1",
            connection_id="connection-1",
            action_id="publish_record",
            input_payload={"record_id": "record-a"},
            runtime_context={**runtime_context, "group_id": "other_recipients"},
        )

    overrides = [call.kwargs.get("idempotency_key_override") for call in claim.call_args_list]
    assert overrides[0] == overrides[1]
    assert overrides[2] != overrides[0]
    assert overrides[3] == overrides[0]
    assert overrides[4] != overrides[0]
    assert str(overrides[0]).startswith("hmac-sha256:")
    assert "job-1" not in str(overrides[0])
    assert (
        claim.call_args_list[0].kwargs["request_payload"]
        != claim.call_args_list[1].kwargs["request_payload"]
    )


@pytest.mark.parametrize(
    ("commit_outcome", "expected_cleanup_calls"),
    [("rolled_back", 1), ("unknown", 0)],
)
def test_workflow_replay_persist_failure_cleans_outputs_only_after_proven_rollback(
    commit_outcome: str,
    expected_cleanup_calls: int,
):
    from core.application.extensions.external_effect_replay import (
        ExternalEffectReplayPersistenceError,
    )
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    bl = MagicMock()
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "example.host-fixture",
        "extension_install_id": "install-1",
        "action_definitions": [{"action_id": "publish_record"}],
        "compiled_manifest": {
            "external_effects": [
                {
                    "operation_id": "publish_record",
                    "effect_kind": "message_delivery",
                    "channel": "api",
                    "idempotency": "required",
                }
            ],
            "operation_manifest": [
                {
                    "operation_id": "publish_record",
                    "outputs": [{"name": "receipt", "value_type": "artifact_handle"}],
                }
            ],
        },
    }
    prepared = SimpleNamespace(
        artifact_output_ids=["artifact-1"],
        dataset_output_ids=[],
        artifact_inputs=[],
        artifact_outputs=[],
        dataset_inputs=[],
        dataset_outputs=[],
    )
    finalized = SimpleNamespace(
        errors=[],
        produced_artifacts=[{"artifact_id": "artifact-1"}],
        produced_datasets=[],
    )
    error = ExternalEffectReplayPersistenceError(commit_outcome=commit_outcome)
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(extension, bl),
        ),
        patch(
            "core.application.orchestration.connector_extension_runtime._prepare_grants_for_extension_action",
            return_value=(object(), prepared),
        ),
        patch(
            "core.application.extensions.external_effects.claim_external_effect",
            return_value={
                "effect": {
                    "effect_id": "effect-1",
                    "claim_token": "claim-1",
                    "status": "claimed",
                },
                "should_spawn": True,
            },
        ),
        patch(
            "core.application.extensions.external_effects.mark_external_effect_running",
            return_value={"effect_id": "effect-1", "status": "running"},
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            return_value={"status": "succeeded", "response": {"ok": True}},
        ),
        patch(
            "core.application.extensions.runtime_grants.finalize_prepared_extension_grants",
            return_value=finalized,
        ),
        patch(
            "core.application.extensions.external_effects.confirm_external_effect_from_invocation",
            side_effect=error,
        ),
        patch(
            "core.application.orchestration.connector_extension_runtime._cleanup_prepared_output_grants"
        ) as cleanup,
        patch(
            "core.infrastructure.extension_runtime.extension_invocation_audit.log_host_extension_invocation"
        ),
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "example.host-fixture.publish_record",
                "connector_install_id": "install-1",
                "input_mapping_jsonb": {"record_id": "record-a"},
            },
            state=_run_state(
                [{"step_id": "publish-record"}],
                account_id="acc-alpha",
                project_id="project-alpha",
                workspace_id="workspace-1",
                workflow_id="workflow-1",
                job_id="job-1",
                step_id="publish-record",
            ),
        )

    assert cleanup.call_count == expected_cleanup_calls
    assert result["detail"] == "external_effect_replay_persist_failed"


def test_workflow_claim_exception_happens_before_output_grant_materialization() -> None:
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    bl = MagicMock()
    grant_plane = object()
    prepared = SimpleNamespace(
        artifact_output_ids=["artifact-1"],
        dataset_output_ids=[],
        artifact_inputs=[],
        artifact_outputs=[],
        dataset_inputs=[],
        dataset_outputs=[],
    )
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "example.host-fixture",
        "extension_install_id": "install-1",
        "action_definitions": [{"action_id": "publish_record"}],
        "compiled_manifest": {
            "external_effects": [
                {
                    "operation_id": "publish_record",
                    "effect_kind": "message_delivery",
                    "channel": "api",
                    "idempotency": "required",
                }
            ],
            "operation_manifest": [
                {
                    "operation_id": "publish_record",
                    "outputs": [{"name": "receipt", "value_type": "artifact_handle"}],
                }
            ],
        },
    }
    run_state = _run_state(
        [{"step_id": "publish-record"}],
        account_id="acc-alpha",
        project_id="project-alpha",
        workspace_id="workspace-1",
        workflow_id="workflow-1",
        job_id="job-1",
        step_id="publish-record",
    )
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(extension, bl),
        ),
        patch(
            "core.application.orchestration.connector_extension_runtime._prepare_grants_for_extension_action",
            return_value=(grant_plane, prepared),
        ) as prepare,
        patch(
            "core.application.extensions.external_effects.reconcile_external_effect_outputs",
            return_value=0,
        ),
        patch(
            "core.application.orchestration.connector_extension_runtime._claim_external_effect_for_action",
            side_effect=RuntimeError("claim projection failed"),
        ),
        patch(
            "core.application.orchestration.connector_extension_runtime._cleanup_prepared_output_grants"
        ) as cleanup,
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action"
        ) as provider,
        pytest.raises(RuntimeError, match="claim projection failed"),
    ):
        execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "example.host-fixture.publish_record",
                "connector_install_id": "install-1",
                "input_mapping_jsonb": {"record_id": "record-a"},
            },
            state=run_state,
        )

    prepare.assert_not_called()
    cleanup.assert_not_called()
    provider.assert_not_called()


@pytest.mark.parametrize("mark_failure", [None, RuntimeError("lease store unavailable")])
def test_workflow_running_lease_failure_cleans_and_fails_claim_before_spawn(
    mark_failure: Exception | None,
) -> None:
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    bl = MagicMock()
    grant_plane = object()
    prepared = SimpleNamespace(
        artifact_output_ids=["artifact-1"],
        dataset_output_ids=[],
        artifact_inputs=[],
        artifact_outputs=[],
        dataset_inputs=[],
        dataset_outputs=[],
    )
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "example.host-fixture",
        "extension_install_id": "install-1",
        "action_definitions": [{"action_id": "publish_record"}],
        "compiled_manifest": {
            "external_effects": [
                {
                    "operation_id": "publish_record",
                    "effect_kind": "message_delivery",
                    "channel": "api",
                    "idempotency": "required",
                }
            ],
            "operation_manifest": [
                {"operation_id": "publish_record", "outputs": [{"name": "receipt"}]}
            ],
        },
    }
    running = (
        MagicMock(side_effect=mark_failure)
        if mark_failure is not None
        else MagicMock(return_value=None)
    )
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(extension, bl),
        ),
        patch(
            "core.application.orchestration.connector_extension_runtime._prepare_grants_for_extension_action",
            return_value=(grant_plane, prepared),
        ),
        patch(
            "core.application.extensions.external_effects.reconcile_external_effect_outputs",
            return_value=0,
        ),
        patch(
            "core.application.orchestration.connector_extension_runtime._claim_external_effect_for_action",
            return_value={
                "declared": True,
                "should_spawn": True,
                "effect": {
                    "effect_id": "effect-1",
                    "claim_token": "claim-1",
                    "status": "claimed",
                },
            },
        ),
        patch(
            "core.application.extensions.external_effects.register_external_effect_output_reconciliation"
        ),
        patch("core.application.extensions.external_effects.mark_external_effect_running", running),
        patch(
            "core.application.extensions.external_effects.fail_external_effect_before_spawn"
        ) as fail_before_spawn,
        patch(
            "core.application.orchestration.connector_extension_runtime._cleanup_prepared_output_grants"
        ) as cleanup,
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action"
        ) as provider,
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "example.host-fixture.publish_record",
                "connector_install_id": "install-1",
            },
            state=_run_state(
                [{"step_id": "publish-record"}],
                account_id="acc-alpha",
                project_id="project-alpha",
                workspace_id="workspace-1",
                workflow_id="workflow-1",
                job_id="job-1",
                step_id="publish-record",
            ),
        )

    assert result["detail"] in {
        "external_effect_lease_reclaimed",
        "external_effect_lease_unavailable",
    }
    cleanup.assert_called_once_with(grant_plane, prepared)
    fail_before_spawn.assert_called_once_with(
        bl,
        effect_id="effect-1",
        claim_token="claim-1",
        code="external_effect_pre_spawn_setup_failed",
    )
    provider.assert_not_called()


@pytest.mark.parametrize(
    ("failure_stage", "expected_detail"),
    [
        ("runner", "external_effect_provider_invocation_exception"),
        ("finalizer", "external_effect_output_finalization_failed"),
        ("session", "external_effect_session_persistence_failed"),
    ],
)
def test_external_effect_connector_post_start_exception_confirms_timeout_and_dedupes(
    failure_stage: str,
    expected_detail: str,
):
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    bl = MagicMock()
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "example.host-fixture",
        "extension_install_id": "install-1",
        "action_definitions": [{"action_id": "publish_record"}],
        "compiled_manifest": {
            "external_effects": [
                {
                    "operation_id": "publish_record",
                    "effect_kind": "message_delivery",
                    "channel": "api",
                    "idempotency": "required",
                }
            ],
            "operation_manifest": [
                {"operation_id": "publish_record", "outputs": [{"name": "receipt"}]}
            ],
        },
    }
    prepared = SimpleNamespace(
        artifact_output_ids=["artifact-1"],
        dataset_output_ids=[],
        artifact_inputs=[],
        artifact_outputs=[],
        dataset_inputs=[],
        dataset_outputs=[],
    )

    def provider_effect(*_args, **_kwargs):
        if failure_stage == "runner":
            raise RuntimeError("provider detail")
        response = {"ok": True}
        if failure_stage == "session":
            response["session_update"] = {"payload": {"access_token": "rotated"}}
        return {"status": "succeeded", "response": response}

    def finalizer_effect(*_args, **_kwargs):
        if failure_stage == "finalizer":
            raise RuntimeError("storage detail")
        return SimpleNamespace(errors=[], produced_artifacts=[], produced_datasets=[])

    def persist_session_effect(*_args, **_kwargs):
        if failure_stage == "session":
            raise RuntimeError("session storage detail")

    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(extension, bl),
        ),
        patch(
            "core.application.orchestration.connector_extension_runtime._prepare_grants_for_extension_action",
            return_value=(object(), prepared),
        ),
        patch(
            "core.application.orchestration.connector_extension_runtime._cleanup_prepared_output_grants"
        ) as cleanup,
        patch(
            "core.application.extensions.external_effects.claim_external_effect",
            side_effect=[
                {
                    "effect": {
                        "effect_id": "effect-timeout",
                        "claim_token": "claim-1",
                        "status": "claimed",
                    },
                    "should_spawn": True,
                },
                {
                    "effect": {"effect_id": "effect-timeout", "status": "timeout_unknown"},
                    "should_spawn": False,
                },
            ],
        ),
        patch(
            "core.application.extensions.external_effects.mark_external_effect_running",
            return_value={"effect_id": "effect-timeout", "status": "running"},
        ),
        patch(
            "core.application.extensions.external_effects.confirm_external_effect_from_invocation",
            return_value={"effect_id": "effect-timeout", "status": "timeout_unknown"},
        ) as confirm,
        patch(
            "core.application.extensions.external_effect_replay.hydrate_external_effect_claim",
            side_effect=lambda _db, claim: {
                **claim,
                "error": "external_effect_timeout_unknown",
            },
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            side_effect=provider_effect,
        ) as provider,
        patch(
            "core.application.extensions.runtime_grants.finalize_prepared_extension_grants",
            side_effect=finalizer_effect,
        ),
        patch(
            "core.application.orchestration.connector_extension_runtime._persist_connection_session_update",
            side_effect=persist_session_effect,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_invocation_audit.log_host_extension_invocation"
        ),
    ):
        step = {
            "step_kind": "connector",
            "connector_id": "example.host-fixture.publish_record",
            "connector_install_id": "install-1",
            "input_mapping_jsonb": {"record_id": "record-a"},
        }
        state = _run_state(
            [{"step_id": "publish-record"}],
            account_id="acc-alpha",
            project_id="project-alpha",
            workflow_id="workflow-1",
            job_id="job-1",
            workspace_id="workspace-1",
        )
        first = execute_connector_step(step=step, state=state)
        duplicate = execute_connector_step(step=step, state=state)

    assert first["ok"] is False
    assert duplicate["ok"] is False
    provider.assert_called_once()
    confirm.assert_called_once()
    assert confirm.call_args.kwargs["claim_token"] == "claim-1"
    assert confirm.call_args.kwargs["invocation"]["detail"] == expected_detail
    assert cleanup.call_count >= 2
    assert "provider detail" not in repr(first)
    assert "storage detail" not in repr(first)
    assert "session storage detail" not in repr(first)


@pytest.mark.parametrize(
    "session_update",
    [
        {"expires_at": 1234.0},
        {"payload": {"access_token": "rotated"}, "expires_at": 0},
    ],
)
def test_external_effect_session_update_malformed_fails_closed(session_update: dict):
    from core.application.orchestration.connector_connection import (
        _persist_connection_session_update,
    )

    _raises_input_3801_1 = MagicMock()
    with pytest.raises(RuntimeError):
        _persist_connection_session_update(
            _raises_input_3801_1,
            runtime_connection={"connection_id": "connection-1"},
            connection_binding={},
            invocation={
                "status": "succeeded",
                "response": {"ok": True, "session_update": session_update},
            },
            fail_closed=True,
        )


def test_host_external_effect_rate_limit_projects_retry_signal_without_provider_call():
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    bl = MagicMock()
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "example.host-fixture",
        "action_definitions": [{"action_id": "publish_record"}],
        "compiled_manifest": {
            "external_effects": [
                {
                    "operation_id": "publish_record",
                    "effect_kind": "message_delivery",
                    "channel": "api",
                    "idempotency": "required",
                    "test_mode_behavior": "suppress",
                }
            ],
            "operation_manifest": [
                {
                    "operation_id": "publish_record",
                    "operation_kind": "action",
                    "inputs": [{"name": "record_id", "required": True}],
                    "outputs": [],
                }
            ],
        },
    }
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(extension, bl),
        ),
        patch(
            "core.application.orchestration.connector_extension_runtime._claim_external_effect_for_action",
            return_value={
                "declared": True,
                "effect": {
                    "effect_id": "effect-rate-limited",
                    "claim_token": "claim-rate-limited",
                    "status": "failed",
                    "response_summary_jsonb": {
                        "definitely_no_external_effect": True,
                        "rate_limited_scope": "connection",
                        "retry_after_seconds": 13,
                    },
                },
                "should_spawn": False,
                "suppressed": False,
                "deduped": False,
                "rate_limited": True,
                "retry_after_seconds": 13,
                "error": "external_effect_rate_limited",
            },
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action"
        ) as run_action,
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "example.host-fixture.publish_record",
                "connector_install_id": "install-1",
                "input_mapping_jsonb": {"record_id": "literal:record-a"},
            },
            state=_run_state(
                [{"step_id": "publish-record"}],
                account_id="acc-alpha",
                project_id="project-alpha",
                workflow_id="workflow-1",
                job_id="job-1",
                workspace_id="workspace-1",
            ),
        )

    assert result["ok"] is False
    assert result["operation_kind"] == "action"
    assert result["request_semantics"] == "mutation"
    assert result["failure_class"] == "transient"
    assert result["retryable"] is True
    assert result["retry_after_seconds"] == 13.0
    assert result["definitely_no_external_effect"] is True
    assert result["external_effect_status"] == "failed"
    assert result["provider_error_code"] == "rate_limited"
    run_action.assert_not_called()


def test_execute_connector_step_dedupes_completed_external_effect(tmp_path, monkeypatch):
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    monkeypatch.setenv("FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS", "1")
    storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifact_store"))
    plane = ArtifactDatasetPlane(storage=storage)
    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    bl = MagicMock()
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "example.host-fixture",
        "action_definitions": [{"action_id": "publish_record"}],
        "compiled_manifest": {
            "artifact_policies": [
                {
                    "operation_id": "publish_record",
                    "outputs": [
                        {
                            "kind": "artifact",
                            "binding_key": "receipt",
                            "filename": "receipt.json",
                            "content_type": "application/json",
                        },
                        {
                            "kind": "dataset",
                            "binding_key": "audit_rows",
                            "schema": {"columns": [{"name": "message_id", "type": "text"}]},
                        },
                    ],
                }
            ],
            "external_effects": [
                {
                    "operation_id": "publish_record",
                    "effect_kind": "message_delivery",
                    "channel": "api",
                    "idempotency": "required",
                    "test_mode_behavior": "suppress",
                }
            ],
            "operation_manifest": [
                {"operation_id": "publish_record", "outputs": [{"name": "receipt"}]}
            ],
        },
    }
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(extension, bl),
        ),
        patch(
            "core.application.extensions.external_effects.cleanup_stale_external_effects",
            return_value={},
        ),
        patch(
            "core.application.extensions.external_effects.claim_external_effect",
            return_value={
                "effect": {
                    "effect_id": "eff_1",
                    "status": "succeeded",
                    "response_summary_jsonb": {
                        "produced_artifacts": [
                            {
                                "artifact_id": "art_previous",
                                "artifact_handle": "artifact:art_previous",
                                "binding_key": "receipt",
                            }
                        ],
                        "produced_datasets": [
                            {
                                "dataset_id": "dst_previous",
                                "dataset_handle": "dataset:dst_previous",
                                "binding_key": "audit_rows",
                            }
                        ],
                        "grant_outputs": {
                            "receipt": "artifact:art_previous",
                            "audit_rows": "dataset:dst_previous",
                        },
                    },
                },
                "should_spawn": False,
                "suppressed": False,
                "deduped": True,
            },
        ) as claim,
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action"
        ) as run_action,
        patch(
            "core.application.extensions.external_effect_replay.hydrate_external_effect_claim",
            side_effect=lambda _db, claim: {
                **claim,
                "replay_invocation": {
                    "status": "succeeded",
                    "response": {"result": {"message_id": "message-previous"}},
                    "produced_artifacts": [
                        {
                            "artifact_id": "art_previous",
                            "artifact_handle": "artifact:art_previous",
                            "binding_key": "receipt",
                        }
                    ],
                    "produced_datasets": [
                        {
                            "dataset_id": "dst_previous",
                            "dataset_handle": "dataset:dst_previous",
                            "binding_key": "audit_rows",
                        }
                    ],
                    "grant_outputs": {
                        "receipt": "artifact:art_previous",
                        "audit_rows": "dataset:dst_previous",
                    },
                },
            },
        ),
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "example.host-fixture.publish_record",
                "connector_install_id": "pi_effect_1",
                "input_mapping_jsonb": {"record_id": "record-a", "title": "Hello"},
            },
            state=_run_state(
                [{"step_id": "publish-record", "for_each_id": "customers"}],
                account_id="acc_alpha",
                project_id="project_alpha",
                workflow_id="wf_1",
                job_id="job_1",
                workspace_id="workspace_alpha",
                _artifact_plane=plane,
                frame=ForEachFrame(
                    for_each_id="customers",
                    for_each_execution_id="fe-1",
                    selected_count=3,
                    item_index=2,
                ),
                for_each=[
                    {"for_each_id": "customers", "over": "state.c", "item_name": "c", "body": {}}
                ],
            ),
        )

    assert result["handled"] is True
    assert result["ok"] is True
    assert result["detail"] == "external_effect_succeeded"
    assert result["external_effect"]["effect_id"] == "eff_1"
    assert result["grant_outputs"]["receipt"] == "artifact:art_previous"
    assert result["grant_outputs"]["audit_rows"] == "dataset:dst_previous"
    assert result["artifact_handle"] == result["grant_outputs"]["receipt"]
    assert result["dataset_handle"] == result["grant_outputs"]["audit_rows"]
    assert plane._artifacts == {}
    assert plane._datasets == {}
    claim.assert_called_once()
    assert claim.call_args.kwargs["source_ref"]["loop"] == {
        "for_each_id": "customers",
        "index": 2,
    }
    run_action.assert_not_called()


def test_execute_connector_step_fails_closed_when_replay_ciphertext_is_missing(
    tmp_path,
    monkeypatch,
):
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    monkeypatch.setenv("FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS", "1")
    storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifact_store"))
    plane = ArtifactDatasetPlane(storage=storage)
    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    bl = MagicMock()
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "example.host-fixture",
        "action_definitions": [{"action_id": "publish_record"}],
        "compiled_manifest": {
            "artifact_policies": [
                {
                    "operation_id": "publish_record",
                    "outputs": [
                        {
                            "kind": "artifact",
                            "binding_key": "receipt",
                            "filename": "receipt.json",
                            "content_type": "application/json",
                        }
                    ],
                }
            ],
            "external_effects": [
                {
                    "operation_id": "publish_record",
                    "effect_kind": "message_delivery",
                    "channel": "api",
                    "idempotency": "required",
                    "test_mode_behavior": "suppress",
                }
            ],
            "operation_manifest": [
                {"operation_id": "publish_record", "outputs": [{"name": "receipt"}]}
            ],
        },
    }
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(extension, bl),
        ),
        patch(
            "core.application.extensions.external_effects.cleanup_stale_external_effects",
            return_value={},
        ),
        patch(
            "core.application.extensions.external_effects.claim_external_effect",
            return_value={
                "effect": {"effect_id": "eff_old", "status": "succeeded"},
                "should_spawn": False,
                "suppressed": False,
                "deduped": True,
            },
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action"
        ) as run_action,
        patch(
            "core.application.extensions.external_effect_replay.hydrate_external_effect_claim",
            side_effect=lambda _db, claim: {
                **claim,
                "error": "external_effect_replay_missing",
                "replay_invocation": {},
            },
        ),
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "example.host-fixture.publish_record",
                "connector_install_id": "pi_effect_1",
                "input_mapping_jsonb": {"record_id": "record-a", "title": "Hello"},
            },
            state=_run_state(
                [{"step_id": "publish-record"}],
                account_id="acc_alpha",
                project_id="project_alpha",
                workflow_id="wf_1",
                job_id="job_1",
                workspace_id="workspace_alpha",
                _artifact_plane=plane,
            ),
        )

    assert result["ok"] is False
    assert result["detail"] == "external_effect_replay_missing"
    assert result["grant_outputs"] == {}
    assert plane._artifacts == {}
    run_action.assert_not_called()


def test_execute_connector_step_suppressed_effect_preserves_by_reference_output_grant(
    tmp_path,
    monkeypatch,
):
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    monkeypatch.setenv("FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS", "1")
    storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifact_store"))
    plane = ArtifactDatasetPlane(storage=storage)
    receipt_handle, _receipt_meta = plane.create_artifact(
        owner_scope={"account_id": "acc_alpha", "project_id": "project_alpha"},
        run_id="run_1",
        step_id="caller",
        filename="receipt.json",
        content_type="application/json",
        retention_class="persistent",
        body=None,
    )
    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    bl = MagicMock()
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "example.host-fixture",
        "action_definitions": [{"action_id": "publish_record"}],
        "compiled_manifest": {
            "artifact_policies": [
                {
                    "operation_id": "publish_record",
                    "outputs": [
                        {
                            "kind": "artifact",
                            "binding_key": "receipt",
                            "field": "receipt_handle",
                        }
                    ],
                }
            ],
            "external_effects": [
                {
                    "operation_id": "publish_record",
                    "effect_kind": "message_delivery",
                    "channel": "api",
                    "idempotency": "required",
                    "test_mode_behavior": "suppress",
                }
            ],
            "operation_manifest": [
                {"operation_id": "publish_record", "outputs": [{"name": "receipt"}]}
            ],
        },
    }
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(extension, bl),
        ),
        patch(
            "core.application.extensions.external_effects.cleanup_stale_external_effects",
            return_value={},
        ),
        patch(
            "core.application.extensions.external_effects.claim_external_effect",
            return_value={
                "effect": {"effect_id": "eff_suppressed", "status": "suppressed"},
                "should_spawn": False,
                "suppressed": True,
                "deduped": False,
            },
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action"
        ) as run_action,
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "example.host-fixture.publish_record",
                "connector_install_id": "pi_effect_1",
                "input_mapping_jsonb": {
                    "record_id": "record-a",
                    "title": "Hello",
                    "receipt_handle": f"artifact:{receipt_handle.artifact_id}",
                },
            },
            state=_run_state(
                [{"step_id": "publish-record"}],
                account_id="acc_alpha",
                project_id="project_alpha",
                workflow_id="wf_1",
                job_id="job_1",
                workspace_id="workspace_alpha",
                _artifact_plane=plane,
            ),
        )

    assert result["ok"] is False
    assert result["detail"] == "external_effect_deduped_outputs_unavailable"
    assert plane.get_artifact(receipt_handle.artifact_id) is not None
    run_action.assert_not_called()


def test_prepare_suppressed_output_only_policy_does_not_require_artifact_plane():
    from core.application.orchestration.connector_grants import _prepare_grants_for_extension_action

    extension = {
        "extension_key": "example.host-fixture",
        "compiled_manifest": {
            "artifact_policies": [
                {
                    "operation_id": "publish_record",
                    "outputs": [
                        {
                            "kind": "artifact",
                            "binding_key": "receipt",
                            "filename": "receipt.json",
                            "content_type": "application/json",
                            "max_size_bytes": 1024,
                        }
                    ],
                }
            ]
        },
    }

    plane, prepared = _prepare_grants_for_extension_action(
        extension=extension,
        action_id="publish_record",
        input_payload={},
        project_state={},
        runtime_context={},
        runtime_scope=SimpleNamespace(),
        state={},
        include_output_grants=False,
    )

    assert plane is None
    assert prepared.artifact_inputs == []
    assert prepared.artifact_outputs == []
    assert prepared.dataset_inputs == []
    assert prepared.dataset_outputs == []


@pytest.mark.parametrize(
    ("group_id", "for_each_id", "expected_group", "expected_for_each"),
    [
        ("", "", None, None),
        ("per_item", "", "per_item", None),
        ("", "each_order", None, "each_order"),
    ],
    ids=["root-step", "group-step", "for-each-step"],
)
def test_step_grants_bind_group_and_execution_scope(
    monkeypatch, group_id, for_each_id, expected_group, expected_for_each
):
    from core.application.extensions import runtime_grants

    captured: dict = {}
    monkeypatch.setattr(
        runtime_grants,
        "prepare_extension_grants",
        lambda **kwargs: captured.update(kwargs) or runtime_grants.PreparedExtensionGrants(),
    )
    monkeypatch.setattr(
        connector_grants, "_shared_artifact_plane_from_state", lambda _state: object()
    )
    extension = {
        "extension_key": "example.host-fixture",
        "compiled_manifest": {
            "artifact_policies": [
                {
                    "operation_id": "publish_record",
                    "outputs": [
                        {
                            "kind": "artifact",
                            "binding_key": "receipt",
                            "filename": "receipt.json",
                            "content_type": "application/json",
                            "max_size_bytes": 1024,
                        }
                    ],
                }
            ]
        },
    }

    connector_grants._prepare_grants_for_extension_action(
        extension=extension,
        action_id="publish_record",
        input_payload={},
        project_state={},
        runtime_context={
            "account_id": "acc",
            "job_id": "job_1",
            "group_id": group_id,
            "for_each_id": for_each_id,
            "step_id": "publish",
            "step_execution_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "phase_id": "legacy",
        },
        runtime_scope=SimpleNamespace(),
        state={},
    )

    audit = captured["audit"]
    assert "group_id" in audit
    assert audit["group_id"] == expected_group
    # Explicit null binds: a root grant never serves a For each and back.
    assert "for_each_id" in audit
    assert audit["for_each_id"] == expected_for_each
    assert audit["step_id"] == "publish"
    assert audit["step_execution_id"] == "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    assert "phase_id" not in audit


def test_collect_output_value_prefers_host_grant_outputs_over_extension_response():
    from core.application.orchestration.connector_mapping import _collect_output_value

    result = {
        "artifact_handle": "artifact:host_default",
        "grant_outputs": {"receipt": "artifact:host_receipt"},
        "response": {
            "result": {
                "artifact_handle": "artifact:extension_default",
                "receipt": "artifact:extension_receipt",
            }
        },
    }

    assert _collect_output_value(result, "artifact_handle") == (
        True,
        "artifact:host_default",
    )
    assert _collect_output_value(result, "receipt") == (True, "artifact:host_receipt")


def test_apply_output_mapping_collects_host_grants_and_nested_extension_result():
    from core.application.orchestration.connector_mapping import _apply_output_mapping

    dispatch_result = {
        "grant_outputs": {
            "markdown_artifact_handle": "artifact:markdown",
            "parsed_json_artifact_handle": "artifact:json",
        },
        "invocation": {
            "response": {
                "result": {
                    "markdown_preview": "# Converted",
                    "provider_status": "success",
                }
            }
        },
    }
    state: dict[str, object] = {}

    _apply_output_mapping(
        step_payload={
            "output_mapping_jsonb": {
                "markdown_artifact_handle": "state.markdown_handle",
                "parsed_json_artifact_handle": "state.json_handle",
                "markdown_preview": "state.preview",
                "provider_status": "state.provider_status",
            }
        },
        dispatch_result=dispatch_result,
        state=state,
    )

    assert dispatch_result["mapped_outputs"] == {
        "markdown_artifact_handle": "artifact:markdown",
        "parsed_json_artifact_handle": "artifact:json",
        "markdown_preview": "# Converted",
        "provider_status": "success",
    }
    assert dispatch_result["state_patch"] == {
        "markdown_handle": "artifact:markdown",
        "json_handle": "artifact:json",
        "preview": "# Converted",
        "provider_status": "success",
        "workflow_data": {
            "markdown_handle": "artifact:markdown",
            "json_handle": "artifact:json",
            "preview": "# Converted",
            "provider_status": "success",
        },
    }


def test_collect_output_value_reaches_deeply_nested_extension_response():
    from core.application.orchestration.connector_mapping import _collect_output_value

    nested: dict[str, object] = {"markdown_preview": "# Converted"}
    for _ in range(30):
        nested = {"response": nested}

    assert _collect_output_value({"invocation": nested}, "markdown_preview") == (
        True,
        "# Converted",
    )


def test_collect_output_value_resolves_dotted_result_paths_and_array_segments():
    from core.application.orchestration.connector_mapping import _collect_output_value

    result = {
        "invocation": {
            "response": {
                "result": {
                    "rows": [{"id": "row-1"}, {"id": "row-2"}],
                }
            }
        }
    }

    assert _collect_output_value(result, "result.rows[]") == (
        True,
        [{"id": "row-1"}, {"id": "row-2"}],
    )
    assert _collect_output_value(result, "result.rows[].id") == (
        True,
        ["row-1", "row-2"],
    )


def test_extension_project_state_loads_project_wide_with_connection_override():
    from core.application.orchestration.connector_connection import (
        _load_extension_project_state_for_target,
    )

    bl = MagicMock()

    def _get_state(*args, **kwargs):
        connection_id = kwargs.get("connection_id")
        if connection_id is None:
            return {
                "namespace": "settings",
                "state_key": "setting:primary",
                "value_jsonb": {"content": "project-default"},
                "revision": 1,
                "connection_id": None,
            }
        assert connection_id == "conn_external"
        return {
            "namespace": "settings",
            "state_key": "setting:primary",
            "value_jsonb": {"value": "connection"},
            "revision": 2,
            "connection_id": "conn_external",
        }

    with patch(
        "core.application.extensions.extension_project_state.get_state", side_effect=_get_state
    ):
        project_state = _load_extension_project_state_for_target(
            bl,
            account_id="acc_alpha",
            project_id="project_alpha",
            extension_id="example.host-fixture",
            extension_install_id="install_alpha",
            connection_id="conn_external",
            extension={
                "project_state": {
                    "reads": [
                        {
                            "namespace": "settings",
                            "state_key_from_input": "setting_id",
                            "state_key_prefix": "setting:",
                        }
                    ]
                }
            },
            input_payload={"setting_id": "primary"},
        )

    items = {
        (item["namespace"], item["state_key"]): item for item in project_state.get("items", [])
    }
    assert items[("settings", "setting:primary")]["value"] == {"value": "connection"}
    assert items[("settings", "setting:primary")]["connection_id"] == "conn_external"
    assert len(items) == 1


def test_extension_project_state_does_not_load_undeclared_namespaces():
    from core.application.orchestration.connector_connection import (
        _load_extension_project_state_for_target,
    )

    list_state = MagicMock()
    with patch("core.application.extensions.extension_project_state.list_state", list_state):
        project_state = _load_extension_project_state_for_target(
            MagicMock(),
            account_id="acc_alpha",
            project_id="project_alpha",
            extension_id="example.host-fixture",
            extension_install_id="install_alpha",
            connection_id="conn_external",
            extension={"project_state": {"reads": []}},
            input_payload={"setting_id": "primary"},
        )

    assert project_state == {}
    list_state.assert_not_called()


def test_execute_connector_step_returns_fail_when_no_runtime_matches():
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    with patch(
        "core.application.registry.registry_manager.RegistryManager.instance", return_value=registry
    ):
        result = execute_connector_step(
            step={"step_kind": "connector", "connector_id": "connector.unknown"},
            state={},
        )

    assert result["ok"] is False
    assert result["runtime"] == "unresolved"


def test_execute_connector_step_prepares_attachment_artifact_grants_from_policy():
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    artifact_meta = {
        "artifact_id": "art_pdf_1",
        "object_key": "artifacts/art_pdf_1/body",
        "owner_scope": {"account_id": "acc_alpha", "project_id": "project_alpha"},
        "size_bytes": 1024,
        "sha256": "a" * 64,
        "filename": "report.pdf",
        "content_type": "application/pdf",
        "retention_class": "run_window",
    }
    plane = SimpleNamespace(
        get_artifact=MagicMock(
            side_effect=lambda artifact_id: (
                dict(artifact_meta) if artifact_id == "art_pdf_1" else None
            )
        ),
        artifact_presigned_url=MagicMock(
            return_value="https://signed.example/artifacts/art_pdf_1/body?method=GET"
        ),
    )
    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    bl = MagicMock()
    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "example.host-fixture",
        "extension_install_id": "install-1",
        "action_definitions": [{"action_id": "publish_record"}],
        "compiled_manifest": {
            "artifact_policies": [
                {
                    "operation_id": "publish_record",
                    "inputs": [
                        {
                            "kind": "artifact",
                            "value_type": "artifact_handle",
                            "binding_key": "attachments",
                            "from_input": "attachments",
                            "required": False,
                        }
                    ],
                }
            ],
            "external_effects": [
                {
                    "operation_id": "publish_record",
                    "effect_kind": "message_delivery",
                    "channel": "api",
                    "idempotency": "required",
                    "test_mode_behavior": "suppress",
                }
            ],
            "operation_manifest": [
                {"operation_id": "publish_record", "outputs": [{"name": "receipt"}]}
            ],
        },
    }
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(extension, bl),
        ),
        patch(
            "core.application.extensions.external_effects.cleanup_stale_external_effects",
            return_value={},
        ),
        patch(
            "core.application.extensions.external_effects.claim_external_effect",
            return_value={
                "effect": {"effect_id": "eff_attach_1", "status": "claimed"},
                "should_spawn": True,
                "suppressed": False,
                "deduped": False,
            },
        ),
        patch(
            "core.application.extensions.external_effects.mark_external_effect_running",
            return_value={"effect_id": "eff_attach_1", "status": "running"},
        ),
        patch(
            "core.application.extensions.external_effects.confirm_external_effect_from_invocation",
            return_value={
                "effect_id": "eff_attach_1",
                "status": "succeeded",
                "__confirmed_replay_invocation": {
                    "status": "succeeded",
                    "response": {"ok": True},
                },
            },
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            return_value={"status": "succeeded", "response": {"ok": True}},
        ) as run_action,
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "example.host-fixture.publish_record",
                "connector_install_id": "install-1",
                "input_mapping_jsonb": {
                    "record_id": "record-a",
                    "title": "Report",
                    "content": "Attached.",
                    "attachments": [
                        {
                            "artifact_id": "art_pdf_1",
                            "filename": "report.pdf",
                            "content_type": "application/pdf",
                        }
                    ],
                },
            },
            state=_run_state(
                [{"step_id": "publish-record"}],
                account_id="acc_alpha",
                project_id="project_alpha",
                workflow_id="wf_1",
                job_id="job_1",
                workspace_id="workspace_alpha",
                _artifact_plane=plane,
            ),
        )

    assert result["ok"] is True
    run_action.assert_called_once()
    assert run_action.call_args.kwargs["timeout_seconds"] == pytest.approx(300.0, abs=1.0)
    artifact_inputs = run_action.call_args.kwargs["artifact_inputs"]
    assert len(artifact_inputs) == 1
    descriptor = artifact_inputs[0]
    assert descriptor["artifact_id"] == "art_pdf_1"
    assert descriptor["artifact_handle"] == "artifact:art_pdf_1"
    assert descriptor["binding_key"] == "attachments"
    assert descriptor["access"]["download_url"].startswith("https://signed.example/")


def test_workflow_test_run_suppresses_external_action_without_materializing_inputs(
    tmp_path,
    monkeypatch,
):
    import io
    import json

    import polars as pl

    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    monkeypatch.setenv("FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS", "1")
    storage = LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifact_store"))
    writer_plane = ArtifactDatasetPlane(storage=storage)
    dataset_handle, original_meta = writer_plane.create_dataset(
        owner_scope={"account_id": "acc_alpha", "project_id": "project_alpha"},
        run_id="tabular_feed",
        step_id="read_downloaded_sample",
        schema={},
        rows=[],
    )
    frame = pl.DataFrame([{"sku": "00123", "qty": "2"}, {"sku": "00002", "qty": "5"}])
    parquet = io.BytesIO()
    frame.write_parquet(parquet)
    storage.delete(str(original_meta["object_key"]))
    object_key = f"datasets/{dataset_handle.dataset_id}/rows.parquet"
    stored = storage.put(
        object_key,
        parquet.getvalue(),
        content_type="application/vnd.apache.parquet",
    )
    parquet_meta = {
        **original_meta,
        "object_key": object_key,
        "content_type": "application/vnd.apache.parquet",
        "encoding": "parquet",
        "format": "parquet",
        "schema": {
            "columns": [
                {"name": name, "type": str(dtype)}
                for name, dtype in zip(frame.columns, frame.dtypes, strict=True)
            ],
            "format": "parquet",
        },
        "row_count": frame.height,
        "size_bytes": stored["size_bytes"],
        "sha256": stored["sha256"],
    }
    storage.put(
        f"datasets/{dataset_handle.dataset_id}/meta.json",
        json.dumps(parquet_meta, separators=(",", ":")).encode("utf-8"),
        content_type="application/json",
    )
    runtime_plane = ArtifactDatasetPlane(storage=storage)
    assert runtime_plane.get_dataset(dataset_handle.dataset_id) == parquet_meta

    extension = {
        "runtime": {"contract_version": "extension_host_v2"},
        "extension_key": "example.host-fixture",
        "extension_install_id": "install-1",
        "action_definitions": [{"action_id": "publish_table"}],
        "compiled_manifest": {
            "artifact_policies": [
                {
                    "operation_id": "publish_table",
                    "inputs": [
                        {
                            "kind": "dataset",
                            "binding_key": "source_rows",
                            "from_input": "dataset_handle",
                            "content_type": "application/x-ndjson",
                            "max_size_bytes": 1024 * 1024,
                        }
                    ],
                    "outputs": [
                        {
                            "kind": "artifact",
                            "binding_key": "receipt",
                            "filename": "receipt.json",
                            "content_type": "application/json",
                            "max_size_bytes": 1024,
                        }
                    ],
                }
            ],
            "external_effects": [
                {
                    "operation_id": "publish_table",
                    "effect_kind": "remote_write",
                    "channel": "api",
                    "idempotency": "required",
                    "test_mode_behavior": "suppress",
                }
            ],
            "operation_manifest": [
                {"operation_id": "publish_table", "outputs": [{"name": "receipt"}]}
            ],
        },
    }
    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    bl = MagicMock()
    bl.get_project_connection_runtime_metadata.return_value = {
        "connection_id": "connection-1",
        "project_id": "project_alpha",
        "account_id": "acc_alpha",
        "extension_id": "example.host-fixture",
        "extension_install_id": "install-1",
        "enabled": True,
        "runtime_status": "ready",
    }
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(extension, bl),
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            return_value={
                "status": "succeeded",
                "response": {
                    "ok": True,
                    "external_effect_status": "suppressed",
                    "definitely_no_external_effect": True,
                },
            },
        ) as run_action,
        patch(
            "core.infrastructure.extension_runtime.extension_invocation_audit.log_host_extension_invocation"
        ),
    ):
        result = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "example.host-fixture.publish_table",
                "connector_install_id": "install-1",
                "connection_binding_jsonb": {"connection_id": "connection-1"},
                "input_mapping_jsonb": {
                    "dataset_handle": f"dataset:{dataset_handle.dataset_id}",
                },
            },
            state=_run_state(
                [{"step_id": "publish-table"}],
                account_id="acc_alpha",
                project_id="project_alpha",
                workspace_id="workspace_alpha",
                workflow_id="wf_table_publish",
                job_id="job_table_publish",
                test_mode=True,
                trusted_runtime_context={
                    "account_id": "acc_alpha",
                    "project_id": "project_alpha",
                    "workspace_id": "workspace_alpha",
                    "workflow_id": "wf_table_publish",
                    "job_id": "job_table_publish",
                    "test_mode": True,
                },
                _artifact_plane=runtime_plane,
            ),
        )

    assert result["ok"] is True, result
    run_action.assert_not_called()
    assert result["detail"] == "test_mode_suppressed"
    assert result["invocation"]["response"] == {
        "ok": True,
        "result": {
            "test_mode_status": "suppressed",
            "external_effect_status": "suppressed",
            "definitely_no_external_effect": True,
        },
        "external_effect_status": "suppressed",
        "definitely_no_external_effect": True,
    }
    assert result["produced_artifacts"] == []
    assert result["produced_datasets"] == []
    assert result["grant_outputs"] == {}


def test_derive_extension_action_preserves_catalog_extension_identity() -> None:
    from core.application.orchestration.connector_effects import _derive_extension_action

    assert _derive_extension_action(
        connector_id="extension.acme.document-converter",
        extension_id="",
        action_id="",
        operation_id="convert_file",
    ) == ("acme.document-converter", "convert_file")


def test_artifact_policy_uses_only_canonical_operator_limit_env(monkeypatch) -> None:
    from core.application.orchestration.connector_grants import _grant_requests_from_policy

    monkeypatch.setenv("FS_EXTENSION_ARTIFACT_MAX_BYTES", str(2 * 1024 * 1024 * 1024))
    requests = _grant_requests_from_policy(
        {
            "operation_id": "copy",
            "inputs": [
                {
                    "kind": "artifact",
                    "from_input": "artifact_handle",
                    "binding_key": "source",
                    "content_type": "application/x-ndjson",
                    "max_size_bytes": 200 * 1024 * 1024,
                    "max_size_env": "FS_EXTENSION_ARTIFACT_MAX_BYTES",
                }
            ],
            "outputs": [
                {
                    "kind": "artifact",
                    "binding_key": "target",
                    "max_size_bytes": 200 * 1024 * 1024,
                    "max_size_env": "FS_EXTENSION_ARTIFACT_MAX_BYTES",
                }
            ],
        },
        input_payload={"artifact_handle": "artifact:source"},
        project_state={},
    )

    expected = 2 * 1024 * 1024 * 1024
    assert requests["artifact_inputs"][0]["max_size_bytes"] == expected
    assert requests["artifact_inputs"][0]["content_type"] == "application/x-ndjson"
    assert requests["artifact_output_specs"][0]["max_size_bytes"] == expected


def test_artifact_policy_opt_in_default_is_host_owned(monkeypatch) -> None:
    from core.application.orchestration.connector_grants import _policy_max_size

    monkeypatch.delenv("FS_EXTENSION_ARTIFACT_MAX_BYTES", raising=False)

    assert (
        _policy_max_size(
            {
                "kind": "artifact",
                "max_size_bytes": 10 * 1024 * 1024 * 1024,
                "max_size_env": "FS_EXTENSION_ARTIFACT_MAX_BYTES",
            }
        )
        == 200 * 1024 * 1024
    )


def test_host_rejects_artifact_input_above_resolved_policy_before_grant(tmp_path) -> None:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter
    from core.application.extensions.runtime_grants import prepare_extension_grants
    from core.application.management.errors import ServiceValidationError
    from core.application.management.scope import ScopeContext

    plane = ArtifactDatasetPlane(
        storage=LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifact_store"))
    )
    handle, _meta = plane.create_artifact(
        owner_scope={"account_id": "acc", "project_id": "project"},
        run_id="run",
        step_id="step",
        filename="source.bin",
        content_type="application/octet-stream",
        body=b"too-large",
    )

    _raises_input_4880_1 = ScopeContext(
        account_id="acc", project_id="project", allowed_project_ids=frozenset({"project"})
    )
    with pytest.raises(ServiceValidationError, match="exceeds max_size_bytes=4"):
        prepare_extension_grants(
            plane=plane,
            scope=_raises_input_4880_1,
            audit={"account_id": "acc", "project_id": "project"},
            extension_id="fixture",
            artifact_inputs=[
                {
                    "artifact_handle": handle.artifact_id,
                    "binding_key": "source",
                    "max_size_bytes": 4,
                }
            ],
        )


@pytest.mark.parametrize(("status", "is_error"), [("succeeded", False), ("failed", True)])
def test_an_extension_step_reports_its_call_and_result_and_keeps_the_job_alive(status, is_error):
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    registry = SimpleNamespace(tools=SimpleNamespace(get_tool=lambda _tool_id: None))
    extension_logger = MagicMock()
    heartbeat_logger = MagicMock()
    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime_helpers.resolve_extension_runtime_record",
            return_value=(
                {
                    "runtime": {"contract_version": "extension_host_v2"},
                    "extension_key": "provider",
                    "action_definitions": [{"action_id": "send"}],
                },
                extension_logger,
            ),
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
            return_value={"status": status, "response": {"ok": status == "succeeded"}},
        ),
        patch(
            "core.application.orchestration.connector_step_runtime._shared_job_store",
            return_value=heartbeat_logger,
        ),
        patch(
            "core.infrastructure.extension_runtime.extension_invocation_audit.log_host_extension_invocation"
        ),
    ):
        execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "provider.send",
                "connector_install_id": "pi_provider_1",
                "input_mapping_jsonb": {"record_id": "record-a"},
            },
            state=_run_state(
                [{"step_id": "step-send"}],
                job_id=13,
                account_id="acc_alpha",
                project_id="project_alpha",
            ),
        )

    events = [
        call.kwargs
        for call in extension_logger.log_event.call_args_list
        if call.kwargs.get("event_type") in {"tool_call", "tool_result"}
    ]
    assert [event["event_type"] for event in events] == ["tool_call", "tool_result"]
    for event in events:
        assert event["phase"] == "step-send"
        assert event["detail"]["step_id"] == "step-send"
        assert event["detail"]["tool_name"] == "provider.send"
        assert "args_excerpt" not in event["detail"]
        assert "result_excerpt" not in event["detail"]
    assert events[1]["detail"]["is_error"] is is_error
    first_beat = heartbeat_logger.update_job_runtime.call_args_list[0]
    assert first_beat.args == ("13",)
    assert first_beat.kwargs["last_response_excerpt"] == "step-send: started"


def test_step_events_reach_the_job_store_when_the_shared_logger_cannot_write_them():
    """The integration store has no log_event; events sent to it vanished in production."""
    from core.infrastructure.db.platform_stores import PlatformIntegrationStore, PlatformJobStore

    assert not hasattr(PlatformIntegrationStore, "log_event")
    assert not hasattr(PlatformIntegrationStore, "update_job_runtime")
    assert hasattr(PlatformJobStore, "log_event")
    assert hasattr(PlatformJobStore, "update_job_runtime")

    job_store = MagicMock()
    with patch.object(connector_events, "_shared_job_store", return_value=job_store):
        connector_events._emit_connector_tool_event(
            SimpleNamespace(),
            job_id="job-1",
            group_id="",
            step_id="export-products",
            tool_id="shopify.export_products",
            function_name="export_products",
            event_type="tool_call",
            excerpt="",
            include_excerpt=False,
        )

    job_store.log_event.assert_called_once()
    assert job_store.log_event.call_args.kwargs["detail"]["step_id"] == "export-products"


def test_external_effect_identity_requires_the_step_not_a_phase():
    from core.application.orchestration.connector_effects import (
        _external_effect_runtime_identity_error,
    )

    extension = {
        "compiled_manifest": {
            "external_effects": [
                {
                    "operation_id": "send_email",
                    "effect_kind": "message_delivery",
                    "channel": "email",
                }
            ]
        }
    }
    runtime_context = {
        "job_id": "job-queued",
        "workflow_id": "wf-queued",
        "account_id": "account-queued",
        "project_id": "project-queued",
        "workspace_id": "workspace-queued",
    }

    error = _external_effect_runtime_identity_error(
        extension=extension, operation_id="send_email", runtime_context=runtime_context
    )
    assert error is not None
    assert error["missing_fields"] == ["step_id"]
    assert (
        _external_effect_runtime_identity_error(
            extension=extension,
            operation_id="send_email",
            runtime_context={**runtime_context, "step_id": "send-email"},
        )
        is None
    )


@pytest.mark.parametrize("existing_lookup_id", [None, "real-id-from-independent-lookup"])
def test_ambiguous_failed_connector_does_not_promote_partial_id_or_erase_independent_output(
    existing_lookup_id,
):
    from core.application.orchestration.connector_step_runtime import execute_connector_step

    state = {"lookup_id": existing_lookup_id} if existing_lookup_id else {}
    original_state = deepcopy(state)
    failed = {
        "handled": True,
        "ok": False,
        "runtime": "extension_action",
        "operation_kind": "action",
        "external_effect": {
            "effect_id": "11111111-1111-1111-1111-111111111111",
            "status": "timeout_unknown",
            "claim_version": 1,
        },
        "invocation": {
            "status": "failed",
            "detail": "provider_timeout",
            "response": {
                "ok": False,
                "external_effect_status": "timeout_unknown",
                "result": {"record_id": "unverified-partial-original-id"},
            },
        },
    }
    looked_up = {
        "handled": True,
        "ok": True,
        "runtime": "extension_action",
        "operation_kind": "source",
        "invocation": {
            "status": "succeeded",
            "response": {"ok": True, "result": {"record_id": "real-correlated-provider-id"}},
        },
    }
    with (
        patch(
            "core.application.orchestration.connector_step_runtime._dispatch_tool_runtime",
            return_value=None,
        ),
        patch(
            "core.application.orchestration.connector_step_runtime._dispatch_extension_runtime",
            side_effect=[failed, looked_up],
        ),
    ):
        ambiguous = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "connector.fixture",
                "output_mapping_jsonb": {"record_id": "state.original_id"},
            },
            state=state,
        )
        lookup = execute_connector_step(
            step={
                "step_kind": "connector",
                "connector_id": "connector.fixture",
                "output_mapping_jsonb": {"record_id": "state.lookup_id"},
            },
            state=state,
        )
    assert ambiguous["ok"] is False
    assert not (ambiguous.get("state_patch") or {}).get("original_id")
    assert not (ambiguous.get("mapped_outputs") or {}).get("record_id")
    assert (
        ambiguous["invocation"]["response"]["result"]["record_id"]
        == "unverified-partial-original-id"
    )
    assert state == original_state
    assert lookup["state_patch"]["lookup_id"] == "real-correlated-provider-id"
    assert lookup["mapped_outputs"]["record_id"] == "real-correlated-provider-id"
