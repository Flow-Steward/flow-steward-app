"""The shapes of a composite action: the implementation marker and the action Invoke (ADR 0065)."""

from __future__ import annotations

import json

import pytest

from core.application.workflows.workflow_action_contract import (
    ACTION_IMPLEMENTATION_SETTING,
    COMPOSITE_ACTION_META_KEY,
    ActionContractError,
    action_config_issues,
    action_connections,
    action_implementation_of,
    action_installs,
    action_ref_of,
    action_run_label,
    action_run_of,
    composite_action_context,
    is_action_implementation,
    is_installed_action_implementation,
    parse_action_implementation,
    parse_action_ref,
    slot_of_binding,
)
from core.application.workflows.workflow_hook_contract import invoke_config_issues
from core.application.workflows.workflow_step_config_contracts import STEP_CONFIG_ALLOWED_KEYS

BASE_EXTENSION = "acme.amazon"
ADDON_EXTENSION = "acme.amazon-pro"


def marker(**changes) -> dict:
    raw = {
        "action_id": "orders.create_full",
        "connections": [
            {
                "slot": "amazon",
                "extension_id": BASE_EXTENSION,
                "connection_type_ids": ["seller_account"],
            }
        ],
    }
    raw.update(changes)
    return raw


def action_config(**changes) -> dict:
    raw = {
        "workflow_id": "extension.pi_addon.create_full",
        "action": {
            "extension_id": ADDON_EXTENSION,
            "install_id": "pi_addon",
            "action_id": "orders.create_full",
        },
        "installs": {BASE_EXTENSION: "pi_base"},
        "connections": {"amazon": {"connection_id": "conn-1"}},
        "input_mapping": {},
        "output_mapping": {},
    }
    raw.update(changes)
    return raw


def test_marker_parses_to_the_action_and_its_slots() -> None:
    parsed = parse_action_implementation(marker())

    assert parsed.action_id == "orders.create_full"
    assert parsed.slot("amazon").extension_id == BASE_EXTENSION
    assert parsed.slot("amazon").connection_type_ids == ("seller_account",)
    assert parsed.slot("other") is None
    assert parsed.dump() == marker()


def test_marker_without_slots_is_an_action_that_needs_no_connection() -> None:
    assert parse_action_implementation({"action_id": "report.build"}).connections == ()


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("create", "workflow.action.marker_invalid"),
        (marker(outcomes=[]), "workflow.action.marker_field_unknown"),
        (marker(action_id=" "), "workflow.action.marker_action_missing"),
        (marker(connections={}), "workflow.action.slot_invalid"),
        (marker(connections=["amazon"]), "workflow.action.slot_invalid"),
        (
            marker(connections=[{"slot": "Amazon", "extension_id": BASE_EXTENSION}]),
            "workflow.action.slot_name_invalid",
        ),
        (
            marker(connections=[{"slot": "amazon", "extension_id": "amazon"}]),
            "workflow.action.slot_extension_invalid",
        ),
        (
            marker(
                connections=[
                    {"slot": "amazon", "extension_id": BASE_EXTENSION, "connection_type_ids": [""]}
                ]
            ),
            "workflow.action.slot_types_invalid",
        ),
        (
            marker(
                connections=[{"slot": "amazon", "extension_id": BASE_EXTENSION, "install": "x"}]
            ),
            "workflow.action.slot_field_unknown",
        ),
        (
            marker(
                connections=[
                    {"slot": "amazon", "extension_id": BASE_EXTENSION},
                    {"slot": "amazon", "extension_id": BASE_EXTENSION},
                ]
            ),
            "workflow.action.slot_duplicate",
        ),
    ],
)
def test_a_malformed_marker_is_refused_with_its_field(raw, code) -> None:
    with pytest.raises(ActionContractError) as caught:
        parse_action_implementation(raw)

    assert caught.value.code == code
    assert caught.value.path.startswith(f"settings.{ACTION_IMPLEMENTATION_SETTING}")


def test_a_workflow_is_an_implementation_when_it_carries_the_marker() -> None:
    plain = {"workflow_id": "orders", "settings": {"callable": {"enabled": True}}}
    marked = {"workflow_id": "create_full", "settings": {ACTION_IMPLEMENTATION_SETTING: marker()}}

    assert not is_action_implementation(plain)
    assert not is_action_implementation(None)
    assert is_action_implementation(marked)
    assert is_action_implementation({**marked, "settings": json.dumps(marked["settings"])})
    # A list projection carries the flag instead of the settings.
    assert is_action_implementation({"workflow_id": "x", ACTION_IMPLEMENTATION_SETTING: True})
    # A claim counts even when it is malformed: such a workflow is still hidden and refused.
    assert is_action_implementation({"settings": {ACTION_IMPLEMENTATION_SETTING: "broken"}})
    assert action_implementation_of(plain) is None
    assert action_implementation_of(marked).action_id == "orders.create_full"


def test_only_a_projected_implementation_is_an_installed_one() -> None:
    marked = {"settings": {ACTION_IMPLEMENTATION_SETTING: marker()}}

    # The author's own workflow in a Development environment stays an ordinary workflow.
    assert not is_installed_action_implementation(marked)
    assert is_installed_action_implementation({**marked, "source_install_id": "pi_addon"})
    assert not is_installed_action_implementation({"source_install_id": "pi_addon"})


