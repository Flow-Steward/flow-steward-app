from __future__ import annotations

from unittest.mock import patch

import pytest

from core.application.registry.contract_validation import (
    validate_agent_skill_compatibility,
    validate_contract_items,
    validate_guardrail_readiness,
    validate_invoke_workflow_step,
    validate_tool_readiness,
    validate_workflow_readiness,
)
from tests._workflow_definitions import edge, linear_graph, linear_workflow, node, stop_step


class _WorkflowOnlyDB:
    def __init__(self, workflow: dict | None):
        self._workflow = workflow

    def get_workflow(self, workflow_id: str):
        if not isinstance(self._workflow, dict):
            return None
        if str(self._workflow.get("workflow_id") or "") != str(workflow_id or ""):
            return None
        return self._workflow


class _WorkflowWithToolsDB(_WorkflowOnlyDB):
    def __init__(self, workflow: dict | None, tools: dict[str, dict]):
        super().__init__(workflow)
        self._tools = {str(key): dict(value) for key, value in tools.items()}

    def get_tool(self, tool_id: str):
        row = self._tools.get(str(tool_id or ""))
        return dict(row) if isinstance(row, dict) else None


class _WorkflowValidationDB(_WorkflowOnlyDB):
    def __init__(
        self,
        workflow: dict | None,
        *,
        tools: dict[str, dict] | None = None,
        skill_inputs: dict[str, list[dict]] | None = None,
        skill_outputs: dict[str, list[dict]] | None = None,
        agent_inputs: dict[str, list[dict]] | None = None,
        agent_outputs: dict[str, list[dict]] | None = None,
    ):
        super().__init__(workflow)
        self._tools = {str(k): dict(v) for k, v in (tools or {}).items()}
        self._skill_inputs = {str(k): list(v) for k, v in (skill_inputs or {}).items()}
        self._skill_outputs = {str(k): list(v) for k, v in (skill_outputs or {}).items()}
        self._agent_inputs = {str(k): list(v) for k, v in (agent_inputs or {}).items()}
        self._agent_outputs = {str(k): list(v) for k, v in (agent_outputs or {}).items()}

    def get_tool(self, tool_id: str):
        row = self._tools.get(str(tool_id or ""))
        return dict(row) if isinstance(row, dict) else None

    def get_skill_inputs(self, skill_id: str):
        return list(self._skill_inputs.get(str(skill_id or ""), []))

    def get_skill_outputs(self, skill_id: str):
        return list(self._skill_outputs.get(str(skill_id or ""), []))

    def get_agent_inputs(self, agent_id: str):
        return list(self._agent_inputs.get(str(agent_id or ""), []))

    def get_agent_outputs(self, agent_id: str):
        return list(self._agent_outputs.get(str(agent_id or ""), []))

    def get_agent_skills(self, agent_id: str):
        _ = agent_id
        return []

    def get_agent_tools(self, agent_id: str):
        _ = agent_id
        return []

    def get_effective_tools(self, agent_id: str):
        _ = agent_id
        return []


class _InvokeValidationDB:
    def __init__(self, workflows: dict[str, dict]):
        self._workflows = {str(k): dict(v) for k, v in (workflows or {}).items()}

    def get_workflow(self, workflow_id: str):
        row = self._workflows.get(str(workflow_id or ""))
        return dict(row) if isinstance(row, dict) else None

    def get_tool(self, tool_id: str):
        _ = tool_id

    def get_skill_inputs(self, skill_id: str):
        _ = skill_id
        return []

    def get_skill_outputs(self, skill_id: str):
        _ = skill_id
        return []

    def get_agent_inputs(self, agent_id: str):
        _ = agent_id
        return []

    def get_agent_outputs(self, agent_id: str):
        _ = agent_id
        return []


class _ToolGuardrailValidationDB:
    def __init__(
        self, *, tool: dict | None = None, guardrail: dict | None = None, agents: dict | None = None
    ):
        self._tool = dict(tool) if isinstance(tool, dict) else None
        self._guardrail = dict(guardrail) if isinstance(guardrail, dict) else None
        self._agents = {str(k): dict(v) for k, v in (agents or {}).items()}

    def get_tool(self, tool_id: str):
        if not isinstance(self._tool, dict):
            return None
        if str(self._tool.get("tool_id") or "") != str(tool_id or ""):
            return None
        return dict(self._tool)

    def get_guardrail(self, guardrail_id: int, *, account_id=None):
        if not isinstance(self._guardrail, dict):
            return None
        if int(self._guardrail.get("id") or 0) != int(guardrail_id or 0):
            return None
        return dict(self._guardrail)

    def get_agent(self, agent_id: str):
        row = self._agents.get(str(agent_id or ""))
        return dict(row) if isinstance(row, dict) else None

    def get_data_contract(self, contract_id: str):
        return None


class _ContractValidationDB:
    def __init__(self, contracts: dict[str, dict] | None = None):
        self._contracts = {str(k): dict(v) for k, v in (contracts or {}).items()}

    def get_data_contract(self, contract_id: str):
        row = self._contracts.get(str(contract_id or ""))
        return dict(row) if isinstance(row, dict) else None


class _PairValidationDB:
    def __init__(self, *, agent_enabled: bool = True, skill_enabled: bool = True):
        self._agent_enabled = bool(agent_enabled)
        self._skill_enabled = bool(skill_enabled)

    def get_agent(self, agent_id: str):
        if str(agent_id or "") != "author":
            return None
        return {"agent_id": "author", "enabled": self._agent_enabled}

    def get_skill(self, skill_id: str):
        if str(skill_id or "") != "write_section":
            return None
        return {
            "skill_id": "write_section",
            "enabled": self._skill_enabled,
            "_inputs": [{"name": "request", "type": "text", "required": True}],
            "_outputs": [{"name": "patch_diff", "type": "text", "required": True}],
        }

    def get_agent_skills(self, agent_id: str):
        return [{"skill_id": "write_section"}]

    def get_agent_tools(self, agent_id: str):
        return []

    def get_effective_tools(self, agent_id: str):
        return []


def _error_codes(validation: dict) -> set[str]:
    return {
        str(item.get("code") or "").strip()
        for item in (validation.get("errors") or [])
        if isinstance(item, dict)
    }


def _warning_codes(validation: dict) -> set[str]:
    return {
        str(item.get("code") or "").strip()
        for item in (validation.get("warnings") or [])
        if isinstance(item, dict)
    }


def _graph(entry: str, *edges: dict) -> dict:
    return {"schema_version": 1, "entry": node(entry), "edges": list(edges)}


def _graph_codes(validation: dict) -> set[str]:
    return {code for code in _error_codes(validation) if code.startswith("workflow.graph.")}


def _error_paths(validation: dict, code: str) -> set[str]:
    return {
        str(item.get("path") or "")
        for item in (validation.get("errors") or [])
        if isinstance(item, dict) and item.get("code") == code
    }


def _set_variable(step_id: str, target: str, value) -> dict:
    return {
        "step_id": step_id,
        "step_kind": "set_variable",
        "override": {"set_variable": {"target": target, "value": value}},
    }


def _switch(step_id: str, selector: str, *case_ids: str) -> dict:
    return {
        "step_id": step_id,
        "step_kind": "switch",
        "override": {
            "switch": {
                "selector": selector,
                "cases": [{"case_id": case_id, "when": case_id} for case_id in case_ids],
            }
        },
    }


class _EmptyIOResolver:
    """Stands in for IOResolver when a test is about step config, not data flow."""

    def __init__(self, db):
        self.db = db

    def get_step_outputs(self, workflow_id: str, step_id: str):
        return []

    def get_available_outputs_for_step(self, workflow_id: str, step_id: str):
        return []

    def validate_step_io(self, workflow_id: str, step_id: str):
        return []


def test_workflow_readiness_rejects_a_step_routed_after_a_stop() -> None:
    workflow = linear_workflow(
        _set_variable("mark-started", "state.workflow_started", True),
        stop_step("done"),
        _set_variable("produce-output", "state.customer_output", "ready"),
        workflow_id="wf-action-after-terminal",
    )

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), workflow["workflow_id"])

    assert "workflow.execution_graph.edges[1].outcome" in _error_paths(
        validation, "workflow.graph.outcome_unsupported"
    )


def test_readiness_rejects_unreachable_intermediate_step() -> None:
    workflow = linear_workflow(
        _switch("route-outcome", "state.outcome", "ok"),
        _set_variable("must-execute", "state.must_execute", True),
        stop_step("succeeded"),
        stop_step("failed", "stop_fail"),
        workflow_id="wf-unreachable-intermediate-step",
        settings={
            "execution_graph": _graph(
                "route-outcome",
                edge("route-outcome", node("succeeded"), "case", case_id="ok"),
                edge("route-outcome", node("failed"), "default"),
                edge("must-execute", node("succeeded")),
            )
        },
    )

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), workflow["workflow_id"])

    assert _error_paths(validation, "workflow.graph.unreachable") == {"workflow.steps.must-execute"}


def test_readiness_rejects_a_step_nothing_routes_to_after_a_stop() -> None:
    workflow = linear_workflow(
        stop_step("done"),
        _set_variable("after-stop", "state.after_stop", True),
        workflow_id="wf-action-after-terminal-step",
        settings={"execution_graph": _graph("done", edge("after-stop", node("done")))},
    )

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), workflow["workflow_id"])

    assert _error_paths(validation, "workflow.graph.unreachable") == {"workflow.steps.after-stop"}


def test_readiness_rejects_a_switch_without_a_default_edge() -> None:
    workflow = linear_workflow(
        _switch("route-outcome", "state.outcome", "ok", "bad"),
        stop_step("succeeded"),
        stop_step("failed", "stop_fail"),
        workflow_id="wf-mixed-terminal-fallthrough",
        settings={
            "execution_graph": _graph(
                "route-outcome",
                edge("route-outcome", node("succeeded"), "case", case_id="ok"),
                edge("route-outcome", node("failed"), "case", case_id="bad"),
            )
        },
    )

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), workflow["workflow_id"])

    missing = [
        item for item in validation["errors"] if item["code"] == "workflow.graph.outcome_missing"
    ]
    assert [item["path"] for item in missing] == ["workflow.steps.route-outcome"]
    assert "default" in missing[0]["message"]


def _std_json_pick_tools() -> dict:
    return {
        "std.json": {
            "tool_id": "std.json",
            "enabled": True,
            "interface": {
                "functions": [
                    {
                        "name": "pick",
                        "parameters": [
                            {"name": "value", "type": "json", "required": True},
                            {"name": "path", "type": "string", "required": True},
                        ],
                        "returns": {"name": "result", "type": "json", "required": True},
                    }
                ]
            },
        }
    }


def test_validate_workflow_readiness_preserves_step_io_issue_context():
    class _ResolverStub(_EmptyIOResolver):
        def validate_step_io(self, workflow_id: str, step_id: str):
            if step_id != "design":
                return []
            return [
                {
                    "input_name": "source_brief",
                    "step_id": step_id,
                    "issue_type": "expected_input_source_unavailable",
                    "source": "message.step.analysis.artifacts.source_brief",
                    "source_step_id": "analysis",
                    "source_name": "source_brief",
                    "path": "boundary_contract.entry[0].source",
                    "severity": "error",
                    "message": (
                        "Expected input 'source_brief' source "
                        "'message.step.analysis.artifacts.source_brief' not available from "
                        "upstream steps or state"
                    ),
                }
            ]

    db = _WorkflowOnlyDB(
        linear_workflow(
            {
                "step_id": "design",
                "step_kind": "http_request",
                "boundary_contract": {
                    "entry": [
                        {
                            "name": "source_brief",
                            "source": "message.step.analysis.artifacts.source_brief",
                        }
                    ]
                },
                "override": {
                    "http_request": {
                        "method": "GET",
                        "url": "https://example.test/health",
                        "response_target": "state.http.health",
                        "expected_statuses": [200],
                        "timeout_seconds": 30,
                    }
                },
            },
            workflow_id="wf_step_io_context",
        )
    )

    with patch("core.application.registry.contract_validation.IOResolver", _ResolverStub):
        validation = validate_workflow_readiness(db, "wf_step_io_context")

    issue = next(
        item
        for item in validation.get("errors") or []
        if item.get("code") == "workflow.step_io.error"
    )
    assert issue["path"] == "workflow.steps.design.boundary_contract.entry[0].source"
    assert issue["step_id"] == "design"
    assert issue["input_name"] == "source_brief"
    assert issue["source"] == "message.step.analysis.artifacts.source_brief"
    assert issue["source_step_id"] == "analysis"
    assert issue["source_name"] == "source_brief"


