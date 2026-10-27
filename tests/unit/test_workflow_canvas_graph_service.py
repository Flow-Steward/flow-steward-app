from __future__ import annotations

from unittest.mock import patch

import pytest

from core.application.registry.contract_validation_workflow_readiness import _workflow_issue_path
from core.application.workflows.canvas_graph_service import build_workflow_canvas_graph
from core.application.workflows.workflow_definition_validation import validate_workflow_definition
from tests._workflow_definitions import edge, linear_workflow, node


class _Store:
    def __init__(self, workflow: dict) -> None:
        self.workflow = workflow

    def get_workflow(self, workflow_id: str) -> dict | None:
        return self.workflow if workflow_id == self.workflow["workflow_id"] else None


def _graph(workflow: dict, validation: dict | None = None) -> dict:
    with patch(
        "core.application.workflows.canvas_graph_service.validate_workflow_readiness",
        return_value=validation or {"valid": True, "errors": [], "warnings": []},
    ):
        return build_workflow_canvas_graph(_Store(workflow), workflow["workflow_id"])


def _switch_workflow() -> dict:
    workflow = linear_workflow(
        {
            "step_id": "route",
            "step_kind": "switch",
            "override": {
                "switch": {
                    "selector": "state.channel",
                    "cases": [
                        {"case_id": "c_email", "when": "email", "label": "Email"},
                        {"case_id": "c_blog", "when": "blog"},
                    ],
                }
            },
        },
        {"step_id": "email", "step_kind": "transform"},
        with_stop=True,
    )
    workflow["settings"]["execution_graph"] = {
        "schema_version": 1,
        "entry": node("route"),
        "edges": [
            edge("route", node("email"), "case", case_id="c_email"),
            edge("route", node("done"), "case", case_id="c_blog"),
            edge("route", node("done"), "default"),
            edge("email", node("done")),
        ],
    }
    return workflow


def test_edges_are_exactly_the_execution_graph() -> None:
    workflow = _switch_workflow()
    graph = _graph(workflow)
    assert graph["edges"] == workflow["settings"]["execution_graph"]["edges"]
    assert graph["entry"] == node("route")
    assert "phases" not in graph


def test_switch_ports_follow_case_ids_and_labels() -> None:
    route = next(step for step in _graph(_switch_workflow())["steps"] if step["id"] == "route")
    assert route["node_type"] == "switchStep"
    assert route["ports"] == [
        {"outcome": {"name": "case", "case_id": "c_email"}, "label": "Email"},
        {"outcome": {"name": "case", "case_id": "c_blog"}, "label": "blog"},
        {"outcome": {"name": "default"}, "label": "Default"},
    ]


@pytest.mark.parametrize(
    ("selector", "summary", "summary_key"),
    [
        ("state.channel", "state.channel (2 cases)", "react.workflows.switch_selector_summary"),
        ("", "2 switch cases", "react.workflows.switch_cases_summary"),
    ],
)
def test_switch_list_cases_summary_matches_localized_count(
    selector: str, summary: str, summary_key: str
) -> None:
    workflow = _switch_workflow()
    config = workflow["steps"][0]["override"]["switch"]
    if selector:
        config["selector"] = selector
    else:
        config.pop("selector")

    route = next(step for step in _graph(workflow)["steps"] if step["id"] == "route")

    assert (
        route["config_summary"],
        route["config_summary_key"],
        route["config_summary_params"],
    ) == (summary, summary_key, {"selector": selector, "case_count": 2})


