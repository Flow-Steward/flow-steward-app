"""Bundle rules of a composite action: the action, its workflow and the workflow's body."""

from __future__ import annotations

import copy

import pytest

from core.application.extensions.composite_actions import (
    composite_action_descriptor_issues,
    composite_action_handlers,
    is_composite_action,
    release_composite_actions,
    workflow_handler_issues,
)
from core.application.registry.projected_artifact_rows import _projected_workflow_steps
from core.infrastructure.extension_runtime.extension_runtime_common import (
    ALLOWED_ACTION_HANDLER_MODES,
    ALLOWED_OPERATION_HANDLER_MODES,
)
from core.infrastructure.extension_runtime.extension_runtime_invocation import (
    COMPOSITE_ACTION_DIRECT_CALL_CODE,
    composite_action_direct_call_refusal,
)

BASE = "acme.amazon"
ADDON = "acme.amazon-pro"


def connector_step(step_id: str, operation: str, *, extension: str = BASE, **fields) -> dict:
    return {
        "step_id": step_id,
        "step_kind": "connector",
        "connector_id": f"extension.{extension}",
        "connection_binding_jsonb": {"slot": "amazon"},
        "override": {"connector": {"extension_id": extension, "operation_id": operation}},
        **fields,
    }


def implementation(**settings) -> dict:
    return {
        "workflow_id": "create_full",
        "display_name": "Create order with shipment",
        "settings": {
            "callable": {"enabled": True, "trigger_channels": ["parent_workflow"]},
            "action_implementation": {
                "action_id": "orders.create_full",
                "connections": [
                    {
                        "slot": "amazon",
                        "extension_id": BASE,
                        "connection_type_ids": ["seller_account"],
                    }
                ],
            },
            **settings,
        },
        "steps": [
            connector_step("create", "orders.create"),
            connector_step("ship", "shipments.create"),
        ],
    }


def descriptor(*workflows: dict, actions: list[dict] | None = None) -> dict:
    return {
        "extension_id": ADDON,
        "dependencies": {"extensions": [{"extension_id": BASE, "required": True}]},
        "action_definitions": actions
        if actions is not None
        else [
            {
                "action_id": "orders.create_full",
                "workflow_visible": True,
                "handler": {"mode": "workflow", "workflow_id": "create_full"},
            },
            {"action_id": "orders.note", "workflow_visible": True, "handler": {}},
        ],
        "artifacts": {
            "workflows": [
                {"workflow_id": row["workflow_id"], "publish": True, "snapshot": row}
                for row in (workflows or (implementation(),))
            ]
        },
    }


def codes(issues: list[dict]) -> list[str]:
    return [issue["code"] for issue in issues]


def with_step(step: dict) -> dict:
    workflow = implementation()
    workflow["steps"] = [step]
    return workflow


def test_workflow_is_an_action_handler_mode_of_its_own() -> None:
    assert "workflow" in ALLOWED_ACTION_HANDLER_MODES
    # A query is never implemented by a workflow.
    assert "workflow" not in ALLOWED_OPERATION_HANDLER_MODES


def test_a_complete_pair_validates_and_resolves() -> None:
    release = descriptor()

    assert composite_action_descriptor_issues(release) == []
    assert composite_action_handlers(release["action_definitions"]) == {
        "orders.create_full": "create_full"
    }
    actions = release_composite_actions(release)
    assert list(actions) == ["orders.create_full"]
    action = actions["orders.create_full"]
    assert action.workflow_id("pi_addon") == "extension.pi_addon.create_full"
    assert action.implementation.slot("amazon").extension_id == BASE
    assert is_composite_action(release, "orders.create_full")
    assert not is_composite_action(release, "orders.note")
    assert is_composite_action({"compiled_manifest": release}, "orders.create_full")