def _http_request_step(step_id: str, **config) -> dict:
    return {
        "step_id": step_id,
        "step_kind": "http_request",
        "override": {
            "http_request": {
                "method": "GET",
                "url": "https://example.test/health",
                "response_target": "state.http.health",
                "expected_statuses": [200],
                "timeout_seconds": 30,
                **config,
            }
        },
    }


def _if_workflow(workflow_id: str, condition) -> dict:
    return linear_workflow(
        {"step_id": "route", "step_kind": "if", "override": {"if": {"condition": condition}}},
        {
            "step_id": "matched",
            "step_kind": "stop_success",
            "override": {"stop_success": {"message": "done"}},
        },
        workflow_id=workflow_id,
        settings={
            "execution_graph": _graph(
                "route",
                edge("route", node("matched"), "true"),
                edge("route", node("matched"), "false"),
            )
        },
    )


def test_readiness_accepts_explicit_expected_failure_terminal() -> None:
    workflow = linear_workflow(
        _set_variable("mark-started", "state.started", True),
        stop_step("failed", "stop_fail"),
        workflow_id="wf-expected-failure",
        settings={"traits": {"expected_failure": True}},
    )

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), workflow["workflow_id"])
    codes = _error_codes(validation)

    assert "workflow.terminal_fail_missing" not in codes
    assert "workflow.terminal_fail_intent_missing" not in codes
    assert _graph_codes(validation) == set()


def test_readiness_requires_expected_failure_intent_for_lone_stop_fail() -> None:
    workflow = linear_workflow(stop_step("failed", "stop_fail"), workflow_id="wf-unmarked-failure")

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), workflow["workflow_id"])

    assert "workflow.terminal_fail_intent_missing" in _error_codes(validation)
    assert _graph_codes(validation) == set()


def test_readiness_accepts_exhaustive_switch_to_success_and_failure_terminals() -> None:
    workflow = linear_workflow(
        _switch("route-outcome", "state.outcome", "ok"),
        stop_step("succeeded"),
        stop_step("failed", "stop_fail"),
        workflow_id="wf-mixed-terminals",
        settings={
            "execution_graph": _graph(
                "route-outcome",
                edge("route-outcome", node("succeeded"), "case", case_id="ok"),
                edge("route-outcome", node("failed"), "default"),
            )
        },
    )

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), workflow["workflow_id"])

    assert _graph_codes(validation) == set()


def test_readiness_accepts_exhaustive_if_to_success_and_failure_terminals() -> None:
    workflow = linear_workflow(
        {
            "step_id": "route-outcome",
            "step_kind": "if",
            "override": {
                "if": {"condition": {"selector": "state.should_succeed", "operator": "truthy"}}
            },
        },
        stop_step("succeeded"),
        stop_step("failed", "stop_fail"),
        workflow_id="wf-mixed-if-terminals",
        settings={
            "execution_graph": _graph(
                "route-outcome",
                edge("route-outcome", node("succeeded"), "true"),
                edge("route-outcome", node("failed"), "false"),
            )
        },
    )

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), workflow["workflow_id"])

    assert _graph_codes(validation) == set()


def test_readiness_rejects_router_before_later_unreachable_exhaustive_router() -> None:
    workflow = linear_workflow(
        _switch("first-router", "state.first_outcome", "ok"),
        _switch("unreachable-exhaustive-router", "state.final_outcome", "ok"),
        stop_step("succeeded"),
        stop_step("failed", "stop_fail"),
        workflow_id="wf-dead-exhaustive-router",
        settings={
            "execution_graph": _graph(
                "first-router",
                edge("first-router", node("succeeded"), "case", case_id="ok"),
                edge("first-router", node("succeeded"), "default"),
                edge("unreachable-exhaustive-router", node("succeeded"), "case", case_id="ok"),
                edge("unreachable-exhaustive-router", node("failed"), "default"),
            )
        },
    )

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), workflow["workflow_id"])

    assert "workflow.steps.unreachable-exhaustive-router" in _error_paths(
        validation, "workflow.graph.unreachable"
    )


def test_readiness_rejects_unknown_branch_target() -> None:
    workflow = linear_workflow(
        _switch("route-outcome", "state.outcome", "ok"),
        stop_step("succeeded"),
        workflow_id="wf-unknown-branch-target",
        settings={
            "execution_graph": _graph(
                "route-outcome",
                edge("route-outcome", node("missing-step"), "case", case_id="ok"),
                edge("route-outcome", node("missing-step"), "default"),
            )
        },
    )

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), workflow["workflow_id"])

    assert "workflow.graph.target_unknown" in _error_codes(validation)


def test_validate_workflow_readiness_warns_callable_without_inputs():
    db = _WorkflowOnlyDB(
        {
            "workflow_id": "wf_no_inputs",
            "settings": {
                "callable": {
                    "enabled": True,
                    "output_mode": "none",
                    "expected_inputs": [],
                    "expected_outputs": [],
                }
            },
            "steps": [],
        }
    )

    validation = validate_workflow_readiness(db, "wf_no_inputs")

    assert "workflow.callable.inputs_empty" in _warning_codes(validation)


def test_validate_workflow_readiness_resolves_projected_extension_workflow():
    workflow = {
        "workflow_id": "extension.install-1.demo",
        "settings": {"callable": {"enabled": False}},
        "steps": [],
    }
    db = _WorkflowOnlyDB(None)

    with patch(
        "core.application.registry.projected_artifact_rows.projected_artifact_rows",
        return_value=[workflow],
    ):
        validation = validate_workflow_readiness(db, workflow["workflow_id"])

    assert "workflow.not_found" not in _error_codes(validation)


def test_validate_workflow_readiness_prefers_projection_over_project_fk_anchor():
    workflow_id = "extension.install-1.demo"
    anchor = {
        "workflow_id": workflow_id,
        "enabled": True,
        "source_install_id": "install-1",
        "projection_mode": "derived",
        "steps": [],
        "groups": [],
    }
    projection = {
        **anchor,
        "steps": [stop_step()],
        "settings": {"execution_graph": linear_graph(["done"])},
    }
    db = _WorkflowOnlyDB(anchor)

    with patch(
        "core.application.registry.projected_artifact_rows.projected_artifact_rows",
        return_value=[projection],
    ):
        validation = validate_workflow_readiness(db, workflow_id)

    assert "workflow.steps_missing" not in _error_codes(validation)


def test_validate_workflow_readiness_allow_empty_inputs_suppresses_warning():
    db = _WorkflowOnlyDB(
        {
            "workflow_id": "wf_no_inputs",
            "settings": {
                "callable": {
                    "enabled": True,
                    "output_mode": "none",
                    "expected_inputs": [],
                    "expected_outputs": [],
                    # Author explicitly acknowledges a callable that takes no inputs
                    # (for example a demo that invokes a child or fails on purpose).
                    "allow_empty_inputs": True,
                }
            },
            "steps": [],
        }
    )

    validation = validate_workflow_readiness(db, "wf_no_inputs")

    assert "workflow.callable.inputs_empty" not in _warning_codes(validation)


def test_validate_agent_skill_compatibility_allows_configuring_disabled_agent():
    db = _PairValidationDB(agent_enabled=False, skill_enabled=True)
    validation = validate_agent_skill_compatibility(db, "author", "write_section")
    codes = _error_codes(validation)

    assert validation.get("valid") is True
    assert "agent.disabled" not in codes


def test_validate_agent_skill_compatibility_rejects_disabled_skill():
    db = _PairValidationDB(agent_enabled=True, skill_enabled=False)
    validation = validate_agent_skill_compatibility(db, "author", "write_section")
    codes = _error_codes(validation)

    assert validation.get("valid") is False
    assert "skill.disabled" in codes


def test_workflow_pre_run_readiness_rejects_a_disabled_agent_dependency():
    workflow = linear_workflow(
        {"step_id": "write", "step_kind": "agent", "agent_id": "author"},
        workflow_id="wf_disabled_agent",
    )

    class _Db(_PairValidationDB):
        def __init__(self):
            super().__init__(agent_enabled=False, skill_enabled=True)

        def get_workflow(self, workflow_id: str):
            return workflow if workflow_id == "wf_disabled_agent" else None

        def get_agent(self, agent_id: str):
            row = super().get_agent(agent_id)
            if row:
                row["_skills"] = [{"skill_id": "write_section"}]
                row["_tools"] = []
            return row

        def get_skill_inputs(self, skill_id: str):
            return []

        def get_skill_outputs(self, skill_id: str):
            return []

        def get_agent_inputs(self, agent_id: str):
            return []

        def get_agent_outputs(self, agent_id: str):
            return []

    validation = validate_workflow_readiness(_Db(), "wf_disabled_agent")

    assert "workflow.step_contract.agent.disabled" in _error_codes(validation)


def test_validate_workflow_readiness_accepts_structured_request_input_mapping():
    workflow = linear_workflow(
        {
            "step_id": "step_generate_launch_draft",
            "display_name": "Generate Launch Email Draft",
            "step_kind": "agent",
            "agent_id": "sample_launch_copywriter",
            "skill_id": "sample_write_launch_copy",
            "input_mapping_jsonb": {
                "request": {
                    "campaign_brief": "$inputs.campaign_brief",
                    "target_audience": "$inputs.target_audience",
                }
            },
            "output_mapping_jsonb": {
                "approved_subject": "state.approved_subject",
                "approved_body": "state.approved_body",
            },
        },
        stop_step("step_finish"),
        workflow_id="wf_structured_request",
        settings={
            "callable": {
                "expected_inputs": [
                    {"name": "campaign_brief", "type": "text", "required": True},
                    {"name": "target_audience", "type": "text", "required": True},
                ],
                "trigger_channels": ["api"],
                "expected_outputs": [
                    {"name": "approved_subject", "type": "text", "required": True},
                    {"name": "approved_body", "type": "text", "required": True},
                ],
            }
        },
    )
    db = _WorkflowValidationDB(
        workflow,
        skill_inputs={
            "sample_write_launch_copy": [
                {"name": "request", "type": "text", "required": True},
            ]
        },
        skill_outputs={
            "sample_write_launch_copy": [
                {"name": "approved_subject", "type": "text", "required": True},
                {"name": "approved_body", "type": "text", "required": True},
            ]
        },
    )

    validation = validate_workflow_readiness(db, "wf_structured_request")

    assert not any(
        "not produced by upstream steps" in str(item.get("message") or "")
        for item in (validation.get("errors") or [])
        if isinstance(item, dict)
    )


