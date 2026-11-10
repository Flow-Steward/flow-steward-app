"""The action Invoke and the action context of its child job (ADR 0065)."""

from __future__ import annotations

import pytest

from core.application.extensions.composite_action_resolution import InstalledComposites
from core.application.extensions.composite_actions import CompositeAction
from core.application.orchestration.composite_action_runtime import (
    ActionPlan,
    ActionRuntimeError,
    child_origin_issue,
    plan_action_invoke,
    resolve_connector_action_binding,
    write_action_context,
)
from core.application.workflows.workflow_action_contract import (
    COMPOSITE_ACTION_META_KEY,
    ActionImplementation,
    ConnectionSlot,
)
from core.application.workflows.workflow_action_launch_gate import (
    DIRECT_LAUNCH_CODE,
    ActionImplementationLaunchError,
    is_action_child_launch,
    refuse_direct_action_implementation,
)

BASE = "acme.amazon"
ADDON = "acme.amazon-pro"
IMPLEMENTATION_ID = "extension.pi_addon.create_full"


def composite(action_id: str = "orders.create_full", *, slots: bool = True) -> CompositeAction:
    connections = (ConnectionSlot("amazon", BASE, ("seller_account",)),) if slots else ()
    return CompositeAction(
        ADDON, action_id, "create_full", ActionImplementation(action_id, connections)
    )


def installs(**overrides: InstalledComposites | None):
    known: dict[str, InstalledComposites | None] = {
        "pi_addon": InstalledComposites(
            install_id="pi_addon",
            extension_id=ADDON,
            release_id="pr_1",
            active=True,
            actions={"orders.create_full": composite()},
            required_dependencies=frozenset({BASE}),
        ),
        "pi_base": InstalledComposites(
            install_id="pi_base", extension_id=BASE, release_id="pr_2", active=True
        ),
    }
    known.update(overrides)
    return known.get


def spec(**changes) -> dict:
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


def plan(config: dict | None = None, *, parent: dict | None = None, resolver=None) -> ActionPlan:
    return plan_action_invoke(
        config or spec(), parent_context=parent or {}, resolve_install=resolver or installs()
    )


def test_a_persons_step_resolves_to_the_implementation_and_its_context() -> None:
    resolved = plan()

    assert resolved.workflow_id == IMPLEMENTATION_ID
    assert resolved.context() == {
        "extension_id": ADDON,
        "install_id": "pi_addon",
        "action_id": "orders.create_full",
        "installs": {ADDON: "pi_addon", BASE: "pi_base"},
        "connections": {"amazon": "conn-1"},
    }
    # The child is an ordinary direct Invoke; the action keys never reach it.
    assert resolved.direct_spec(spec()) == {
        "workflow_id": IMPLEMENTATION_ID,
        "input_mapping": {"order": "state.order"},
        "output_mapping": {"order_id": "state.order_id"},
    }


def test_a_nested_action_takes_its_install_and_slot_from_the_caller() -> None:
    nested = {
        "action": {"extension_id": ADDON, "action_id": "orders.create_full"},
        "connections": {"amazon": {"slot": "store"}},
    }
    parent = {
        "installs": {ADDON: "pi_addon", BASE: "pi_base"},
        "connections": {"store": "conn-7"},
    }

    resolved = plan(nested, parent=parent)

    assert resolved.install_id == "pi_addon"
    assert resolved.workflow_id == IMPLEMENTATION_ID
    assert resolved.installs == {ADDON: "pi_addon", BASE: "pi_base"}
    assert resolved.connections == {"amazon": "conn-7"}


def test_an_action_without_slots_needs_no_connection() -> None:
    addon = InstalledComposites(
        install_id="pi_addon",
        extension_id=ADDON,
        release_id="pr_1",
        active=True,
        actions={"orders.create_full": composite(slots=False)},
    )

    resolved = plan(spec(installs={}, connections={}), resolver=installs(pi_addon=addon))

    assert resolved.installs == {ADDON: "pi_addon"}
    assert resolved.connections == {}