@pytest.mark.parametrize("selector", ["state.channel", ""])
@pytest.mark.parametrize(
    ("cases_config", "expected_count"),
    [
        pytest.param({"cases": {"email": "Email", "blog": "Blog"}}, 2, id="legacy-dict"),
        pytest.param({"cases": []}, 0, id="empty-list"),
        pytest.param({"cases": {}}, 0, id="empty-dict"),
        pytest.param({}, 0, id="absent"),
        pytest.param({"cases": None}, 0, id="null"),
        pytest.param({"cases": "email"}, 0, id="invalid-string"),
        pytest.param({"cases": 2}, 0, id="invalid-number"),
    ],
)
def test_switch_summary_preserves_legacy_dict_and_empty_case_counts(
    selector: str, cases_config: dict, expected_count: int
) -> None:
    workflow = _switch_workflow()
    workflow["steps"][0]["override"]["switch"] = {"selector": selector, **cases_config}

    route = next(step for step in _graph(workflow)["steps"] if step["id"] == "route")

    assert route["config_summary_params"] == {"selector": selector, "case_count": expected_count}
    assert route["config_summary"] == (
        f"{selector} ({expected_count} cases)" if selector else f"{expected_count} switch cases"
    )
    assert route["config_summary_key"] == (
        "react.workflows.switch_selector_summary"
        if selector
        else "react.workflows.switch_cases_summary"
    )


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        pytest.param(
            {"selector": "state.line.qty", "operator": "lte", "value": 0},
            "state.line.qty ≤ 0",
            id="selector-operator-zero",
        ),
        pytest.param(
            {"selector": "state.a", "operator": "equals", "value": "state.b"},
            "state.a = state.b",
            id="selector-right-state-path",
        ),
        pytest.param(
            {"selector": "state.a", "operator": "equals", "value": "state"},
            "state.a = state",
            id="selector-right-state-root",
        ),
        pytest.param(
            {"equals": {"left": "state.a", "right": "state.b"}},
            "state.a = state.b",
            id="operator-key-right-state-path",
        ),
        pytest.param(
            {"equals": {"left": "state.a", "right": "state"}},
            "state.a = state",
            id="operator-key-right-state-root",
        ),
        pytest.param(
            {"selector": "state.value", "operator": "equals", "value": False},
            "state.value = false",
            id="false",
        ),
        pytest.param(
            {"selector": "state.value", "operator": "not_equals", "value": None},
            "state.value ≠ null",
            id="null",
        ),
        pytest.param(
            {"selector": "state.value", "operator": "gt", "value": 1}, "state.value > 1", id="gt"
        ),
        pytest.param(
            {"selector": "state.value", "operator": "gte", "value": 1}, "state.value ≥ 1", id="gte"
        ),
        pytest.param(
            {"selector": "state.value", "operator": "lt", "value": 1}, "state.value < 1", id="lt"
        ),
        pytest.param(
            {"selector": "state.text", "operator": "contains", "value": "x"},
            'state.text contains "x"',
            id="contains",
        ),
        pytest.param(
            {"selector": "state.text", "operator": "in", "value": ["x"]},
            'state.text in ["x"]',
            id="in",
        ),
        pytest.param(
            {"selector": "state.text", "operator": "not_in", "value": ["x"]},
            'state.text not in ["x"]',
            id="not-in",
        ),
        pytest.param(
            {"selector": "state.ready", "operator": "exists"}, "state.ready exists", id="exists"
        ),
        pytest.param(
            {"selector": "state.ready", "operator": "truthy"},
            "state.ready truthy",
            id="truthy-selector",
        ),
        pytest.param(
            {"selector": "state.ready", "operator": "always_true"},
            "always true",
            id="constant-true-selector-form",
        ),
        pytest.param(
            {"selector": "state.text", "operator": "equals", "value": ""},
            'state.text = ""',
            id="empty-string",
        ),
        pytest.param(
            {"selector": "state.text", "operator": "equals", "value": 'a"\\b\n'},
            r'state.text = "a\"\\b\n"',
            id="escaped-string",
        ),
        pytest.param(
            {"lte": {"left": "state.line.qty", "right": 0}}, "state.line.qty ≤ 0", id="operator-key"
        ),
        pytest.param({"truthy": "state.ready"}, "state.ready truthy", id="truthy"),
        pytest.param({"operator": "always_false"}, "always false", id="constant-false"),
        pytest.param(
            {"selector": "state.x", "operator": "lte", "value": 0, "nested": True},
            "conditional branch",
            id="unsupported-extra",
        ),
        pytest.param(
            {"selector": "state.x", "operator": "eq", "value": 0},
            "conditional branch",
            id="unsupported-alias",
        ),
        pytest.param(
            {"selector": "state.x", "operator": "constructor", "value": 0},
            "conditional branch",
            id="inherited-constructor",
        ),
        pytest.param(
            {"toString": {"left": "state.x", "right": 0}},
            "conditional branch",
            id="inherited-to-string",
        ),
        pytest.param(
            {"selector": "state.x", "operator": "lte"}, "conditional branch", id="missing-value"
        ),
        pytest.param(
            {"selector": "state.x", "operator": "always_true", "value": 0},
            "conditional branch",
            id="constant-extra",
        ),
        pytest.param(
            {"lte": {"left": "state.x", "right": 0, "extra": True}},
            "conditional branch",
            id="alternate-extra",
        ),
        pytest.param(
            {"and": [{"truthy": "state.x"}]}, "conditional branch", id="nested-expression"
        ),
        pytest.param(
            {"always_false": None},
            "conditional branch",
            id="alternate-constant-unsupported-runtime",
        ),
        pytest.param(
            {"selector": 0, "operator": "always_false"},
            "conditional branch",
            id="malformed-constant-selector",
        ),
        pytest.param(False, "conditional branch", id="unsupported-scalar"),
    ],
)
def test_if_condition_summary_formats_only_supported_shapes(
    condition: object, expected: str
) -> None:
    workflow = linear_workflow(
        {"step_id": "route", "step_kind": "if", "override": {"if": {"condition": condition}}},
        with_stop=True,
    )
    route = next(step for step in _graph(workflow)["steps"] if step["id"] == "route")
    assert route["config_summary"] == expected
    assert route.get("config_summary_key") == (
        "react.workflows.if_default_summary" if expected == "conditional branch" else None
    )