def test_validate_workflow_readiness_rejects_connector_step_missing_required_manifest_input():
    workflow = linear_workflow(
        {
            "step_id": "workflow_start",
            "display_name": "Start workflow",
            "step_kind": "set_variable",
        },
        {
            "step_id": "pick_json_path",
            "display_name": "Pick JSON path",
            "step_kind": "connector",
            "connector_id": "connector.std.json",
            "operation_id": "pick",
            "input_mapping_jsonb": {
                "path": "state.inputs.path",
            },
            "override": {
                "connector": {
                    "operation_id": "pick",
                    "function_name": "pick",
                    "input_mapping": {
                        "path": "state.inputs.path",
                    },
                }
            },
            "output_mapping_jsonb": {"result": "state.picked_value"},
        },
        stop_step("workflow_success"),
        workflow_id="wf_json_pick",
        settings={
            "callable": {
                "enabled": True,
                "trigger_channels": ["api"],
                "expected_inputs": [
                    {"name": "json_payload", "type": "json", "required": True},
                    {"name": "path", "type": "text", "required": True},
                ],
                "expected_outputs": [{"name": "picked_value", "type": "json", "required": True}],
            }
        },
    )
    db = _WorkflowValidationDB(workflow, tools=_std_json_pick_tools())

    validation = validate_workflow_readiness(db, "wf_json_pick")

    assert validation.get("valid") is False
    assert any(
        "Required input 'value' (from connector.std.json::pick) not produced by upstream steps"
        in str(item.get("message") or "")
        for item in (validation.get("errors") or [])
        if isinstance(item, dict)
    )


def test_validate_workflow_readiness_accepts_connector_callable_input_and_literal_mapping():
    workflow = linear_workflow(
        _set_variable("workflow_start", "state.started", True),
        {
            "step_id": "pick_json_path",
            "step_kind": "connector",
            "connector_id": "connector.std.json",
            "operation_id": "pick",
            "boundary_contract": {
                "entry": [{"name": "order_payload", "type": "json", "required": True}],
                "exit": ["customer_email"],
            },
            "input_mapping_jsonb": {
                "value": "state.inputs.order_payload",
                "path": "literal:customer.email",
            },
            "override": {
                "connector": {
                    "operation_id": "pick",
                    "function_name": "pick",
                    "input_mapping": {
                        "value": "state.inputs.order_payload",
                        "path": "literal:customer.email",
                    },
                    "output_mapping": {"result": "state.customer_email"},
                }
            },
            "output_mapping_jsonb": {"result": "state.customer_email"},
        },
        stop_step("workflow_success"),
        workflow_id="wf_json_pick",
        settings={
            "callable": {
                "enabled": True,
                "trigger_channels": ["api"],
                "expected_inputs": [
                    {"name": "order_payload", "type": "json", "required": True},
                ],
                "expected_outputs": [{"name": "customer_email", "type": "json"}],
            }
        },
    )
    db = _WorkflowValidationDB(workflow, tools=_std_json_pick_tools())

    validation = validate_workflow_readiness(db, "wf_json_pick")

    assert validation.get("valid") is True, validation.get("errors")


def test_validate_contract_items_accepts_matching_data_contract():
    db = _ContractValidationDB(
        {
            "outline.v1": {
                "contract_id": "outline.v1",
                "kind": "json",
                "enabled": True,
            }
        }
    )

    normalized, issues = validate_contract_items(
        [{"name": "outline", "type": "json", "contract_id": "outline.v1"}],
        path_prefix="skill.expected_outputs",
        default_required=True,
        db=db,
    )

    assert issues == []
    assert normalized == [
        {
            "name": "outline",
            "type": "object",
            "description": "",
            "required": True,
            "contract_id": "outline.v1",
        }
    ]


def test_validate_contract_items_rejects_missing_data_contract():
    db = _ContractValidationDB({})

    _, issues = validate_contract_items(
        [{"name": "outline", "type": "json", "contract_id": "missing.v1"}],
        path_prefix="skill.expected_outputs",
        default_required=True,
        db=db,
    )

    assert any(str(item.get("code")) == "contract.data_contract_missing" for item in issues)


def test_validate_contract_items_rejects_data_contract_kind_mismatch():
    db = _ContractValidationDB(
        {
            "story_ids.v1": {
                "contract_id": "story_ids.v1",
                "kind": "list",
                "enabled": True,
            }
        }
    )

    _, issues = validate_contract_items(
        [{"name": "outline", "type": "json", "contract_id": "story_ids.v1"}],
        path_prefix="skill.expected_outputs",
        default_required=True,
        db=db,
    )

    assert any(str(item.get("code")) == "contract.data_contract_kind_mismatch" for item in issues)


def test_validate_workflow_readiness_callable_output_mode_none_allows_no_outputs():
    db = _WorkflowOnlyDB(
        {
            "workflow_id": "wf_callable",
            "settings": {
                "callable": {
                    "enabled": True,
                    "output_mode": "none",
                    "expected_inputs": [{"name": "ticket_id", "type": "text", "required": True}],
                    "expected_outputs": [],
                }
            },
            "steps": [],
        }
    )

    validation = validate_workflow_readiness(db, "wf_callable")
    codes = _error_codes(validation)

    assert validation.get("valid") is False
    assert "workflow.interface.variable_outputs_missing" not in codes
    assert "workflow.steps_missing" in codes


def test_validate_workflow_readiness_ignores_a_stored_output_mode():
    """What a workflow returns follows from its outputs; a stored mode demands nothing."""
    db = _WorkflowOnlyDB(
        {
            "workflow_id": "wf_callable",
            "settings": {
                "callable": {
                    "enabled": True,
                    "output_mode": "side_effect",
                    "side_effects": [],
                    "expected_inputs": [{"name": "ticket_id", "type": "text", "required": True}],
                    "expected_outputs": [],
                }
            },
            "steps": [],
        }
    )

    validation = validate_workflow_readiness(db, "wf_callable")
    codes = _error_codes(validation) | _warning_codes(validation)

    assert (
        not {
            "workflow.interface.variable_outputs_missing",
            "workflow.interface.artifacts_missing",
            "workflow.interface.side_effects_missing",
        }
        & codes
    )


def test_validate_workflow_readiness_rejects_hollow_control_flow_only_workflow():
    db = _WorkflowOnlyDB(
        linear_workflow(
            {
                "step_id": "shape_result",
                "step_kind": "set_variable",
                "override": {
                    "set_variable": {"assignments": {"state.result": {"source": "literal:ok"}}}
                },
                "output_mapping_jsonb": {"result": "state.result"},
            },
            workflow_id="wf_hollow",
            settings={
                "callable": {
                    "enabled": True,
                    "output_mode": "return",
                    "expected_outputs": [{"name": "result", "type": "text", "required": True}],
                }
            },
        )
    )

    validation = validate_workflow_readiness(db, "wf_hollow")
    codes = _error_codes(validation)

    assert validation.get("valid") is False
    assert "workflow.execution_bindings_missing" in codes


def test_validate_tool_readiness_detects_missing_display_name():
    db = _ToolGuardrailValidationDB(
        tool={
            "tool_id": "search_tool",
            "display_name": "",
            "version": "1.0.0",
            "interface": {},
            "implementation": {},
            "config": {},
            "tags": [],
            "enabled": True,
        }
    )
    validation = validate_tool_readiness(db, "search_tool")
    codes = _error_codes(validation)

    assert validation.get("valid") is False
    assert "tool.display_name_missing" in codes


def test_validate_tool_readiness_rejects_unresolvable_enabled_internal_tool():
    db = _ToolGuardrailValidationDB(
        tool={
            "tool_id": "broken_tool",
            "display_name": "Broken tool",
            "version": "1.0.0",
            "interface": {"function_name": "missing_fn", "parameters": []},
            "implementation": {"type": "internal", "module": "does.not.exist"},
            "config": {},
            "tags": [],
            "enabled": True,
        }
    )
    validation = validate_tool_readiness(db, "broken_tool")
    codes = _error_codes(validation)

    assert validation.get("valid") is False
    assert "tool.runtime_unresolvable" in codes


def test_validate_tool_readiness_rejects_enabled_unsupported_implementation_type():
    db = _ToolGuardrailValidationDB(
        tool={
            "tool_id": "python_tool",
            "display_name": "Python tool",
            "version": "1.0.0",
            "interface": {"function_name": "run_py", "parameters": []},
            "implementation": {
                "type": "python",
                "entrypoint": "core.application.orchestration.tools.python_tool:run",
            },
            "config": {},
            "tags": [],
            "enabled": True,
        }
    )
    validation = validate_tool_readiness(db, "python_tool")
    codes = _error_codes(validation)

    assert validation.get("valid") is False
    assert "tool.runtime_validation_unsupported" in codes


def test_validate_tool_readiness_accepts_extension_implementation_kind():
    db = _ToolGuardrailValidationDB(
        tool={
            "tool_id": "provider_tool",
            "display_name": "Provider Tool",
            "version": "1.0.0",
            "interface": {"function_name": "echo_text", "parameters": []},
            "implementation": {"kind": "extension", "extension_key": "example_tool_provider"},
            "config": {},
            "tags": [],
            "enabled": True,
        }
    )

    validation = validate_tool_readiness(db, "provider_tool")
    codes = _error_codes(validation)

    assert "tool.runtime_validation_unsupported" not in codes


def test_validate_guardrail_readiness_detects_missing_scoped_agent():
    db = _ToolGuardrailValidationDB(
        guardrail={
            "id": 42,
            "display_name": "Validate output",
            "mode": "deterministic",
            "rule_type": "custom",
            "severity": "error",
            "config": {},
            "_agents": ["author", "missing_agent"],
        },
        agents={"author": {"agent_id": "author"}},
    )
    validation = validate_guardrail_readiness(db, 42)
    codes = _error_codes(validation)

    assert validation.get("valid") is False
    assert "guardrail.agent_missing" in codes


def test_validate_guardrail_readiness_rejects_empty_agent_scope():
    db = _ToolGuardrailValidationDB(
        guardrail={
            "id": 42,
            "display_name": "Validate output",
            "mode": "deterministic",
            "rule_type": "custom",
            "severity": "error",
            "config": {},
            "_agents": [],
        },
    )

    validation = validate_guardrail_readiness(db, 42)
    codes = _error_codes(validation)

    assert validation.get("valid") is False
    assert "guardrail.agent_scope_empty" in codes


def test_validate_workflow_readiness_callable_rejects_invalid_types():
    db = _WorkflowOnlyDB(
        {
            "workflow_id": "wf_callable_bad",
            "settings": {
                "callable": {
                    "enabled": True,
                    "expected_outputs": [{"name": "result", "type": "blob"}],
                }
            },
            "steps": [],
        }
    )

    validation = validate_workflow_readiness(db, "wf_callable_bad")
    assert validation.get("valid") is False
    errors = validation.get("errors") or []
    assert any(
        str(item.get("code")) == "contract.invalid_type"
        and str(item.get("path")) == "workflow.callable.expected_outputs[0].type"
        for item in errors
        if isinstance(item, dict)
    )


def test_validate_workflow_readiness_rejects_legacy_callable_boolean_settings():
    db = _WorkflowOnlyDB(
        {
            "workflow_id": "wf_callable_bool",
            "settings": {
                "callable": True,
                "callable_expected_inputs": [{"name": "request", "type": "text"}],
                "callable_expected_outputs": [{"name": "plan", "type": "json", "required": True}],
            },
            "steps": [],
        }
    )

    validation = validate_workflow_readiness(db, "wf_callable_bool")
    codes = _error_codes(validation)

    assert validation.get("valid") is False
    assert "workflow.steps_missing" in codes
    assert "workflow.interface.invalid_callable_shape" in codes


def test_validate_workflow_readiness_callable_output_must_be_produced_by_workflow():
    db = _WorkflowOnlyDB(
        linear_workflow(
            workflow_id="wf_callable_unproducible",
            settings={
                "callable": {
                    "enabled": True,
                    "expected_inputs": [{"name": "request", "type": "text"}],
                    "expected_outputs": [{"name": "final_plan", "type": "json", "required": True}],
                }
            },
        )
    )

    validation = validate_workflow_readiness(db, "wf_callable_unproducible")
    codes = _error_codes(validation)

    assert validation.get("valid") is False
    assert "workflow.callable.output_not_produced" in codes