@pytest.mark.parametrize(
    ("config", "resolver", "code"),
    [
        (
            spec(action={"extension_id": ADDON, "action_id": "orders.create_full"}),
            None,
            "action_install_unresolved",
        ),
        (spec(), installs(pi_addon=None), "action_install_unavailable"),
        (
            spec(action={"extension_id": BASE, "install_id": "pi_addon", "action_id": "x"}),
            None,
            "action_install_unavailable",
        ),
        (
            spec(),
            installs(pi_addon=InstalledComposites("pi_addon", ADDON, "pr_1", False)),
            "action_install_inactive",
        ),
        (
            spec(
                action={"extension_id": ADDON, "install_id": "pi_addon", "action_id": "orders.old"}
            ),
            None,
            "action_not_composite",
        ),
        (spec(workflow_id="extension.pi_addon.other"), None, "action_target_mismatch"),
        (spec(installs={}), None, "action_install_unresolved"),
        (spec(installs={BASE: "pi_gone"}), None, "action_install_unavailable"),
        (spec(installs={BASE: "pi_addon"}), None, "action_install_unavailable"),
        (spec(connections={}), None, "action_connection_required"),
        (spec(connections={"amazon": {"slot": "store"}}), None, "action_connection_required"),
        (spec(action="orders.create_full"), None, "workflow.action.ref_invalid"),
    ],
)
def test_an_action_invoke_that_cannot_resolve_is_refused(config, resolver, code) -> None:
    with pytest.raises(ActionRuntimeError) as caught:
        plan(config, resolver=resolver)

    assert caught.value.code == code


def implementation_row(**changes) -> dict:
    row = {
        "workflow_id": IMPLEMENTATION_ID,
        "source_install_id": "pi_addon",
        "settings": {"action_implementation": {"action_id": "orders.create_full"}},
    }
    row.update(changes)
    return row


def test_an_implementation_runs_only_under_the_action_that_resolves_to_it() -> None:
    resolved = plan()

    assert child_origin_issue(implementation_row(), resolved) == ""
    # A direct Invoke of an implementation is refused whatever the saved step says.
    assert "runs only as that action" in child_origin_issue(implementation_row(), None)
    assert child_origin_issue(implementation_row(source_install_id="pi_other"), resolved)
    other = implementation_row(settings={"action_implementation": {"action_id": "orders.ship"}})
    assert child_origin_issue(other, resolved)
    # An action never stands in front of an ordinary workflow.
    assert child_origin_issue({"workflow_id": "report", "settings": {}}, resolved)
    assert child_origin_issue({"workflow_id": "report", "settings": {}}, None) == ""
    assert child_origin_issue(None, None) == ""


def test_the_action_context_is_written_into_the_child_launch_metadata() -> None:
    meta = {"invoke_parent_job_id": "41"}

    write_action_context(meta, None)
    assert COMPOSITE_ACTION_META_KEY not in meta
    write_action_context(meta, plan())
    assert meta[COMPOSITE_ACTION_META_KEY]["connections"] == {"amazon": "conn-1"}


def connector_step(**fields) -> dict:
    return {
        "step_id": "create",
        "step_kind": "connector",
        "connector_id": f"extension.{BASE}",
        "override": {"connector": {"extension_id": BASE, "operation_id": "orders.create"}},
        **fields,
    }


def binding(
    step: dict, connection_binding: dict, install_id: str = "", context: dict | None = None
):
    loads: list[int] = []

    def load_context() -> dict:
        loads.append(1)
        return context or {}

    result = resolve_connector_action_binding(
        step=step,
        connection_binding=connection_binding,
        connector_install_id=install_id,
        load_context=load_context,
    )
    return result, len(loads)


def test_an_ordinary_connector_step_never_loads_the_action_context() -> None:
    result, loads = binding(connector_step(), {"connection_id": "conn-1"}, "pi_base")

    assert loads == 0
    assert result.error is None
    assert result.connection_binding == {"connection_id": "conn-1"}
    assert result.connector_install_id == "pi_base"


def test_inside_an_action_the_slot_and_the_install_come_from_the_job() -> None:
    context = {"installs": {BASE: "pi_base"}, "connections": {"amazon": "conn-1"}}

    result, loads = binding(connector_step(), {"slot": "amazon"}, context=context)

    assert loads == 1
    assert result.error is None
    assert result.connection_binding == {"connection_id": "conn-1"}
    assert result.connector_install_id == "pi_base"