def test_action_ref_names_one_action_of_one_extension() -> None:
    ref = parse_action_ref(action_config()["action"])

    assert (ref.extension_id, ref.install_id, ref.action_id) == (
        ADDON_EXTENSION,
        "pi_addon",
        "orders.create_full",
    )
    assert action_ref_of({"workflow_id": "child"}) is None
    # Inside a bundle the install is absent: the caller's choice supplies it.
    assert parse_action_ref({"extension_id": BASE_EXTENSION, "action_id": "a"}).install_id == ""


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("orders.create_full", "workflow.action.ref_invalid"),
        ({"extension_id": "amazon", "action_id": "a"}, "workflow.action.ref_extension_invalid"),
        ({"extension_id": BASE_EXTENSION}, "workflow.action.ref_action_missing"),
        (
            {"extension_id": BASE_EXTENSION, "action_id": "a", "workflow": "x"},
            "workflow.action.ref_field_unknown",
        ),
    ],
)
def test_a_malformed_action_ref_is_refused(raw, code) -> None:
    with pytest.raises(ActionContractError) as caught:
        parse_action_ref(raw)

    assert caught.value.code == code


def test_installs_and_connections_are_read_from_the_invoke_config() -> None:
    config = action_config(
        connections={
            "amazon": {"connection_id": " conn-1 "},
            "billing": {"slot": "books"},
            "empty": {},
            "broken": "conn-2",
        }
    )

    assert action_installs(config) == {BASE_EXTENSION: "pi_base"}
    assert action_connections(config) == {
        "amazon": {"connection_id": "conn-1"},
        "billing": {"slot": "books"},
    }
    assert action_installs({"installs": "pi_base"}) == {}
    assert slot_of_binding({"slot": " amazon "}) == "amazon"
    assert slot_of_binding({"connection_id": "conn-1"}) == ""


def test_a_complete_action_invoke_has_no_issues() -> None:
    assert action_config_issues(action_config()) == []
    assert invoke_config_issues(action_config()) == []
    assert action_config_issues({"workflow_id": "child", "input_mapping": {}}) == []


@pytest.mark.parametrize(
    ("config", "code"),
    [
        ({"workflow_id": "child", "installs": {}}, "workflow.action.origin_missing"),
        ({"workflow_id": "child", "connections": {}}, "workflow.action.origin_missing"),
        (action_config(action={"extension_id": "amazon"}), "workflow.action.ref_extension_invalid"),
        (action_config(installs=["pi_base"]), "workflow.action.mapping_invalid"),
        (action_config(connections={"amazon": {}}), "workflow.action.connection_invalid"),
        (
            action_config(connections={"Amazon": {"connection_id": "c"}}),
            "workflow.action.connection_invalid",
        ),
    ],
)
def test_action_keys_of_an_invoke_are_checked(config, code) -> None:
    issues = action_config_issues(config)

    assert [issue["code"] for issue in issues] == [code]
    assert issues[0]["path"].startswith("override.invoke_workflow")


def test_an_action_never_stands_next_to_a_hook() -> None:
    config = {"action": action_config()["action"], "hook": {"hook_id": "before"}}

    assert [issue["code"] for issue in action_config_issues(config)] == [
        "workflow.action.hook_conflict"
    ]


def test_an_invoke_names_a_workflow_a_hook_or_an_action() -> None:
    # Inside a bundle an action of another extension is named by the action alone.
    action_only = {"action": {"extension_id": BASE_EXTENSION, "action_id": "orders.sync"}}

    assert invoke_config_issues(action_only) == []
    assert [issue["code"] for issue in invoke_config_issues({"input_mapping": {}})] == [
        "workflow.invoke.target_missing"
    ]


def test_the_invoke_step_accepts_the_action_keys() -> None:
    assert {"action", "installs", "connections"} <= STEP_CONFIG_ALLOWED_KEYS["invoke_workflow"]


def test_the_action_context_is_read_from_job_metadata_only() -> None:
    context = {"install_id": "pi_addon", "connections": {"amazon": "conn-1"}}

    assert composite_action_context({COMPOSITE_ACTION_META_KEY: context}) == context
    assert composite_action_context(json.dumps({COMPOSITE_ACTION_META_KEY: context})) == context
    assert composite_action_context({COMPOSITE_ACTION_META_KEY: "conn-1"}) == {}
    assert composite_action_context("not json") == {}
    assert composite_action_context(None) == {}


def test_the_run_of_an_action_reads_as_the_action_and_its_extension() -> None:
    context = {"extension_id": "acme.amazon-pro", "action_id": "orders.create_full"}
    named = {**context, "extension_name": "Amazon Pro"}
    job = {"selected_workflow_display_name": "Create order with shipment"}

    # A detail row carries the whole meta, a list row only the selected column.
    assert action_run_label({**job, "meta": {COMPOSITE_ACTION_META_KEY: named}}) == (
        "Create order with shipment · Amazon Pro"
    )
    assert action_run_label({**job, COMPOSITE_ACTION_META_KEY: named}) == (
        "Create order with shipment · Amazon Pro"
    )
    # A run created before the name was recorded reads by the extension's id.
    assert action_run_of({"meta": json.dumps({COMPOSITE_ACTION_META_KEY: context})}) == {
        **context,
        "extension_name": "acme.amazon-pro",
    }
    assert action_run_label({COMPOSITE_ACTION_META_KEY: context}) == (
        "orders.create_full · acme.amazon-pro"
    )


def test_an_ordinary_run_has_no_action_label() -> None:
    assert action_run_label({"selected_workflow_display_name": "Weekly report", "meta": {}}) is None
    assert (
        action_run_label({COMPOSITE_ACTION_META_KEY: {"extension_id": "acme.amazon-pro"}}) is None
    )
    assert action_run_of(None) == {}