def test_an_optional_callable_output_no_step_produces_is_a_warning_not_a_refusal():
    """R2-11: a hook's steps may leave a result unset; it then keeps its input."""
    db = _WorkflowOnlyDB(
        linear_workflow(
            workflow_id="wf_callable_optional",
            settings={
                "callable": {
                    "enabled": True,
                    "expected_inputs": [{"name": "request", "type": "text"}],
                    "expected_outputs": [{"name": "final_plan", "type": "json", "required": False}],
                }
            },
        )
    )

    validation = validate_workflow_readiness(db, "wf_callable_optional")

    assert "workflow.callable.output_not_produced" not in _error_codes(validation)
    assert "workflow.callable.output_not_produced" in {
        warning["code"] for warning in validation.get("warnings") or []
    }


def test_validate_workflow_readiness_uses_connector_return_type_for_callable_output():
    from core.application.registry.builtin_standard_tools import builtin_standard_tool_manifests

    db = _WorkflowWithToolsDB(
        linear_workflow(
            _set_variable("workflow_start", "state.workflow_started", True),
            {
                "step_id": "uppercase_text",
                "step_kind": "connector",
                "connector_id": "connector.std.text",
                "boundary_contract": {"exit": ["upper_text"]},
                "input_mapping_jsonb": {"value": "state.inputs.text"},
                "output_mapping_jsonb": {"result": "state.upper_text"},
                "override": {
                    "connector": {
                        "function_name": "upper",
                        "input_mapping": {"value": "state.inputs.text"},
                        "output_mapping": {"result": "state.upper_text"},
                    }
                },
            },
            {
                "step_id": "finish_text_flow",
                "step_kind": "stop_success",
                "override": {"stop_success": {"reason": "Uppercase flow complete."}},
            },
            workflow_id="wf_callable_connector",
            settings={
                "callable": {
                    "enabled": True,
                    "trigger_channels": ["parent_workflow"],
                    "expected_inputs": [{"name": "text", "type": "paragraph"}],
                    "expected_outputs": [
                        {"name": "upper_text", "type": "paragraph", "required": True}
                    ],
                }
            },
        ),
        builtin_standard_tool_manifests(),
    )

    validation = validate_workflow_readiness(db, "wf_callable_connector")
    codes = _error_codes(validation)

    assert validation.get("valid") is True, validation.get("errors")
    assert "workflow.callable.output_type_mismatch" not in codes
    assert "workflow.boundary_exit_not_produced" not in codes


def _install_scoped_scalar_tool() -> dict:
    return {
        "tool_id": "extension.install-showcase.demo_tools",
        "account_id": "account-showcase",
        "source_install_id": "install-showcase",
        "enabled": True,
        "implementation": {
            "type": "extension",
            "extension_key": "acme.example-showcase",
            "action": "invoke_tool",
        },
        "interface": {
            "functions": [
                {
                    "name": "inspect_result",
                    "returns": {
                        "type": "object",
                        "required": ["row_count", "summary"],
                        "properties": {
                            "row_count": {"type": "integer"},
                            "summary": {"type": "string"},
                        },
                    },
                }
            ]
        },
    }


def _install_scoped_scalar_workflow() -> dict:
    return linear_workflow(
        {
            "step_id": "inspect",
            "step_kind": "connector",
            "connector_id": "extension.install-showcase.demo_tools",
            "connector_install_id": "install-showcase",
            "boundary_contract": {"exit": ["catalog_row_count", "weather_summary"]},
            "override": {"connector": {"function_name": "inspect_result"}},
            "output_mapping_jsonb": {
                "row_count": "state.catalog_row_count",
                "summary": "state.weather_summary",
            },
        },
        workflow_id="extension.install-showcase.demo_scalar_outputs",
        account_id="account-showcase",
        settings={
            "callable": {
                "enabled": True,
                "trigger_channels": ["parent_workflow"],
                "expected_inputs": [],
                "expected_outputs": [
                    {"name": "catalog_row_count", "type": "number", "required": True},
                    {"name": "weather_summary", "type": "paragraph", "required": True},
                ],
            }
        },
    )


def _active_showcase_install_contract() -> dict:
    return {
        "account_id": "account-showcase",
        "extension_id": "acme.example-showcase",
        "install_id": "install-showcase",
        "install_state": "active",
        "operation_manifest": [{"operation_id": "invoke_tool", "outputs": []}],
    }


def test_callable_validation_preserves_install_scoped_tool_scalar_return_types():
    from core.application.registry.io_resolver import IOResolver

    workflow = _install_scoped_scalar_workflow()
    tool = _install_scoped_scalar_tool()
    db = _WorkflowValidationDB(workflow, tools={tool["tool_id"]: tool})

    with patch.object(
        IOResolver,
        "_get_extension_install_contract",
        return_value=_active_showcase_install_contract(),
    ):
        validation = validate_workflow_readiness(db, workflow["workflow_id"])

    codes = _error_codes(validation)
    assert "workflow.callable.output_type_mismatch" not in codes
    assert "workflow.boundary_exit_not_produced" not in codes


@pytest.mark.parametrize("authority_failure", ["missing_install", "wrong_install", "wrong_account"])
def test_install_scoped_tool_output_contract_fails_closed_without_exact_authority(
    authority_failure: str,
):
    from core.application.registry.io_resolver import IOResolver

    workflow = _install_scoped_scalar_workflow()
    tool = _install_scoped_scalar_tool()
    contract = _active_showcase_install_contract()
    step = workflow["steps"][0]
    if authority_failure == "missing_install":
        step.pop("connector_install_id")
    elif authority_failure == "wrong_install":
        contract["install_id"] = "install-other"
    else:
        contract["account_id"] = "account-other"
    db = _WorkflowValidationDB(workflow, tools={tool["tool_id"]: tool})

    with patch.object(IOResolver, "_get_extension_install_contract", return_value=contract):
        outputs = IOResolver(db).get_available_outputs_for_step(workflow["workflow_id"], "done")

    by_name = {item["name"]: item["type"] for item in outputs}
    assert by_name["catalog_row_count"] == "json"
    assert by_name["weather_summary"] == "json"


def test_child_invoke_validation_preserves_install_scoped_tool_string_return_type():
    from core.application.registry.io_resolver import IOResolver

    child = _install_scoped_scalar_workflow()
    child["settings"]["callable"]["expected_outputs"] = [
        {"name": "weather_summary", "type": "paragraph", "required": True}
    ]
    child["steps"][0]["boundary_contract"] = {"exit": ["weather_summary"]}
    tool = _install_scoped_scalar_tool()

    class _InstallScopedInvokeDB(_InvokeValidationDB):
        def get_tool(self, tool_id: str):
            return dict(tool) if tool_id == tool["tool_id"] else None

    db = _InstallScopedInvokeDB(workflows={child["workflow_id"]: child})
    step = {
        "step_id": "run_child",
        "step_kind": "invoke_workflow",
        "override": {
            "invoke_workflow": {
                "workflow_id": child["workflow_id"],
                "input_mapping": {},
                "output_mapping": {"weather_summary": "weather_summary"},
            }
        },
    }

    with patch.object(
        IOResolver,
        "_get_extension_install_contract",
        return_value=_active_showcase_install_contract(),
    ):
        validation = validate_invoke_workflow_step(
            db,
            workflow_id="wf_parent",
            step=step,
            path_prefix="workflow.steps.run_child",
        )

    codes = _error_codes(validation)
    assert "invoke.child.output_type_mismatch" not in codes
    assert "invoke.child.output_not_produced" not in codes


def test_validate_workflow_readiness_rejects_raw_mcp_connector_id():
    db = _WorkflowOnlyDB(
        linear_workflow(
            _set_variable("workflow_start", "state.workflow_started", True),
            {
                "step_id": "sync_remote",
                "step_kind": "connector",
                "connector_id": "mcp_c134570ca411419d",
                "override": {
                    "connector": {
                        "operation_id": "fs_browser_full.project_list",
                    }
                },
            },
            {
                "step_id": "workflow_success",
                "step_kind": "stop_success",
                "override": {"stop_success": {"message": "Done."}},
            },
            workflow_id="wf_raw_mcp_connector",
        )
    )

    validation = validate_workflow_readiness(db, "wf_raw_mcp_connector")
    codes = _error_codes(validation)

    assert validation.get("valid") is False
    assert "workflow.step_connector_raw_mcp_unsupported" in codes
    assert _error_paths(validation, "workflow.step_connector_raw_mcp_unsupported") == {
        "workflow.steps.sync_remote.connector_id"
    }


def test_validate_workflow_readiness_rejects_missing_direct_projected_tool():
    workflow = linear_workflow(
        {
            "step_id": "remote_lookup",
            "step_kind": "connector",
            "connector_id": "connector.remote.lookup",
            "override": {"connector": {"operation_id": "lookup"}},
        },
        workflow_id="wf_missing_projected_tool",
    )

    class _Db(_WorkflowOnlyDB):
        def get_tool(self, _tool_id: str):
            return None

    validation = validate_workflow_readiness(_Db(workflow), workflow["workflow_id"])

    assert "workflow.step_connector_tool_unavailable" in _error_codes(validation)
    assert any("remote.lookup" in str(error.get("message") or "") for error in validation["errors"])


def test_validate_workflow_readiness_rejects_agent_with_missing_projected_tool():
    workflow = linear_workflow(
        {
            "step_id": "agent_run",
            "step_kind": "agent",
            "agent_id": "agent.remote",
        },
        workflow_id="wf_agent_missing_projected_tool",
    )

    class _Db(_WorkflowOnlyDB):
        def get_agent(self, _agent_id: str):
            return {"agent_id": "agent.remote", "tools": [{"tool_id": "remote.resolve"}]}

        def get_agent_skills(self, _agent_id: str):
            return []

        def get_agent_inputs(self, _agent_id: str):
            return []

        def get_agent_outputs(self, _agent_id: str):
            return []

        def get_tool(self, _tool_id: str):
            return None

    validation = validate_workflow_readiness(_Db(workflow), workflow["workflow_id"])

    assert "workflow.agent_tool_unavailable" in _error_codes(validation)
    assert any(
        "remote.resolve" in str(error.get("message") or "") for error in validation["errors"]
    )


def test_validate_workflow_readiness_cannot_use_derived_workflow_block_as_producer():
    class _WorkflowWithPublishedBlockDB(_WorkflowOnlyDB):
        def get_published_block_contract(self, block_key: str):
            if str(block_key or "") != "workflow.wf_callable_projected":
                return None
            return {
                "outputs": [
                    {"field_key": "review_report", "value_type": "text"},
                ]
            }

    db = _WorkflowWithPublishedBlockDB(
        linear_workflow(
            workflow_id="wf_callable_projected",
            settings={
                "callable": {
                    "enabled": True,
                    "expected_inputs": [{"name": "request", "type": "text"}],
                    "expected_outputs": [
                        {"name": "review_report", "type": "text", "required": True}
                    ],
                }
            },
        )
    )

    validation = validate_workflow_readiness(db, "wf_callable_projected")
    codes = _error_codes(validation)

    assert validation.get("valid") is False
    assert "workflow.callable.output_not_produced" in codes