def test_a_descriptor_without_composite_actions_has_no_issues() -> None:
    plain = {"workflow_id": "report", "steps": [], "settings": {}}

    assert composite_action_descriptor_issues(descriptor(plain, actions=[])) == []
    assert release_composite_actions(descriptor(plain, actions=[])) == {}
    assert release_composite_actions(None) == {}


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (
            {"action_id": "a", "workflow_visible": True, "handler": {"mode": "workflow"}},
            ["composite_action_workflow_missing"],
        ),
        (
            {
                "action_id": "a",
                "workflow_visible": False,
                "handler": {"mode": "workflow", "workflow_id": "w"},
            },
            ["composite_action_not_workflow_visible"],
        ),
        (
            {
                "action_id": "a",
                "workflow_visible": True,
                "handler": {"mode": "workflow", "workflow_id": "w", "config": {"x": 1}},
            },
            ["composite_action_handler_config"],
        ),
        (
            {
                "action_id": "a",
                "workflow_visible": True,
                "handler": {"mode": "workflow", "workflow_id": "w"},
            },
            [],
        ),
    ],
)
def test_manifest_rules_of_a_workflow_handler(action, expected) -> None:
    issues = workflow_handler_issues(action, manifest_path="extension.yaml", index=2)

    assert codes(issues) == expected
    assert all(issue["field_path"].startswith("action_definitions[2].") for issue in issues)


def test_the_action_and_the_workflow_must_name_each_other() -> None:
    unmarked = implementation()
    del unmarked["settings"]["action_implementation"]
    renamed = implementation()
    renamed["settings"]["action_implementation"]["action_id"] = "orders.other"

    assert codes(composite_action_descriptor_issues(descriptor(unmarked))) == [
        "composite_action_marker_missing"
    ]
    assert codes(
        composite_action_descriptor_issues(descriptor({"workflow_id": "other", "steps": []}))
    ) == ["composite_action_workflow_unknown"]
    assert codes(composite_action_descriptor_issues(descriptor(renamed))) == [
        "composite_action_marker_mismatch",
        "composite_action_marker_missing",
    ]
    assert release_composite_actions(descriptor(renamed)) == {}


def test_a_broken_marker_is_reported_with_its_workflow() -> None:
    broken = implementation()
    broken["settings"]["action_implementation"]["connections"] = [{"slot": "Amazon"}]

    issues = composite_action_descriptor_issues(descriptor(broken))

    assert "workflow.action.slot_name_invalid" in codes(issues)
    assert issues[0]["path"].startswith("artifacts.workflows.create_full.settings.")


@pytest.mark.parametrize(
    "callable_settings",
    [
        {"enabled": True, "trigger_channels": ["parent_workflow", "api"]},
        {"enabled": True, "trigger_channels": []},
        {"enabled": False, "trigger_channels": ["parent_workflow"]},
        {},
    ],
)
def test_an_implementation_allows_only_the_parent_workflow_channel(callable_settings) -> None:
    issues = composite_action_descriptor_issues(
        descriptor(implementation(callable=callable_settings))
    )

    assert codes(issues) == ["workflow.action.channel_invalid"]


def test_a_hook_inside_an_implementation_is_forbidden() -> None:
    hook = {
        "step_id": "before",
        "step_kind": "invoke_workflow",
        "override": {"invoke_workflow": {"hook": {"hook_id": "before"}}},
    }

    issues = composite_action_descriptor_issues(descriptor(with_step(hook)))

    assert codes(issues) == ["workflow.action.hook_forbidden"]
    assert issues[0]["path"] == (
        "artifacts.workflows.create_full.steps.before.override.invoke_workflow.hook"
    )


def test_a_plain_invoke_inside_an_implementation_is_forbidden() -> None:
    # Calling an ordinary workflow would be the way to reach a hook through it.
    plain = {
        "step_id": "child",
        "step_kind": "invoke_workflow",
        "override": {"invoke_workflow": {"workflow_id": "report"}},
    }

    assert codes(composite_action_descriptor_issues(descriptor(with_step(plain)))) == [
        "workflow.action.plain_invoke_forbidden"
    ]


def nested(action: dict, **config) -> dict:
    return {
        "step_id": "nested",
        "step_kind": "invoke_workflow",
        "override": {"invoke_workflow": {"action": action, **config}},
    }