def test_if_condition_summary_preserves_legacy_strings() -> None:
    workflow = linear_workflow(
        {
            "step_id": "route",
            "step_kind": "if",
            "override": {"if": {"condition": "state.ready == true"}},
        },
        with_stop=True,
    )
    route = next(step for step in _graph(workflow)["steps"] if step["id"] == "route")
    assert route["config_summary"] == "state.ready == true"


def test_if_condition_summary_preserves_legacy_selector_fallback() -> None:
    workflow = linear_workflow(
        {"step_id": "route", "step_kind": "if", "override": {"if": {"selector": "state.ready"}}},
        with_stop=True,
    )
    route = next(step for step in _graph(workflow)["steps"] if step["id"] == "route")
    assert route["config_summary"] == "state.ready"


def test_ports_of_approval_wait_stop_and_error_policy() -> None:
    workflow = linear_workflow(
        {"step_id": "review", "step_kind": "approval"},
        {"step_id": "pause", "step_kind": "wait", "override": {"wait": {"duration_ms": 1}}},
        {
            "step_id": "call",
            "step_kind": "http_request",
            "error_policy": {"on_error": "route_to_step"},
        },
    )
    steps = {step["id"]: step for step in _graph(workflow)["steps"]}
    assert [port["outcome"]["name"] for port in steps["review"]["ports"]] == [
        "approved",
        "rejected",
    ]
    assert [port["outcome"]["name"] for port in steps["pause"]["ports"]] == ["success"]
    assert [port["outcome"]["name"] for port in steps["call"]["ports"]] == ["success", "error"]
    assert steps["done"]["ports"] == []
    assert steps["done"]["node_type"] == "end"


def test_steps_expose_their_disabled_flag() -> None:
    workflow = linear_workflow(
        {"step_id": "skipped", "step_kind": "transform", "disabled": True},
        {"step_id": "runs", "step_kind": "transform"},
    )
    steps = {step["id"]: step for step in _graph(workflow)["steps"]}
    assert steps["skipped"]["disabled"] is True
    assert steps["runs"]["disabled"] is False


def test_groups_and_membership_are_real_groups_only() -> None:
    workflow = linear_workflow({"step_id": "a"})
    workflow["groups"] = [
        {
            "group_id": "items",
            "display_name": "Each item",
            "body": {"kind": "sequence", "entry": node("l1")},
        }
    ]
    workflow["steps"].append({"step_id": "l1", "step_kind": "transform", "group_id": "items"})
    graph = _graph(workflow)
    assert [group["id"] for group in graph["groups"]] == ["items"]
    steps = {step["id"]: step for step in graph["steps"]}
    assert steps["l1"]["group_id"] == "items"
    assert steps["a"]["group_id"] is None