def test_validate_workflow_readiness_cannot_use_derived_json_block_as_array_producer():
    class _WorkflowWithPublishedBlockDB(_WorkflowOnlyDB):
        def get_published_block_contract(self, block_key: str):
            if str(block_key or "") != "workflow.wf_callable_array":
                return None
            return {
                "outputs": [
                    {"field_key": "files_changed", "value_type": "json"},
                ]
            }

    db = _WorkflowWithPublishedBlockDB(
        linear_workflow(
            workflow_id="wf_callable_array",
            settings={
                "callable": {
                    "enabled": True,
                    "expected_inputs": [{"name": "request", "type": "text"}],
                    "expected_outputs": [
                        {"name": "files_changed", "type": "array", "required": True}
                    ],
                }
            },
        )
    )

    validation = validate_workflow_readiness(db, "wf_callable_array")
    codes = _error_codes(validation)

    assert "workflow.callable.output_type_mismatch" not in codes
    assert "workflow.callable.output_not_produced" in codes


def test_validate_workflow_readiness_handles_non_object_step_rows():
    db = _WorkflowOnlyDB({"workflow_id": "wf_bad_step_rows", "steps": ["broken_step_row"]})

    with patch("core.application.registry.contract_validation.IOResolver", _EmptyIOResolver):
        validation = validate_workflow_readiness(db, "wf_bad_step_rows")
    codes = _error_codes(validation)

    assert validation.get("valid") is False
    assert "workflow.step_invalid_type" in codes
    assert "workflow.steps_missing" in codes


def test_validate_workflow_readiness_http_request_step_does_not_require_agent_binding():
    db = _WorkflowOnlyDB(
        linear_workflow(
            _set_variable("mark_started", "state.workflow_started", True),
            _http_request_step("check_health"),
            {
                "step_id": "end",
                "step_kind": "stop_success",
                "override": {"stop_success": {"message": "done"}},
            },
            workflow_id="wf_http_request",
        )
    )

    with patch("core.application.registry.contract_validation.IOResolver", _EmptyIOResolver):
        validation = validate_workflow_readiness(db, "wf_http_request")
    codes = _error_codes(validation)

    assert validation.get("valid") is True, validation.get("errors")
    assert "workflow.step_agent_missing" not in codes


def test_validate_workflow_readiness_rejects_http_request_env_auth_ref():
    db = _WorkflowOnlyDB(
        linear_workflow(
            _set_variable("mark_started", "state.workflow_started", True),
            _http_request_step("check_health", auth_mode="bearer", auth_ref="env:DATABASE_URL"),
            workflow_id="wf_http_env_auth",
        )
    )

    with patch("core.application.registry.contract_validation.IOResolver", _EmptyIOResolver):
        validation = validate_workflow_readiness(db, "wf_http_env_auth")
    codes = _error_codes(validation)

    assert validation.get("valid") is False
    assert "workflow.step_http_request_auth_ref_env_forbidden" in codes


def test_validate_workflow_readiness_rejects_http_request_invalid_response_limit():
    db = _WorkflowOnlyDB(
        linear_workflow(
            _http_request_step(
                "fetch_file",
                url="https://example.test/file",
                response_target="state.http.file",
                response_mode="artifact",
                max_response_bytes=201 * 1024 * 1024,
            ),
            workflow_id="wf_http_limit",
        )
    )

    with patch("core.application.registry.contract_validation.IOResolver", _EmptyIOResolver):
        validation = validate_workflow_readiness(db, "wf_http_limit")
    codes = _error_codes(validation)

    assert validation.get("valid") is False
    assert "workflow.step_http_request_max_response_bytes_invalid" in codes


def test_validate_workflow_readiness_rejects_authenticated_templated_origin():
    db = _WorkflowOnlyDB(
        linear_workflow(
            _http_request_step(
                "fetch_file",
                url="https://{{state.host}}/file",
                response_target="state.http.file",
                auth_mode="bearer",
                auth_ref="api_token",
            ),
            workflow_id="wf_http_auth_origin",
        )
    )

    with patch("core.application.registry.contract_validation.IOResolver", _EmptyIOResolver):
        validation = validate_workflow_readiness(db, "wf_http_auth_origin")
    codes = _error_codes(validation)

    assert validation.get("valid") is False
    assert "workflow.step_http_request_auth_url_origin_invalid" in codes


def test_validate_workflow_readiness_validates_if_condition_operator_shape():
    db = _WorkflowOnlyDB(
        _if_workflow(
            "wf_if_condition",
            {"selector": "state.inputs.city", "operator": "unsupported", "value": "Berlin"},
        )
    )

    with patch("core.application.registry.contract_validation.IOResolver", _EmptyIOResolver):
        validation = validate_workflow_readiness(db, "wf_if_condition")
    codes = _error_codes(validation)

    assert validation.get("valid") is False
    assert "workflow.step_if_condition_operator_invalid" in codes


def test_validate_workflow_readiness_rejects_if_constant_conditions():
    for condition in (True, 1, "True", "false", "nonsense"):
        db = _WorkflowOnlyDB(_if_workflow("wf_if_constant", condition))

        with patch("core.application.registry.contract_validation.IOResolver", _EmptyIOResolver):
            validation = validate_workflow_readiness(db, "wf_if_constant")
        codes = _error_codes(validation)

        assert validation.get("valid") is False
        assert "workflow.step_if_condition_invalid" in codes


def test_validate_workflow_readiness_accepts_if_state_expression_condition():
    db = _WorkflowOnlyDB(_if_workflow("wf_if_expression", "state.inputs.city == 'Berlin'"))

    with patch("core.application.registry.contract_validation.IOResolver", _EmptyIOResolver):
        validation = validate_workflow_readiness(db, "wf_if_expression")
    codes = _error_codes(validation)

    assert "workflow.step_if_condition_invalid" not in codes
    assert _graph_codes(validation) == set()


def test_validate_workflow_readiness_accepts_if_constant_operator_condition():
    db = _WorkflowOnlyDB(_if_workflow("wf_if_constant_operator", {"operator": "always_true"}))

    with patch("core.application.registry.contract_validation.IOResolver", _EmptyIOResolver):
        validation = validate_workflow_readiness(db, "wf_if_constant_operator")
    codes = _error_codes(validation)

    assert "workflow.step_if_condition_invalid" not in codes
    assert "workflow.step_if_condition_selector_missing" not in codes


def test_validate_workflow_readiness_rejects_switch_cases_without_targets():
    workflow = linear_workflow(
        {
            "step_id": "route_channel_switch",
            "step_kind": "switch",
            "override": {
                "switch": {
                    "selector": "state.inputs.channel",
                    "cases": ["email", "social"],
                }
            },
        },
        workflow_id="wf_switch_bad",
    )

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), "wf_switch_bad")

    assert validation["valid"] is False
    assert "workflow.step_switch_cases_invalid" in _error_codes(validation)


def test_validate_invoke_step_requires_output_mapping_for_declared_child_outputs():
    db = _InvokeValidationDB(
        workflows={
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_inputs": [{"name": "request", "type": "text", "required": True}],
                        "expected_outputs": [{"name": "plan", "type": "json", "required": True}],
                    }
                },
            }
        }
    )
    step = {
        "agent_id": "planner",
        "skill_id": "create_plan",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "input_mapping": {"request": "state.task_summary"},
                "output_mapping": {},
            }
        },
    }

    validation = validate_invoke_workflow_step(
        db,
        workflow_id="wf_parent",
        step=step,
        path_prefix="workflow.steps.run_child",
    )

    codes = _error_codes(validation)
    assert validation.get("valid") is False
    assert "invoke.required_output_unmapped" in codes


def test_validate_invoke_step_rejects_host_owned_output_mapping_target():
    db = _InvokeValidationDB(
        workflows={
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "trigger_channels": ["parent_workflow"],
                        "expected_inputs": [],
                        "expected_outputs": [
                            {"name": "enabled", "type": "boolean", "required": True}
                        ],
                    }
                },
            }
        }
    )
    step = {
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "input_mapping": {},
                "output_mapping": {"enabled": "state.test_mode"},
            }
        }
    }

    validation = validate_invoke_workflow_step(
        db,
        workflow_id="wf_parent",
        step=step,
        path_prefix="workflow.steps.run_child",
    )

    assert validation.get("valid") is False
    assert "invoke.output_mapping_target_reserved" in _error_codes(validation)


def test_validate_invoke_step_allows_unmapped_optional_child_outputs():
    db = _InvokeValidationDB(
        workflows={
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_inputs": [{"name": "request", "type": "text", "required": True}],
                        "expected_outputs": [
                            {"name": "plan", "type": "json", "required": True},
                            {"name": "commit_sha", "type": "text", "required": False},
                        ],
                    }
                },
            }
        }
    )
    step = {
        "agent_id": "planner",
        "skill_id": "create_plan",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "input_mapping": {"request": "state.task_summary"},
                "output_mapping": {"plan": "child_plan"},
            }
        },
    }

    validation = validate_invoke_workflow_step(
        db,
        workflow_id="wf_parent",
        step=step,
        path_prefix="workflow.steps.run_child",
    )

    codes = _error_codes(validation)
    assert "invoke.required_output_unmapped" not in codes


def test_validate_invoke_step_rejects_child_without_parent_workflow_channel():
    db = _InvokeValidationDB(
        workflows={
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "trigger_channels": ["api"],
                        "expected_inputs": [{"name": "request", "type": "text", "required": True}],
                        "output_mode": "none",
                    }
                },
            }
        }
    )
    step = {
        "agent_id": "planner",
        "skill_id": "create_plan",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "input_mapping": {"request": "state.task_summary"},
                "output_mapping": {},
            }
        },
    }

    validation = validate_invoke_workflow_step(
        db,
        workflow_id="wf_parent",
        step=step,
        path_prefix="workflow.steps.run_child",
    )

    codes = _error_codes(validation)
    assert validation.get("valid") is False
    assert "invoke.child_not_parent_workflow_triggerable" in codes


def test_validate_invoke_step_allows_a_parallel_lane():
    db = _InvokeValidationDB(
        workflows={
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "trigger_channels": ["parent_workflow"],
                        "expected_inputs": [],
                        "output_mode": "none",
                    }
                },
            }
        }
    )
    step = {
        "step_id": "run_child",
        "step_kind": "invoke_workflow",
        "group_id": "fan_out",
        "lane_id": "lane_a",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "input_mapping": {},
                "output_mapping": {},
            }
        },
    }

    validation = validate_invoke_workflow_step(
        db,
        workflow_id="wf_parent",
        step=step,
        path_prefix="workflow.steps.run_child",
    )

    codes = _error_codes(validation)
    assert validation.get("valid") is True
    assert "invoke.parallel_group_unsupported" not in codes


_INLINE_LOOP_SETTINGS = {
    "callable": {
        "enabled": True,
        "output_mode": "none",
        "expected_inputs": [],
        "expected_outputs": [],
        "allow_empty_inputs": True,
    }
}


def _inline_loop_workflow(*loop_steps: dict, workflow_id: str = "wf_inline_loop_contract") -> dict:
    """Start marker, the given loop steps, an HTTP probe, then Stop Success."""
    return linear_workflow(
        _set_variable("mark_started", "state.workflow_started", True),
        *loop_steps,
        {
            "step_id": "probe",
            "step_kind": "http_request",
            "override": {
                "http_request": {
                    "method": "GET",
                    "url": "https://example.com",
                    "response_target": "state.probe",
                    "timeout_seconds": 5,
                    "expected_statuses": [200],
                }
            },
        },
        workflow_id=workflow_id,
        settings={"callable": dict(_INLINE_LOOP_SETTINGS["callable"])},
    )


def _transform_step(workflow: dict) -> dict:
    return next(step for step in workflow["steps"] if step["step_id"] == "normalize_payload")


