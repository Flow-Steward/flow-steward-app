"""The builder canvas: a read model of the definition, never a routing source.

Nodes are the workflow's own steps, its run-once Groups and its For each
containers; edges are exactly the ``execution_graph`` edges. Nothing here derives an edge from
``sort_order`` or a step's config, so the canvas shows what a run will do.
"""

from __future__ import annotations

from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.registry.contract_validation_workflow_readiness import (
    validate_workflow_readiness,
)
from core.application.registry.step_skill_resolution import catalog_agent_row
from core.application.registry.workflow_contract_service import WorkflowInterfaceService
from core.application.workflows.canvas_graph_common import (
    _empty_scope_summary,
    _step_config_summary,
    _step_config_summary_metadata,
    _to_object,
)
from core.application.workflows.canvas_graph_summaries import (
    _build_automation_summary_by_scope,
    _build_validation_issues,
    _build_validation_summary_by_scope,
    _list_automation_rules,
)
from core.application.workflows.execution_graph import EXECUTION_GRAPH_SETTING, OUTCOME_ERROR
from core.application.workflows.extension_connection_validation import step_extension_id
from core.application.workflows.workflow_definition_validation import (
    ERROR_POLICY_ROUTE_TO_STEP,
    step_outcomes,
)
from core.application.workflows.workflow_step_kinds import normalize_step_kind

_NODE_TYPES = {
    "if": "ifStep",
    "switch": "switchStep",
    "batch": "batch",
    "approval": "approval",
    "wait": "wait",
    "stop_success": "end",
    "stop_fail": "end",
}

_OUTCOME_LABELS = {
    "success": "Next",
    "true": "True",
    "false": "False",
    "default": "Default",
    "approved": "Approved",
    "rejected": "Rejected",
    "error": "On error",
}


def _canvas_node_type(step_kind: str) -> str:
    return _NODE_TYPES.get(step_kind, "action")


def _switch_case_labels(config: dict[str, Any]) -> dict[str, str]:
    cases = config.get("cases")
    labels: dict[str, str] = {}
    for case in cases if isinstance(cases, list) else []:
        if isinstance(case, dict) and str(case.get("case_id") or "").strip():
            label = case.get("label") if case.get("label") not in (None, "") else case.get("when")
            labels[str(case["case_id"]).strip()] = str(
                label if label is not None else case["case_id"]
            )
    return labels


def _step_ports(
    step_kind: str, override: dict[str, Any], error_policy: dict[str, Any]
) -> list[dict[str, Any]]:
    """One port per outcome the step must route; the error port only under route_to_step."""
    config = _to_object(override.get(step_kind))
    case_labels = _switch_case_labels(config) if step_kind == "switch" else {}
    ports = [
        {
            "outcome": outcome.dump(),
            "label": case_labels.get(outcome.case_id or "", outcome.case_id)
            if outcome.case_id
            else _OUTCOME_LABELS.get(outcome.name, outcome.name),
        }
        for outcome in step_outcomes(step_kind, config)
    ]
    if str(error_policy.get("on_error") or "").strip() == ERROR_POLICY_ROUTE_TO_STEP:
        ports.append({"outcome": {"name": OUTCOME_ERROR}, "label": _OUTCOME_LABELS[OUTCOME_ERROR]})
    return ports