def test_validation_issues_attach_to_step_and_group_scopes() -> None:
    workflow = linear_workflow({"step_id": "a"})
    validation = {
        "valid": False,
        "errors": [
            {"code": "workflow.graph.outcome_missing", "message": "x", "path": "workflow.steps.a"},
            {
                "code": "workflow.group.lanes_missing",
                "message": "y",
                "path": "workflow.groups.g.body",
            },
            {"code": "workflow.graph.missing", "message": "z", "path": "workflow.execution_graph"},
        ],
        "warnings": [],
    }
    graph = _graph(workflow, validation)
    scopes = {(issue["scope"], issue["scope_id"]) for issue in graph["validation_issues"]}
    assert scopes == {("step", "a"), ("group", "g"), ("workflow", "workflow")}
    step_a = next(step for step in graph["steps"] if step["id"] == "a")
    assert step_a["validation_summary"]["error_count"] == 1


def test_missing_switch_case_keeps_structured_outcome_and_exact_canvas_correlation() -> None:
    workflow = _switch_workflow()
    graph = workflow["settings"]["execution_graph"]
    graph["edges"] = [
        edge for edge in graph["edges"] if edge["outcome"] != {"name": "case", "case_id": "c_email"}
    ]
    definition = {
        "steps": workflow["steps"],
        "groups": workflow.get("groups", []),
        "for_each": workflow.get("for_each", []),
        "execution_graph": graph,
    }
    issue = next(
        item
        for item in validate_workflow_definition(definition)
        if item["code"] == "workflow.graph.outcome_missing" and "c_email" in item["message"]
    )
    assert (issue["outcome_name"], issue["outcome_case_id"], issue["path"]) == (
        "case",
        "c_email",
        "steps.route",
    )
    # Readiness prefixes definition paths before the canvas sees them.
    readiness_issue = {**issue, "path": _workflow_issue_path(issue["path"])}
    assert readiness_issue["path"] == "workflow.steps.route"
    canvas = _graph(workflow, {"valid": False, "errors": [readiness_issue], "warnings": []})
    shown = next(
        item
        for item in canvas["validation_issues"]
        if item["code"] == "workflow.graph.outcome_missing"
    )
    assert (shown["scope"], shown["scope_id"], shown["severity"], shown["field_path"]) == (
        "step",
        "route",
        "error",
        readiness_issue["path"],
    )
    assert (shown["issues"][0]["outcome_name"], shown["issues"][0]["outcome_case_id"]) == (
        "case",
        "c_email",
    )
    assert shown["suggested_action_key"] == "react.workflows.diagnostic_action_missing_outcome"


def test_exact_http_url_and_failure_intent_actions_keep_machine_correlation() -> None:
    workflow = linear_workflow({"step_id": "request", "step_kind": "http_request"})
    paths = {
        "workflow.step_http_request_url_missing": "workflow.steps.request.override.http_request.url",
        "workflow.terminal_fail_intent_missing": "workflow.settings.traits.expected_failure",
    }
    validation = {
        "valid": False,
        "errors": [
            {"code": code, "message": "raw producer message", "path": path, "severity": "error"}
            for code, path in paths.items()
        ],
        "warnings": [],
    }
    issues = {item["code"]: item for item in _graph(workflow, validation)["validation_issues"]}
    assert {code: (issues[code]["field_path"], issues[code]["severity"]) for code in paths} == {
        code: (path, "error") for code, path in paths.items()
    }
    assert issues["workflow.step_http_request_url_missing"]["suggested_action_key"] == (
        "react.workflows.diagnostic_action_http_url"
    )
    assert issues["workflow.terminal_fail_intent_missing"]["suggested_action_key"] == (
        "react.workflows.diagnostic_action_expected_failure"
    )


def test_agent_canvas_uses_friendly_catalog_label_and_truthful_missing_fallback():
    from core.application.auth.config import DEFAULT_ACCOUNT_ID

    workflow = linear_workflow(
        {"step_id": "write", "step_kind": "agent", "agent_id": "writer"},
        account_id=DEFAULT_ACCOUNT_ID,
    )
    store = _Store(workflow)
    store.get_agent = lambda agent_id: {"agent_id": agent_id, "display_name": "Report writer"}
    with patch(
        "core.application.workflows.canvas_graph_service.validate_workflow_readiness",
        return_value={"valid": True, "errors": [], "warnings": []},
    ):
        step = build_workflow_canvas_graph(store, workflow["workflow_id"])["steps"][0]
        assert step["config_summary"] == "agent: Report writer"
        assert step["config_summary_params"] == {"label": "Report writer"}
        store.get_agent = lambda _agent_id: None
        missing = build_workflow_canvas_graph(store, workflow["workflow_id"])["steps"][0]
        assert missing["config_summary"] == "agent: writer"