def _loop_around_transform(workflow: dict, loop: dict) -> dict:
    """Move the transform into a For each entered from the start marker."""
    _transform_step(workflow)["for_each_id"] = "normalize"
    workflow["for_each"] = [
        {
            "for_each_id": "normalize",
            "over": loop["over"],
            "item_name": loop.get("item_name", "item"),
            "index_name": loop.get("index_name"),
            "body": {"kind": "sequence", "entry": node("normalize_payload")},
        }
    ]
    workflow["settings"]["execution_graph"] = _graph(
        "mark_started",
        edge("mark_started", node("normalize", "for_each")),
        edge(
            "normalize_payload",
            {"kind": "for_each_exit", "for_each_id": "normalize", "lane_id": None},
        ),
        edge("normalize", node("done"), source_kind="for_each"),
    )
    return workflow


@pytest.mark.parametrize("inputs", [["message", None], {}, "message"])
def test_readiness_reports_malformed_boundary_entry_without_mutating_storage(inputs):
    from copy import deepcopy

    workflow = _workflow_with_top_level_transform("state.inputs.payload")
    _transform_step(workflow)["boundary_contract"] = {"entry": inputs}
    before = deepcopy(workflow)
    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), "wf_transform_ready")
    expected_code = (
        "contract.item_not_object" if isinstance(inputs, list) else "contract.items_not_list"
    )
    matching = [issue for issue in validation["errors"] if issue["code"] == expected_code]
    assert matching
    assert matching[0]["path"].startswith(
        "workflow.steps.normalize_payload.boundary_contract.entry"
    )
    assert validation["valid"] is False
    assert workflow == before


def test_validate_workflow_readiness_accepts_transform_grounded_by_sourced_boundary_entry():
    workflow = _workflow_with_top_level_transform("state.inputs.payload", callable_input=False)
    _transform_step(workflow)["boundary_contract"] = {
        "entry": [
            {
                "name": "payload",
                "type": "json",
                "required": True,
                "source": "state.workflow_started",
            }
        ]
    }

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), "wf_transform_ready")

    assert validation["valid"] is True, validation["errors"]
    assert "workflow.execution_bindings_missing" not in _error_codes(validation)


def test_validate_workflow_readiness_rejects_transform_grounded_by_sourceless_boundary_entry():
    workflow = _workflow_with_top_level_transform("state.inputs.payload", callable_input=False)
    _transform_step(workflow)["boundary_contract"] = {
        "entry": [{"name": "payload", "type": "json", "required": True}]
    }

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), "wf_transform_ready")

    assert validation["valid"] is False
    assert any(
        error.get("issue_type") == "transform_input_source_unavailable"
        for error in validation.get("errors") or []
    )


def test_validate_invoke_step_rejects_unavailable_parent_source_mapping():
    step = {
        "step_id": "run_child",
        "step_kind": "invoke_workflow",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "input_mapping": {"ticket_id": "message.step.analysis.artifacts.source_brief"},
                "output_mapping": {"result": "child_result"},
            }
        },
    }
    db = _InvokeValidationDB(
        workflows={
            "wf_parent": linear_workflow(step, workflow_id="wf_parent"),
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_inputs": [
                            {"name": "ticket_id", "type": "text", "required": True}
                        ],
                        "expected_outputs": [{"name": "result", "type": "text", "required": True}],
                    }
                },
            },
        }
    )

    validation = validate_invoke_workflow_step(
        db,
        workflow_id="wf_parent",
        step=step,
        path_prefix="workflow.steps.run_child",
    )

    codes = _error_codes(validation)
    assert validation.get("valid") is False
    assert "invoke.input_mapping_source_unavailable" in codes
    issue = next(
        item
        for item in validation.get("errors") or []
        if item.get("code") == "invoke.input_mapping_source_unavailable"
    )
    assert (
        issue["path"] == "workflow.steps.run_child.override.invoke_workflow.input_mapping.ticket_id"
    )
    assert issue["input_name"] == "ticket_id"
    assert issue["source"] == "message.step.analysis.artifacts.source_brief"
    assert issue["source_step_id"] == "analysis"
    assert issue["source_name"] == "source_brief"


def test_validate_invoke_step_rejects_input_mapping_type_mismatch():
    step = {
        "step_id": "run_child",
        "step_kind": "invoke_workflow",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "input_mapping": {"effort_score": "state.task_summary"},
                "output_mapping": {"result": "child_result"},
            }
        },
    }
    db = _InvokeValidationDB(
        workflows={
            "wf_parent": linear_workflow(step, workflow_id="wf_parent"),
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_inputs": [
                            {"name": "effort_score", "type": "number", "required": True}
                        ],
                        "expected_outputs": [{"name": "result", "type": "text", "required": True}],
                    }
                },
            },
        }
    )

    validation = validate_invoke_workflow_step(
        db,
        workflow_id="wf_parent",
        step=step,
        path_prefix="workflow.steps.run_child",
    )

    codes = _error_codes(validation)
    assert validation.get("valid") is False
    assert "invoke.input_mapping_type_mismatch" in codes


def test_validate_invoke_step_rejects_indirect_recursion_cycle():
    db = _InvokeValidationDB(
        workflows={
            "wf_a": {"workflow_id": "wf_a", "steps": [], "groups": []},
            "wf_b": {
                "workflow_id": "wf_b",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_inputs": [{"name": "request", "type": "text", "required": True}],
                        "expected_outputs": [{"name": "result", "type": "text", "required": True}],
                    }
                },
                "steps": [
                    {
                        "step_id": "call_a",
                        "step_kind": "invoke_workflow",
                        "override": {
                            "invoke_workflow": {
                                "workflow_id": "wf_a",
                                "input_mapping": {"request": "state.task_summary"},
                                "output_mapping": {"result": "child_result"},
                            }
                        },
                    }
                ],
                "groups": [],
            },
        }
    )
    step = {
        "step_id": "call_b",
        "step_kind": "invoke_workflow",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_b",
                "input_mapping": {"request": "state.task_summary"},
                "output_mapping": {"result": "child_result"},
            }
        },
    }

    validation = validate_invoke_workflow_step(
        db,
        workflow_id="wf_a",
        step=step,
        path_prefix="workflow.steps.call_b",
    )

    codes = _error_codes(validation)
    assert validation.get("valid") is False
    assert "invoke.indirect_recursion_forbidden" in codes


def test_validate_invoke_step_rejects_direct_self_recursion():
    db = _InvokeValidationDB(
        workflows={
            "wf_parent": {
                "workflow_id": "wf_parent",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "trigger_channels": ["parent_workflow"],
                        "output_mode": "none",
                        "expected_inputs": [],
                    }
                },
            }
        }
    )
    step = {
        "step_id": "run_child",
        "step_kind": "invoke_workflow",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_parent",
                "input_mapping": {},
                "output_mapping": {},
            }
        },
    }

    validation = validate_invoke_workflow_step(
        db,
        workflow_id="wf_parent",
        step=step,
        path_prefix="workflow.steps.run_child",
    )

    codes = _error_codes(validation)
    assert validation.get("valid") is False
    assert "invoke.self_call_forbidden" in codes


def test_validate_invoke_step_allows_child_workflow_dependency_chain():
    db = _InvokeValidationDB(
        workflows={
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "trigger_channels": ["parent_workflow"],
                        "output_mode": "none",
                        "expected_inputs": [],
                    }
                },
                "steps": [
                    {
                        "step_id": "run_grandchild",
                        "step_kind": "invoke_workflow",
                        "override": {
                            "invoke_workflow": {
                                "workflow_id": "wf_grandchild",
                                "input_mapping": {},
                                "output_mapping": {},
                            }
                        },
                    }
                ],
                "groups": [],
            },
            "wf_grandchild": {
                "workflow_id": "wf_grandchild",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "output_mode": "none",
                        "expected_inputs": [],
                    }
                },
                "steps": [],
                "groups": [],
            },
        }
    )
    step = {
        "step_id": "run_child",
        "step_kind": "invoke_workflow",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "input_mapping": {},
                "output_mapping": {},
            }
        },
    }

    validation = validate_invoke_workflow_step(
        db,
        workflow_id="wf_parent",
        step=step,
        path_prefix="workflow.steps.run_child",
    )

    codes = _error_codes(validation)
    assert validation.get("valid") is True
    assert "invoke.indirect_recursion_forbidden" not in codes
    assert "invoke.self_call_forbidden" not in codes


def test_validate_invoke_step_rejects_child_callable_output_not_produced():
    db = _InvokeValidationDB(
        workflows={
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_inputs": [{"name": "request", "type": "text", "required": True}],
                        "expected_outputs": [{"name": "plan", "type": "json", "required": True}],
                    }
                },
                "steps": [stop_step()],
                "groups": [],
            }
        }
    )
    step = {
        "step_id": "run_child",
        "step_kind": "invoke_workflow",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "input_mapping": {"request": "state.task_summary"},
                "output_mapping": {"plan": "child_plan"},
            }
        },
    }

    validation = validate_invoke_workflow_step(
        db,
        workflow_id="wf_parent",
        step=step,
        path_prefix="workflow.steps.run_child",
    )

    codes = _error_codes(validation)
    assert validation.get("valid") is False
    assert "invoke.child.output_not_produced" in codes


def test_validate_invoke_step_rejects_child_with_only_derived_workflow_output_metadata():
    class _InvokeValidationWithWorkflowBlockDB(_InvokeValidationDB):
        def get_published_block_contract(self, block_key: str):
            if str(block_key or "") != "workflow.wf_child":
                return None
            return {
                "outputs": [
                    {"field_key": "plan", "value_type": "json"},
                ]
            }

    db = _InvokeValidationWithWorkflowBlockDB(
        workflows={
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "expected_inputs": [{"name": "request", "type": "text", "required": True}],
                        "expected_outputs": [{"name": "plan", "type": "json", "required": True}],
                    }
                },
                "steps": [stop_step()],
                "groups": [],
            }
        }
    )
    step = {
        "step_id": "run_child",
        "step_kind": "invoke_workflow",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "input_mapping": {"request": "state.task_summary"},
                "output_mapping": {"plan": "child_plan"},
            }
        },
    }

    validation = validate_invoke_workflow_step(
        db,
        workflow_id="wf_parent",
        step=step,
        path_prefix="workflow.steps.run_child",
    )

    codes = _error_codes(validation)
    assert "invoke.child.output_not_produced" in codes


def test_validate_workflow_readiness_detects_step_published_block_pair_mismatch():
    workflow = linear_workflow(
        {
            "step_id": "draft",
            "step_kind": "agent",
            "agent_id": "planner",
            "skill_id": "draft_plan",
            "override": {"published_block_key": "planner.draft_plan"},
        },
        workflow_id="wf_demo",
    )

    class _WorkflowBlockBindingDB:
        def get_workflow(self, workflow_id: str):
            return workflow if workflow_id == "wf_demo" else None

        def get_agent_skills(self, agent_id: str):
            if agent_id == "planner":
                return [{"skill_id": "draft_plan"}]
            return []

        def get_published_block_contract(self, block_key: str):
            if block_key != "planner.draft_plan":
                return None
            return {
                "block_key": "planner.draft_plan",
                "enabled": True,
                "source_kind": "agent",
                "source_ref": "developer::write_section",
            }

    db = _WorkflowBlockBindingDB()
    with (
        patch("core.application.registry.contract_validation.IOResolver", _EmptyIOResolver),
        patch(
            "core.application.registry.contract_validation.validate_agent_skill_compatibility",
            return_value={"valid": True, "errors": [], "warnings": []},
        ),
    ):
        validation = validate_workflow_readiness(db, "wf_demo")

    codes = _error_codes(validation)
    assert validation.get("valid") is False
    assert "workflow.step_published_block_pair_mismatch" in codes
    assert _error_paths(validation, "workflow.step_published_block_pair_mismatch") == {
        "workflow.steps.draft.override.published_block_key"
    }


