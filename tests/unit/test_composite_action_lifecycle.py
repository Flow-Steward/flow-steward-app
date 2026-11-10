"""A composite action over time: releases, dependencies, export and import (ADR 0065)."""

from __future__ import annotations

import pytest

from core.application.workflows.canvas_graph_common import _step_config_summary
from core.application.workflows.extension_connection_validation import step_extension_id
from core.application.workflows.workflow_action_contract import action_invoke_dependencies
from core.application.workflows.workflow_action_release import (
    RELEASE_ACTION_CODE,
    ActionReleaseBlockedError,
    check_release_actions,
    release_action_issues,
)
from core.application.workflows.workflow_portability_common import _step_child_workflow_ref
from core.application.workflows.workflow_portability_rewrite import WorkflowPortabilityRewriteMixin

BASE = "acme.amazon"
ADDON = "acme.amazon-pro"
IMPLEMENTATION_ID = "extension.pi_addon.create_full"


def action_config(**changes) -> dict:
    raw = {
        "workflow_id": IMPLEMENTATION_ID,
        "action": {
            "extension_id": ADDON,
            "install_id": "pi_addon",
            "action_id": "orders.create_full",
        },
        "installs": {BASE: "pi_base"},
        "connections": {"amazon": {"connection_id": "conn-1"}},
        "input_mapping": {"order": "state.order"},
        "output_mapping": {"order_id": "state.order_id"},
    }
    raw.update(changes)
    return raw


def action_step(**changes) -> dict:
    return {
        "step_id": "create",
        "step_kind": "invoke_workflow",
        "override": {"invoke_workflow": action_config(**changes)},
    }


def implementation(
    *, inputs=("order",), outputs=("order_id",), slots=("amazon",), steps=()
) -> dict:
    return {
        "workflow_id": "create_full",
        "settings": {
            "callable": {
                "enabled": True,
                "trigger_channels": ["parent_workflow"],
                "input_mode": "fields",
                "expected_inputs": [
                    {"name": name, "type": "string", "required": True} for name in inputs
                ],
                "expected_outputs": [{"name": name, "type": "string"} for name in outputs],
            },
            "action_implementation": {
                "action_id": "orders.create_full",
                "connections": [
                    {"slot": slot, "extension_id": BASE, "connection_type_ids": []}
                    for slot in slots
                ],
            },
        },
        "steps": list(steps),
    }


class Cursor:
    """Answers the three reads of the release guard by the table each one names."""

    def __init__(self, *, targets: list[str], callers: list[tuple[str, str, bool]]) -> None:
        self.targets = targets
        self.callers = callers
        self.rows: list[tuple] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        if "SELECT DISTINCT dependency_ref" in sql:
            assert params == ("acct", "extension.pi\\_addon.%")
            self.rows = [(target,) for target in self.targets]
        elif "registry_workflow_hook_bindings" in sql:
            self.rows = []
        else:
            self.rows = list(self.callers)

    def fetchall(self) -> list[tuple]:
        return self.rows


class Registry:
    def __init__(self, workflows: dict[str, dict]) -> None:
        self.workflows = workflows

    def get_workflow(self, workflow_id: str) -> dict | None:
        return self.workflows.get(workflow_id)


def release_issues(*, step: dict | None = None, enabled: bool = True, workflow=None, handlers=None):
    return release_action_issues(
        Registry({"orders": {"workflow_id": "orders", "steps": [step or action_step()]}}),
        Cursor(targets=[IMPLEMENTATION_ID], callers=[("orders", "create", enabled)]),
        account_id="acct",
        install_id="pi_addon",
        extension_id=ADDON,
        workflows={"create_full": workflow or implementation()},
        action_handlers=({"orders.create_full": "create_full"} if handlers is None else handlers),
        required_dependencies=frozenset({BASE}),
    )


def test_a_compatible_release_changes_nothing_for_the_caller() -> None:
    assert release_issues() == ([], [])


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"handlers": {}}, "no longer offers action 'orders.create_full'"),
        ({"handlers": {"orders.create_full": "other"}}, "no longer offers action"),
        (
            {"workflow": implementation(inputs=("order", "priority"))},
            "requires input 'priority', which is not mapped",
        ),
        ({"workflow": implementation(inputs=())}, "no longer declares input 'order'"),
        ({"workflow": implementation(outputs=())}, "no longer declares output 'order_id'"),
        (
            {"workflow": implementation(slots=("amazon", "books"))},
            "adds connection 'books', which the step has not chosen",
        ),
        ({"step": action_step(installs={})}, f"needs extension '{BASE}'"),
    ],
)
def test_a_release_that_breaks_an_enabled_caller_is_blocked(kwargs, reason) -> None:
    blockers, diagnostics = release_issues(**kwargs)

    assert diagnostics == []
    assert [issue["code"] for issue in blockers] == [RELEASE_ACTION_CODE]
    assert reason in blockers[0]["message"]
    assert blockers[0]["message"].startswith("step 'create' of 'orders':")
    assert blockers[0]["consumer"]["caller_workflow_id"] == "orders"