def test_an_implementation_may_call_another_action_of_its_own_bundle() -> None:
    inner = implementation()
    inner["workflow_id"] = "ship_only"
    inner["settings"]["action_implementation"]["action_id"] = "orders.ship"
    outer = with_step(
        nested(
            {"extension_id": ADDON, "action_id": "orders.ship"},
            workflow_id="ship_only",
            connections={"amazon": {"slot": "amazon"}},
        )
    )
    actions = [
        {
            "action_id": "orders.create_full",
            "workflow_visible": True,
            "handler": {"mode": "workflow", "workflow_id": "create_full"},
        },
        {
            "action_id": "orders.ship",
            "workflow_visible": True,
            "handler": {"mode": "workflow", "workflow_id": "ship_only"},
        },
    ]

    assert composite_action_descriptor_issues(descriptor(outer, inner, actions=actions)) == []


@pytest.mark.parametrize(
    ("step", "code"),
    [
        (
            nested({"extension_id": ADDON, "action_id": "orders.missing"}, workflow_id="x"),
            "workflow.action.unknown",
        ),
        (
            nested({"extension_id": ADDON, "action_id": "orders.create_full"}, workflow_id="other"),
            "workflow.action.target_mismatch",
        ),
        (
            nested({"extension_id": "acme.books", "action_id": "invoice.create"}),
            "workflow.action.extension_not_declared",
        ),
        (
            nested({"extension_id": BASE, "action_id": "orders.sync"}, workflow_id="sync"),
            "workflow.action.dependency_target_forbidden",
        ),
        (
            nested({"extension_id": BASE, "install_id": "pi_base", "action_id": "orders.sync"}),
            "workflow.action.bundle_install_forbidden",
        ),
        (
            nested({"extension_id": BASE, "action_id": "orders.sync"}, installs={BASE: "pi_base"}),
            "workflow.action.bundle_install_forbidden",
        ),
        (
            nested(
                {"extension_id": BASE, "action_id": "orders.sync"},
                connections={"amazon": {"connection_id": "conn-1"}},
            ),
            "workflow.action.slot_unknown",
        ),
        (
            nested(
                {"extension_id": BASE, "action_id": "orders.sync"},
                connections={"amazon": {"slot": "books"}},
            ),
            "workflow.action.slot_unknown",
        ),
    ],
)
def test_nested_action_rules(step, code) -> None:
    assert code in codes(composite_action_descriptor_issues(descriptor(with_step(step))))


def test_an_action_of_a_required_dependency_is_named_by_the_action_alone() -> None:
    step = nested(
        {"extension_id": BASE, "action_id": "orders.sync"},
        connections={"account": {"slot": "amazon"}},
    )

    assert composite_action_descriptor_issues(descriptor(with_step(step))) == []


@pytest.mark.parametrize(
    ("step", "code"),
    [
        (
            connector_step("a", "orders.create", connection_binding_jsonb={"connection_id": "c1"}),
            "workflow.action.connection_must_be_slot",
        ),
        (
            connector_step(
                "a",
                "orders.create",
                connection_binding_jsonb={"slot": "amazon", "connection_id": "c1"},
            ),
            "workflow.action.connection_must_be_slot",
        ),
        (
            connector_step("a", "orders.create", connection_binding_jsonb={"slot": "books"}),
            "workflow.action.connection_must_be_slot",
        ),
        (
            connector_step("a", "orders.create", connector_install_id="pi_base"),
            "workflow.action.bundle_install_forbidden",
        ),
        (
            connector_step("a", "invoice.create", extension="acme.books"),
            "workflow.action.extension_not_declared",
        ),
        (
            connector_step("a", "notes.add", extension=ADDON),
            "workflow.action.slot_extension_mismatch",
        ),
    ],
)
def test_connector_step_rules(step, code) -> None:
    assert code in codes(composite_action_descriptor_issues(descriptor(with_step(step))))


def test_a_connector_step_may_need_no_connection_and_call_its_own_extension() -> None:
    step = connector_step("note", "notes.add", extension=ADDON)
    del step["connection_binding_jsonb"]

    assert composite_action_descriptor_issues(descriptor(with_step(step))) == []


def test_a_slot_names_this_extension_or_a_required_dependency() -> None:
    foreign = implementation()
    foreign["settings"]["action_implementation"]["connections"][0]["extension_id"] = "acme.books"
    foreign["steps"] = []
    optional = descriptor()
    optional["dependencies"]["extensions"][0]["required"] = False

    assert codes(composite_action_descriptor_issues(descriptor(foreign))) == [
        "workflow.action.extension_not_declared"
    ]
    # An optional dependency may be absent, so an implementation cannot rely on it.
    assert "workflow.action.extension_not_declared" in codes(
        composite_action_descriptor_issues(optional)
    )