def test_validate_workflow_readiness_rejects_reserved_set_variable_target():
    workflow = linear_workflow(
        {
            "step_id": "spoof_scope",
            "step_kind": "set_variable",
            "override": {
                "set_variable": {
                    "assignments": {
                        "state.account_id": "acct-victim",
                        "state.test_mode": True,
                        "state.safe_value": "ok",
                    }
                }
            },
        },
        workflow_id="wf_reserved_set_variable",
        settings={"callable": dict(_INLINE_LOOP_SETTINGS["callable"])},
    )

    validation = validate_workflow_readiness(
        _WorkflowOnlyDB(workflow),
        "wf_reserved_set_variable",
    )

    assert validation.get("valid") is False
    assert "workflow.step_set_variable_target_reserved" in _error_codes(validation)
    reserved_paths = _error_paths(validation, "workflow.step_set_variable_target_reserved")
    assert (
        "workflow.steps.spoof_scope.override.set_variable.assignments.state.test_mode"
        in reserved_paths
    )


def test_validate_workflow_readiness_rejects_reserved_output_mapping_target():
    workflow = linear_workflow(
        {
            "step_id": "spoof_test_mode",
            "step_kind": "agent",
            "agent_id": "writer",
            "output_mapping_jsonb": {"result": "state.test_mode"},
        },
        workflow_id="wf_reserved_output_mapping",
        settings={"callable": dict(_INLINE_LOOP_SETTINGS["callable"])},
    )

    validation = validate_workflow_readiness(
        _WorkflowValidationDB(workflow, agent_outputs={"writer": [{"name": "result"}]}),
        "wf_reserved_output_mapping",
    )

    assert validation.get("valid") is False
    assert "workflow.step_output_mapping_target_reserved" in _error_codes(validation)


@pytest.mark.parametrize("channel", ["messages", "agent_messages"])
def test_a_step_cannot_write_the_graph_conversation_channels(channel):
    """state.messages runs through the LangGraph chat reducer.

    An IMAP template mapped its message summaries there; validation passed, and every
    run on a non-empty mailbox failed with "Message dict must contain 'role' and
    'content' keys". The name is refused when the workflow is checked instead.
    """

    def validation_for(target):
        workflow = linear_workflow(
            {
                "step_id": "read_mailbox",
                "step_kind": "agent",
                "agent_id": "writer",
                "output_mapping_jsonb": {"result": target},
            },
            workflow_id="wf_conversation_channel",
            settings={"callable": dict(_INLINE_LOOP_SETTINGS["callable"])},
        )
        return validate_workflow_readiness(
            _WorkflowValidationDB(workflow, agent_outputs={"writer": [{"name": "result"}]}),
            "wf_conversation_channel",
        )

    refused = validation_for(f"state.{channel}")
    assert refused.get("valid") is False
    assert "workflow.step_output_mapping_target_reserved" in _error_codes(refused)
    message = next(
        issue["message"]
        for issue in refused["errors"]
        if issue["code"] == "workflow.step_output_mapping_target_reserved"
    )
    assert f"state.{channel}" in message and "another state key" in message

    allowed = validation_for(f"state.{channel}_summary")
    assert "workflow.step_output_mapping_target_reserved" not in _error_codes(allowed)


def test_validate_workflow_readiness_rejects_reserved_http_response_target():
    workflow = linear_workflow(
        {
            "step_id": "spoof_test_mode",
            "step_kind": "http_request",
            "override": {
                "http_request": {
                    "method": "GET",
                    "url": "https://example.invalid",
                    "response_target": "state.trusted_runtime_context.test_mode",
                    "timeout_seconds": 5,
                    "expected_statuses": [200],
                }
            },
        },
        workflow_id="wf_reserved_http_target",
    )

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), workflow["workflow_id"])

    assert validation.get("valid") is False
    assert "workflow.step_state_target_reserved" in _error_codes(validation)


def _workflow_with_top_level_transform(source: str, *, callable_input: bool = True) -> dict:
    expected_inputs = [{"name": "payload", "type": "json", "required": True}]
    return linear_workflow(
        _set_variable("mark_started", "state.workflow_started", True),
        {
            "step_id": "normalize_payload",
            "step_kind": "transform",
            "override": {
                "transform": {
                    "input": {"source": source},
                    "result_target": "state.normalized_payload",
                    "fields": [
                        {"from": "name", "to": "name", "type": "text"},
                        {
                            "value": {"literal": "WH-001"},
                            "to": "warehouse_id",
                            "type": "text",
                        },
                    ],
                }
            },
        },
        workflow_id="wf_transform_ready",
        settings={
            "callable": {
                "enabled": True,
                "output_mode": "none",
                "expected_inputs": expected_inputs if callable_input else [],
                "expected_outputs": [],
                "allow_empty_inputs": not callable_input,
                "trigger_channels": ["api"],
            }
        },
    )


def test_validate_workflow_readiness_accepts_transform_grounded_by_prior_output():
    workflow = _workflow_with_top_level_transform("state.workflow_started")

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), "wf_transform_ready")

    assert validation["valid"] is True, validation["errors"]
    assert "workflow.execution_bindings_missing" not in _error_codes(validation)


def test_validate_workflow_readiness_accepts_transform_grounded_by_callable_expected_input():
    workflow = _workflow_with_top_level_transform("state.inputs.payload")

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), "wf_transform_ready")

    assert validation["valid"] is True, validation["errors"]
    assert "workflow.execution_bindings_missing" not in _error_codes(validation)


def test_validate_workflow_readiness_accepts_secret_callable_expected_input():
    workflow = _workflow_with_top_level_transform("state.inputs.payload")
    workflow["settings"]["callable"]["expected_inputs"].append(
        {
            "name": "password",
            "type": "secret",
            "required": True,
            "default_secret_ref": "workflow:wf_transform_ready:password:v1",
        }
    )

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), "wf_transform_ready")

    assert validation["valid"] is True, validation["errors"]
    assert "contract.invalid_type" not in _error_codes(validation)


def test_validate_contract_items_keeps_secret_forbidden_for_outputs_and_artifacts():
    for path_prefix in (
        "workflow.callable.expected_outputs",
        "workflow.callable.returned_files",
    ):
        _, issues = validate_contract_items(
            [{"name": "password", "type": "secret", "required": True}],
            path_prefix=path_prefix,
            default_required=True,
        )

        assert [issue["code"] for issue in issues] == ["contract.invalid_type"]


def test_validate_invoke_step_accepts_secret_child_expected_input_type():
    step = {
        "step_id": "run_child",
        "step_kind": "invoke_workflow",
        "override": {
            "invoke_workflow": {
                "workflow_id": "wf_child",
                "input_mapping": {"password": "state.inputs.password"},
                "output_mapping": {},
            }
        },
    }
    db = _InvokeValidationDB(
        workflows={
            "wf_parent": linear_workflow(
                step,
                workflow_id="wf_parent",
                settings={
                    "callable": {
                        "enabled": True,
                        "expected_inputs": [
                            {"name": "password", "type": "secret", "required": True}
                        ],
                    }
                },
            ),
            "wf_child": {
                "workflow_id": "wf_child",
                "enabled": True,
                "settings": {
                    "callable": {
                        "enabled": True,
                        "output_mode": "none",
                        "trigger_channels": ["parent_workflow"],
                        "expected_inputs": [
                            {
                                "name": "password",
                                "type": "secret",
                                "required": True,
                                "default_secret_ref": "workflow:wf_child:password:v1",
                            }
                        ],
                        "expected_outputs": [],
                    }
                },
                "steps": [],
                "groups": [],
            },
        }
    )

    validation = validate_invoke_workflow_step(
        db,
        workflow_id="wf_parent",
        step=step,
        path_prefix="workflow.steps.run_child",
    )

    assert validation["valid"] is True, validation["errors"]
    assert "contract.invalid_type" not in _error_codes(validation)


def test_validate_workflow_readiness_accepts_transform_over_all_declared_inputs():
    workflow = _workflow_with_top_level_transform("state.inputs")

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), "wf_transform_ready")

    assert validation["valid"] is True, validation["errors"]


def test_validate_workflow_readiness_rejects_transform_over_inputs_none_declared():
    workflow = _workflow_with_top_level_transform("state.inputs", callable_input=False)

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), "wf_transform_ready")

    assert any(
        error.get("issue_type") == "transform_input_source_unavailable"
        for error in validation.get("errors") or []
    )


def test_validate_workflow_readiness_accepts_transform_grounded_by_callable_input_subpath():
    workflow = _workflow_with_top_level_transform("state.inputs.payload.customer")

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), "wf_transform_ready")

    assert validation["valid"] is True, validation["errors"]
    assert "workflow.execution_bindings_missing" not in _error_codes(validation)


def test_validate_workflow_readiness_rejects_transform_grounded_only_by_json_global():
    workflow = _workflow_with_top_level_transform("state.routing_signals", callable_input=False)

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), "wf_transform_ready")

    assert validation["valid"] is False
    assert any(
        error.get("issue_type") == "transform_input_source_unavailable"
        for error in validation.get("errors") or []
    )


def test_validate_workflow_readiness_rejects_transform_without_upstream_source():
    workflow = _workflow_with_top_level_transform("state.missing_payload", callable_input=False)

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), "wf_transform_ready")

    assert validation["valid"] is False
    assert any(
        error.get("issue_type") == "transform_input_source_unavailable"
        for error in validation.get("errors") or []
    )


def test_validate_workflow_readiness_rejects_batch_above_cursor_page_limit():
    workflow = _inline_loop_workflow(
        {
            "step_id": "batch_dataset",
            "step_kind": "batch",
            "override": {
                "batch": {
                    "source_path": "state.inputs.rows",
                    "result_target": "state.dataset_pages",
                    "batch_size": 1001,
                }
            },
        },
        workflow_id="wf_batch_limit",
    )

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), "wf_batch_limit")

    assert validation["valid"] is False
    assert "workflow.step_batch_size_exceeds_page_limit" in _error_codes(validation)


def test_batch_builder_profile_publishes_cursor_page_limit():
    from core.application.registry.builtin_step_kind_profiles import (
        builtin_step_kind_profiles,
    )

    profile = next(row for row in builtin_step_kind_profiles() if row["step_kind_key"] == "batch")
    batch_schema = profile["config_schema_jsonb"]["properties"]["batch"]

    assert batch_schema["properties"]["batch_size"]["maximum"] == 1000


def test_invoke_workflow_step_without_override_is_rejected_for_readiness_parity():
    """F2 (2026-07-05): the runtime reads the child + mappings from override.invoke_workflow, so
    a hand-authored / API / canvas step declared invoke_workflow with only a top-level
    workflow_id column would save clean but never invoke anything. Readiness must reject it."""
    from core.application.registry.contract_validation_invoke import validate_invoke_workflow_step

    result = validate_invoke_workflow_step(
        None,
        workflow_id="parent_wf",
        step={"step_id": "call_child", "step_kind": "invoke_workflow", "workflow_id": "child_wf"},
        path_prefix="workflow.steps.call_child",
    )
    assert result["valid"] is False
    assert [(e.get("code"), e.get("path")) for e in result["errors"]] == [
        (
            "invoke.workflow_id_required",
            "workflow.steps.call_child.override.invoke_workflow.workflow_id",
        )
    ]


def test_non_invoke_step_without_invoke_override_stays_valid():
    from core.application.registry.contract_validation_invoke import validate_invoke_workflow_step

    result = validate_invoke_workflow_step(
        None,
        workflow_id="parent_wf",
        step={"step_id": "research", "step_kind": "agent", "agent_id": "researcher"},
        path_prefix="workflow.steps.research",
    )
    assert result["valid"] is True
    assert result["errors"] == []