def test_a_wait_may_not_appear_under_a_caller_in_a_parallel_lane() -> None:
    waiting = implementation(steps=[{"step_id": "confirm", "step_kind": "approval"}])
    lane_step = {**action_step(), "lane_id": "lane-1"}

    blockers, _ = release_issues(step=lane_step, workflow=waiting)

    assert "may not wait" in blockers[0]["message"]
    assert release_issues(workflow=waiting) == ([], [])


def test_a_disabled_caller_only_gets_a_diagnostic() -> None:
    blockers, diagnostics = release_issues(enabled=False, handlers={})

    assert blockers == []
    assert [issue["code"] for issue in diagnostics] == [RELEASE_ACTION_CODE]


def test_a_plain_invoke_of_an_extension_workflow_is_not_judged_here() -> None:
    plain = {
        "step_id": "create",
        "step_kind": "invoke_workflow",
        "override": {"invoke_workflow": {"workflow_id": IMPLEMENTATION_ID}},
    }

    assert release_issues(step=plain, handlers={}) == ([], [])


def test_a_blocking_release_is_refused_with_its_callers() -> None:
    registry = Registry({"orders": {"workflow_id": "orders", "steps": [action_step()]}})
    kwargs = {
        "account_id": "acct",
        "install_id": "pi_addon",
        "extension_id": ADDON,
        "workflows": {"create_full": implementation()},
        "required_dependencies": frozenset({BASE}),
    }

    def cursor() -> Cursor:
        return Cursor(targets=[IMPLEMENTATION_ID], callers=[("orders", "create", True)])

    assert (
        check_release_actions(
            registry, cursor(), action_handlers={"orders.create_full": "create_full"}, **kwargs
        )
        == []
    )
    with pytest.raises(ActionReleaseBlockedError) as caught:
        check_release_actions(registry, cursor(), action_handlers={}, **kwargs)
    assert len(caught.value.issues) == 1
    assert "step 'create' of 'orders'" in str(caught.value)


def test_an_action_step_records_the_dependencies_of_a_connector_step() -> None:
    assert action_invoke_dependencies(action_config()) == [
        ("extension_install", "pi_addon", "pi_addon"),
        ("extension_action", "orders.create_full", "pi_addon"),
        ("extension_install", "pi_base", "pi_base"),
        ("project_connection", "conn-1", ""),
    ]
    assert action_invoke_dependencies({"workflow_id": "report"}) == []
    assert action_invoke_dependencies({"action": "broken"}) == []
    assert action_invoke_dependencies(None) == []


def test_the_implementation_is_never_a_portable_child() -> None:
    plain = {"step_kind": "invoke_workflow", "override": {"invoke_workflow": {"workflow_id": "x"}}}

    assert _step_child_workflow_ref(action_step()) == ""
    assert _step_child_workflow_ref(plain) == "x"


def test_import_points_an_action_step_at_this_accounts_installs_and_connections() -> None:
    def mapped(target: str) -> dict:
        return {"action": "map_existing", "target_ref": target}

    resolutions = {
        ("extension", "pi_addon"): mapped("pi_new_addon"),
        ("extension", "pi_base"): mapped("pi_new_base"),
        ("project_connection", "conn-1"): mapped("conn-9"),
    }

    rewritten = WorkflowPortabilityRewriteMixin()._rewrite_step(action_step(), resolutions)
    config = rewritten["override"]["invoke_workflow"]

    assert config["action"] == {
        "extension_id": ADDON,
        "install_id": "pi_new_addon",
        "action_id": "orders.create_full",
    }
    assert config["workflow_id"] == "extension.pi_new_addon.create_full"
    assert config["installs"] == {BASE: "pi_new_base"}
    assert config["connections"] == {"amazon": {"connection_id": "conn-9"}}
    assert config["input_mapping"] == {"order": "state.order"}


def test_an_action_step_reads_as_its_extension_on_the_canvas() -> None:
    assert step_extension_id(action_step()) == ADDON
    assert step_extension_id({"step_kind": "invoke_workflow", "override": {}}) == ""
    assert (
        _step_config_summary(action_step(), workflow_id="orders")
        == f"extension.{ADDON} -> orders.create_full"
    )