def test_projection_points_own_extension_steps_at_the_install() -> None:
    own = connector_step("note", "notes.add", extension=ADDON)
    dependency = connector_step("create", "orders.create")
    own_action = nested(
        {"extension_id": ADDON, "action_id": "orders.ship"}, workflow_id="ship_only"
    )
    dependency_action = nested({"extension_id": BASE, "action_id": "orders.sync"})
    before = copy.deepcopy([own, dependency, own_action, dependency_action])
    references = {
        "agent": {},
        "skill": {},
        "tool": {},
        "workflow": {"ship_only": "extension.pi_addon.ship_only"},
    }

    steps = _projected_workflow_steps(
        [own, dependency, own_action, dependency_action],
        references,
        "pi_addon",
        extension_id=ADDON,
    )

    assert steps[0]["connector_install_id"] == "pi_addon"
    # Another extension stays unresolved: the caller chooses its install.
    assert "connector_install_id" not in steps[1]
    assert steps[1]["connection_binding_jsonb"] == {"slot": "amazon"}
    invoke = steps[2]["override"]["invoke_workflow"]
    assert invoke["workflow_id"] == "extension.pi_addon.ship_only"
    assert invoke["action"]["install_id"] == "pi_addon"
    assert "install_id" not in steps[3]["override"]["invoke_workflow"]["action"]
    assert [own, dependency, own_action, dependency_action] == before


def test_a_composite_action_cannot_be_called_directly() -> None:
    extension = {
        "bundle_root": "/bundles/amazon-pro",
        "action_definitions": descriptor()["action_definitions"],
    }

    refusal = composite_action_direct_call_refusal(extension, "orders.create_full")

    assert refusal["status"] == "failed"
    assert refusal["response"]["error_code"] == COMPOSITE_ACTION_DIRECT_CALL_CODE
    assert refusal["response"]["definitely_no_external_effect"] is True
    assert composite_action_direct_call_refusal(extension, "orders.note") is None
    assert composite_action_direct_call_refusal(extension, "") is None
    assert composite_action_direct_call_refusal(extension, "invoke_tool") is None


def with_interface(operation: dict, *, inputs: list[str], outputs: list[str]) -> dict:
    workflow = implementation(
        callable={
            "enabled": True,
            "trigger_channels": ["parent_workflow"],
            "expected_inputs": [{"name": name, "type": "string"} for name in inputs],
            "expected_outputs": [{"name": name, "type": "string"} for name in outputs],
        }
    )
    release = descriptor(workflow)
    release["operation_manifest"] = [{"operation_id": "orders.create_full", **operation}]
    return release


def test_the_operation_describes_the_interface_of_the_workflow() -> None:
    operation = {
        "inputs": [{"name": "order", "type": "object"}],
        "outputs": [{"name": "order_id", "type": "string"}],
    }

    assert (
        composite_action_descriptor_issues(
            with_interface(operation, inputs=["order"], outputs=["order_id"])
        )
        == []
    )


def test_an_input_or_output_on_one_side_only_is_refused() -> None:
    operation = {
        "inputs": [{"name": "order"}, {"name": "priority"}],
        "outputs": [{"name": "order_id"}],
    }

    issues = composite_action_descriptor_issues(
        with_interface(operation, inputs=["order"], outputs=["order_id", "shipment_id"])
    )

    assert codes(issues) == [
        "composite_action_interface_mismatch",
        "composite_action_interface_mismatch",
    ]
    assert [issue["path"] for issue in issues] == [
        "operation_manifest.orders.create_full.inputs",
        "operation_manifest.orders.create_full.outputs",
    ]
    assert "priority" in issues[0]["message"]
    assert "shipment_id" in issues[1]["message"]


def test_the_operation_of_a_composite_action_declares_no_connection_type() -> None:
    operation = {"inputs": [], "outputs": [], "connection_type_ids": ["seller_account"]}

    assert codes(
        composite_action_descriptor_issues(with_interface(operation, inputs=[], outputs=[]))
    ) == ["composite_action_operation_connection"]