def test_validate_workflow_readiness_accepts_transform_reading_a_custom_loop_item():
    """LOOP-001: the runtime publishes any declared item_name; authoring must agree.

    A loop group writes its current item into state before each iteration of its
    body. Authoring once knew only the built-in name, so the same reference was
    accepted for a connector step and rejected for a transform in the same loop.
    """
    workflow = _workflow_with_top_level_transform("state.current_row", callable_input=False)
    _loop_around_transform(workflow, {"over": "workflow_started", "item_name": "current_row"})

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), "wf_transform_ready")

    assert validation["valid"] is True, validation["errors"]
    assert "workflow.execution_bindings_missing" not in _error_codes(validation)


def test_validate_workflow_readiness_accepts_a_subpath_of_the_loop_item():
    workflow = _workflow_with_top_level_transform("state.current_row.sku", callable_input=False)
    _loop_around_transform(workflow, {"over": "workflow_started", "item_name": "current_row"})

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), "wf_transform_ready")

    assert validation["valid"] is True, validation["errors"]


def test_validate_workflow_readiness_accepts_the_for_each_index_name():
    """A named index is published beside the item; authoring agrees."""
    workflow = _workflow_with_top_level_transform("state.row_index", callable_input=False)
    _loop_around_transform(
        workflow,
        {"over": "workflow_started", "item_name": "current_row", "index_name": "row_index"},
    )

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), "wf_transform_ready")

    assert validation["valid"] is True, validation["errors"]


def test_validate_workflow_readiness_rejects_current_item_without_a_loop():
    workflow = _workflow_with_top_level_transform("state.current_item", callable_input=False)

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), "wf_transform_ready")

    assert validation["valid"] is False
    assert any(
        error.get("issue_type") == "transform_input_source_unavailable"
        for error in validation.get("errors") or []
    )


def test_validate_workflow_readiness_still_rejects_a_name_no_loop_publishes():
    """Availability must follow what the loop declares, not accept anything."""
    workflow = _workflow_with_top_level_transform("state.some_other_row", callable_input=False)
    _loop_around_transform(workflow, {"over": "workflow_started", "item_name": "current_row"})

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), "wf_transform_ready")

    assert validation["valid"] is False
    assert any(
        error.get("issue_type") == "transform_input_source_unavailable"
        for error in validation.get("errors") or []
    )


@pytest.mark.parametrize(
    "reserved_target",
    [
        "test_mode",
        "trusted_runtime_context",
        "_trusted_runtime_context",
        "project_id",
    ],
)
def test_workflow_readiness_rejects_reserved_approval_override_target(reserved_target):
    workflow = _workflow_with_top_level_transform("state.workflow_started", callable_input=False)
    workflow["steps"].insert(
        1,
        {
            "step_id": "review",
            "step_kind": "approval",
            "group_id": None,
            "lane_id": None,
            "override": {
                "approval": {"response_mapping": {"override_content_targets": [reserved_target]}}
            },
        },
    )
    workflow["settings"]["execution_graph"] = _graph(
        "mark_started",
        edge("mark_started", node("review")),
        edge("review", node("normalize_payload"), "approved"),
        edge("review", node("done"), "rejected"),
        edge("normalize_payload", node("done")),
    )

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), "wf_transform_ready")

    assert _error_paths(validation, "workflow.approval_response_target_reserved") == {
        "workflow.steps.review.override.approval.response_mapping.override_content_targets[0]"
    }


def _collect_in_for_each(config: dict) -> dict:
    workflow = _workflow_with_top_level_transform("state.inputs.payload")
    workflow["steps"].append(
        {
            "step_id": "keep",
            "step_kind": "collect",
            "for_each_id": "rows",
            "group_id": None,
            "lane_id": None,
            "override": {"collect": config},
        }
    )
    return workflow


@pytest.mark.parametrize(
    ("config", "code"),
    [
        ({"source": "state.row"}, "workflow.step_collect_target_missing"),
        ({"target": "emails", "source": "state.row"}, "workflow.step_collect_target_invalid"),
        ({"target": "state.emails"}, "workflow.step_collect_source_missing"),
        (
            {"target": "state.emails", "source": "state.row", "mode": "flatten"},
            "workflow.step_collect_mode_invalid",
        ),
        (
            {"target": "state.by_email", "source": "state.row", "mode": "set_key"},
            "workflow.step_collect_key_invalid",
        ),
        (
            {"target": "state.emails", "source": "state.row", "key": "x"},
            "workflow.step_collect_key_unexpected",
        ),
        (
            {"target": "state.test_mode", "source": "state.row"},
            "workflow.step_state_target_reserved",
        ),
    ],
)
def test_collect_config_is_checked_before_a_run(config, code):
    workflow = _collect_in_for_each(config)

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), workflow["workflow_id"])

    assert code in _error_codes(validation)


@pytest.mark.parametrize(
    "config",
    [
        {"target": "state.emails", "source": "state.row.email"},
        {"target": "state.by_email", "source": "state.row", "mode": "set_key", "key": "a.b@x.test"},
        {
            "target": "state.by_email",
            "source": "state.row",
            "mode": "set_key",
            "key": {"source": "state.row.email"},
        },
    ],
)
def test_a_well_formed_collect_config_has_no_config_issue(config):
    workflow = _collect_in_for_each(config)

    validation = validate_workflow_readiness(_WorkflowOnlyDB(workflow), workflow["workflow_id"])

    assert not [
        code for code in _error_codes(validation) if code.startswith("workflow.step_collect")
    ]


def _child_returning(*returned: str) -> dict:
    """A callable child whose steps return the named files."""
    return {
        "workflow_id": "wf_child",
        "enabled": True,
        "settings": {"callable": {"enabled": True, "trigger_channels": ["parent_workflow"]}},
        "steps": [
            {
                "step_id": f"save_{name}",
                "step_kind": "file_from_base64",
                "override": {
                    "file_from_base64": {
                        "source": "state.inputs.body",
                        "result_target": f"state.{name}",
                    },
                    "returned_files": [{"result": "file", "key": name}],
                },
            }
            for name in returned
        ],
    }


def _invoke_mapping_validation(child: dict, artifact_mapping: dict) -> dict:
    return validate_invoke_workflow_step(
        _InvokeValidationDB(workflows={"wf_child": child}),
        workflow_id="wf_parent",
        step={
            "override": {
                "invoke_workflow": {
                    "workflow_id": "wf_child",
                    "input_mapping": {},
                    "artifact_mapping": artifact_mapping,
                }
            }
        },
        path_prefix="workflow.steps.run_child",
    )


def test_a_parent_maps_a_returned_child_file_by_its_key():
    validation = _invoke_mapping_validation(
        _child_returning("report"), {"report": "state.child_report"}
    )

    assert validation["valid"] is True, validation["errors"]
    assert not {
        "invoke.artifact_mapping_unknown_key",
        "invoke.artifact_mapping_unused",
    } & _warning_codes(validation)


def test_a_parent_mapping_of_an_unticked_child_file_is_diagnosed():
    unknown = _invoke_mapping_validation(
        _child_returning("receipt"), {"report": "state.child_report"}
    )
    unused = _invoke_mapping_validation(_child_returning(), {"report": "state.child_report"})

    assert "invoke.artifact_mapping_unknown_key" in _warning_codes(unknown)
    assert "invoke.artifact_mapping_unused" in _warning_codes(unused)


def _template_callable_workflow(workflow_id: str, *, output_type: str = "string") -> dict:
    return linear_workflow(
        {
            "step_id": "render",
            "step_kind": "template",
            "override": {"template": {"text": "Ready", "result_target": "state.outputs.text"}},
        },
        workflow_id=workflow_id,
        settings={
            "callable": {
                "enabled": True,
                "allow_empty_inputs": True,
                "trigger_channels": ["parent_workflow"],
                "expected_inputs": [],
                "expected_outputs": [{"name": "text", "type": output_type, "required": True}],
            }
        },
    )


@pytest.mark.parametrize("workflow_id", ["exported_template", "new_template_identity"])
def test_template_callable_output_validates_and_executes_with_either_identity(workflow_id):
    from core.application.orchestration.template_step_runtime import execute_template_step

    workflow = _template_callable_workflow(workflow_id)
    validation = validate_workflow_readiness(_WorkflowValidationDB(workflow), workflow_id)

    assert validation["valid"] is True, validation["errors"]
    result = execute_template_step(workflow["steps"][0]["override"]["template"], {})
    assert result["ok"] is True
    assert result["text"] == "Ready"


def test_template_callable_output_preserves_wrong_type_failure():
    workflow = _template_callable_workflow("template_wrong_type", output_type="number")
    validation = validate_workflow_readiness(
        _WorkflowValidationDB(workflow), workflow["workflow_id"]
    )
    assert "workflow.callable.output_type_mismatch" in _error_codes(validation)
    assert "workflow.callable.output_not_produced" not in _error_codes(validation)


def test_branch_templates_both_produce_callable_decision():
    workflow = _template_callable_workflow("branch_templates")
    workflow["settings"]["callable"]["expected_outputs"][0]["name"] = "decision"
    workflow["settings"]["callable"]["expected_inputs"] = [{"name": "choose", "type": "boolean"}]
    workflow["steps"] = [
        {
            "step_id": "choose",
            "step_kind": "if",
            "override": {
                "if": {"condition": {"selector": "state.inputs.choose", "operator": "truthy"}}
            },
        },
        *[
            {
                "step_id": name,
                "step_kind": "template",
                "override": {
                    "template": {
                        "text": value,
                        "result_target": "state.outputs.decision",
                    }
                },
            }
            for name, value in [("yes", "accept"), ("no", "reject")]
        ],
        stop_step(),
    ]
    workflow["settings"]["execution_graph"] = {
        "schema_version": 1,
        "entry": node("choose"),
        "edges": [
            edge("choose", node("yes"), "true"),
            edge("choose", node("no"), "false"),
            edge("yes", node("done")),
            edge("no", node("done")),
        ],
    }
    validation = validate_workflow_readiness(
        _WorkflowValidationDB(workflow), workflow["workflow_id"]
    )
    assert validation["valid"] is True, validation["errors"]


@pytest.mark.parametrize("workflow_id", ["exported_template", "new_template_identity"])
def test_template_callable_draft_validation_persistence_and_execution_agree(workflow_id):
    import yaml

    from core.application.orchestration.graph.nodes import execute_step_node
    from core.application.workflows.authoring.service import WorkflowAuthoringService
    from tests._step_run_state import step_run_state
    from tests.unit.test_workflow_authoring_catalog import SCOPE
    from tests.unit.test_workflow_authoring_drafts import DraftDB, bundle, lead_with

    workflow = _template_callable_workflow(workflow_id)
    parsed = yaml.safe_load(bundle())
    candidate = parsed["workflows"][0]
    candidate["workflow_id"] = workflow_id
    candidate["settings"] = {"callable": workflow["settings"]["callable"]}
    lead_with(parsed, [workflow["steps"][0]])
    text = yaml.safe_dump(parsed)
    db = DraftDB()
    authoring = WorkflowAuthoringService(db, creation_authorizer=lambda _scope: None)

    validation = authoring.workflow_draft_validate(
        scope=SCOPE, project_id="project", bundle_yaml=text
    )
    assert validation["can_save"] is True, validation["issues"]
    assert validation["readiness"]["valid"] is True, validation["issues"]
    assert db.workflows == {}

    saved = authoring.workflow_draft_save(scope=SCOPE, project_id="project", bundle_yaml=text)
    assert saved["workflow_id"] == workflow_id
    stored = db.get_workflow(workflow_id)
    assert stored["steps"][0]["override"]["template"]["result_target"] == "state.outputs.text"
    readiness = validate_workflow_readiness(db, workflow_id)
    assert readiness["valid"] is True, readiness["errors"]
    update = execute_step_node(step_run_state(stored["steps"][0]))
    assert update["outputs"]["text"] == "Ready"
    assert update["agent_messages"][-1]["status"] == "pass"