def test_the_slot_wins_over_a_connection_the_step_names() -> None:
    context = {"installs": {BASE: "pi_base"}, "connections": {"amazon": "conn-1"}}

    result, _ = binding(
        connector_step(), {"slot": "amazon", "connection_id": "conn-dev"}, context=context
    )

    assert result.connection_binding == {"connection_id": "conn-1"}


@pytest.mark.parametrize(
    ("connection_binding", "context", "code"),
    [
        ({"slot": "amazon"}, {}, "action_context_missing"),
        (
            {"slot": "amazon"},
            {"installs": {BASE: "pi_base"}, "connections": {}},
            "action_connection_required",
        ),
        (
            {"slot": "amazon"},
            {"installs": {}, "connections": {"amazon": "conn-1"}},
            "action_install_unresolved",
        ),
    ],
)
def test_a_step_that_cannot_resolve_fails_before_the_connector_is_called(
    connection_binding, context, code
) -> None:
    result, _ = binding(connector_step(), connection_binding, context=context)

    assert result.error["error"] == code
    assert result.error["ok"] is False
    assert result.error["definitely_no_external_effect"] is True


def test_the_authors_own_workflow_tests_with_the_connection_it_names() -> None:
    # Not an action job: the author's workflow in a Development environment
    # names both the slot it will read once packaged and a connection to test with.
    result, _ = binding(
        connector_step(connector_install_id="pi_base"),
        {"slot": "amazon", "connection_id": "conn-dev"},
        "pi_base",
    )

    assert result.error is None
    assert result.connection_binding == {"connection_id": "conn-dev"}


def test_a_step_without_a_slot_keeps_the_existing_missing_install_refusal() -> None:
    result, loads = binding(connector_step(), {})

    assert loads == 1
    assert result.error is None
    assert result.connector_install_id == ""


def test_only_a_child_of_the_action_invoke_may_start_an_implementation() -> None:
    loads: list[str] = []

    def load(workflow_id: str, account_id: str) -> dict:
        loads.append(workflow_id)
        return implementation_row()

    with pytest.raises(ActionImplementationLaunchError) as caught:
        refuse_direct_action_implementation(
            workflow_id=IMPLEMENTATION_ID, meta={"test_mode": True}, load_workflow=load
        )
    assert caught.value.code == DIRECT_LAUNCH_CODE
    # A restart copies neither the action context nor the parent link.
    with pytest.raises(ActionImplementationLaunchError):
        refuse_direct_action_implementation(
            workflow_id=IMPLEMENTATION_ID,
            meta={COMPOSITE_ACTION_META_KEY: {"install_id": "pi_addon"}},
            load_workflow=load,
        )
    assert loads == [IMPLEMENTATION_ID, IMPLEMENTATION_ID]

    child_meta = {
        COMPOSITE_ACTION_META_KEY: {"install_id": "pi_addon"},
        "invoke_parent_job_id": "41",
    }
    assert is_action_child_launch(child_meta)
    refuse_direct_action_implementation(
        workflow_id=IMPLEMENTATION_ID, meta=child_meta, load_workflow=load
    )
    assert loads == [IMPLEMENTATION_ID, IMPLEMENTATION_ID]


def test_other_workflows_pass_the_launch_gate_untouched() -> None:
    loads: list[str] = []

    def load(workflow_id: str, account_id: str) -> dict | None:
        loads.append(workflow_id)
        return {"workflow_id": workflow_id, "source_install_id": "pi_addon", "settings": {}}

    refuse_direct_action_implementation(workflow_id="orders", meta=None, load_workflow=load)
    refuse_direct_action_implementation(workflow_id="", meta=None, load_workflow=load)
    assert loads == []
    # A visible workflow of an extension is looked up and allowed.
    refuse_direct_action_implementation(
        workflow_id="extension.pi_addon.report", meta=None, load_workflow=load
    )
    refuse_direct_action_implementation(
        workflow_id="extension.pi_addon.gone", meta=None, load_workflow=lambda *_: None
    )
    assert loads == ["extension.pi_addon.report"]