def build_workflow_canvas_graph(db: Any, workflow_id: str) -> dict[str, Any]:
    workflow = db.get_workflow(workflow_id)
    if not isinstance(workflow, dict):
        raise LookupError(f"Workflow '{workflow_id}' not found")

    interface_service = WorkflowInterfaceService()
    interface = interface_service.interface_from_definition(workflow)
    interface_contract = interface_service.public_contract_payload(interface)
    validation = validate_workflow_readiness(db, workflow_id)
    validation_issues = _build_validation_issues(workflow, validation)
    validation_summary_by_scope = _build_validation_summary_by_scope(validation_issues)
    automation_summary_by_scope = _build_automation_summary_by_scope(db, workflow_id)

    steps: list[dict[str, Any]] = []
    for raw_step in workflow.get("steps") or []:
        if not isinstance(raw_step, dict):
            continue
        step_id = str(raw_step.get("step_id") or "").strip()
        if not step_id:
            continue
        step_kind = normalize_step_kind(raw_step.get("step_kind"), preserve_unknown=True)
        override = _to_object(raw_step.get("override"))
        if step_kind == "agent":
            agent = (
                catalog_agent_row(
                    db,
                    str(raw_step.get("agent_id") or ""),
                    account_id=str(workflow.get("account_id") or DEFAULT_ACCOUNT_ID),
                )
                or {}
            )
            raw_step = {
                **raw_step,
                "agent_display_name": str(agent.get("display_name") or "").strip(),
            }
        steps.append(
            {
                "id": step_id,
                "step_id": step_id,
                "node_type": _canvas_node_type(step_kind),
                "kind": step_kind,
                "group_id": str(raw_step.get("group_id") or "").strip() or None,
                "for_each_id": str(raw_step.get("for_each_id") or "").strip() or None,
                "lane_id": str(raw_step.get("lane_id") or "").strip() or None,
                "display_name": str(raw_step.get("display_name") or step_id).strip() or step_id,
                "config_summary": _step_config_summary(raw_step, workflow_id=workflow_id),
                "ports": _step_ports(step_kind, override, _to_object(raw_step.get("error_policy"))),
                **_step_config_summary_metadata(raw_step, workflow_id=workflow_id),
                "optional": bool(raw_step.get("optional", False)),
                "disabled": bool(raw_step.get("disabled", False)),
                # Which extension a connector step comes from, so the canvas can
                # show that extension's logo on the node.
                "extension_id": step_extension_id(raw_step) or None,
                "automation_summary": automation_summary_by_scope.get(
                    ("step", step_id), _empty_scope_summary()
                ),
                "validation_summary": validation_summary_by_scope.get(
                    ("step", step_id), _empty_scope_summary()
                ),
            }
        )

    groups: list[dict[str, Any]] = []
    for raw_group in workflow.get("groups") or []:
        if not isinstance(raw_group, dict):
            continue
        group_id = str(raw_group.get("group_id") or "").strip()
        if not group_id:
            continue
        groups.append(
            {
                "id": group_id,
                "group_id": group_id,
                "display_name": str(raw_group.get("display_name") or group_id).strip() or group_id,
                "body": _to_object(raw_group.get("body")),
                "error_policy": _to_object(raw_group.get("error_policy")),
                "automation_summary": automation_summary_by_scope.get(
                    ("group", group_id), _empty_scope_summary()
                ),
                "validation_summary": validation_summary_by_scope.get(
                    ("group", group_id), _empty_scope_summary()
                ),
            }
        )

    for_each: list[dict[str, Any]] = []
    for raw_item in workflow.get("for_each") or []:
        if not isinstance(raw_item, dict):
            continue
        for_each_id = str(raw_item.get("for_each_id") or "").strip()
        if not for_each_id:
            continue
        for_each.append(
            {
                "id": for_each_id,
                "for_each_id": for_each_id,
                "group_id": str(raw_item.get("group_id") or "").strip() or None,
                "display_name": str(raw_item.get("display_name") or for_each_id).strip()
                or for_each_id,
                "over": str(raw_item.get("over") or "").strip(),
                "item_name": str(raw_item.get("item_name") or "").strip(),
                "index_name": str(raw_item.get("index_name") or "").strip() or None,
                "body": _to_object(raw_item.get("body")),
                "error_policy": _to_object(raw_item.get("error_policy")),
                "automation_summary": automation_summary_by_scope.get(
                    ("for_each", for_each_id), _empty_scope_summary()
                ),
                "validation_summary": validation_summary_by_scope.get(
                    ("for_each", for_each_id), _empty_scope_summary()
                ),
            }
        )

    graph = _to_object(_to_object(workflow.get("settings")).get(EXECUTION_GRAPH_SETTING))
    raw_edges = graph.get("edges")
    edges = (
        [dict(edge) for edge in raw_edges if isinstance(edge, dict)]
        if isinstance(raw_edges, list)
        else []
    )

    output_contract = {
        "output_mode": interface_contract.get("output_mode") or "none",
        "expected_outputs": interface_contract.get("expected_outputs") or [],
        "returned_files": interface_contract.get("returned_files") or [],
    }
    interface_payload = {
        "trigger_channels": interface_contract.get("trigger_channels") or [],
        "input_mode": interface_contract.get("input_mode") or "fields",
        "expected_inputs": interface_contract.get("expected_inputs") or [],
        "input_schema": interface_contract.get("input_schema") or {},
        "strict_inputs": bool(interface_contract.get("strict_inputs", True)),
    }

    return {
        "workflow": {
            "id": str(workflow.get("workflow_id") or workflow_id).strip() or workflow_id,
            "display_name": str(workflow.get("display_name") or workflow_id).strip() or workflow_id,
            "enabled": bool(workflow.get("enabled", False)),
            "revision": workflow.get("revision"),
            "callable_by_parent": bool(interface.enabled)
            and "parent_workflow" in set(interface.trigger_channels),
        },
        "interface_contract": interface_payload,
        "output_contract": output_contract,
        "entry": graph.get("entry"),
        "groups": groups,
        "for_each": for_each,
        "steps": steps,
        "edges": edges,
        "validation_issues": validation_issues,
        "automation_rules": _list_automation_rules(db, workflow_id),
    }


__all__ = ["build_workflow_canvas_graph"]
