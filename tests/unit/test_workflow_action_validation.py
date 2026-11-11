"""Save-time rules of an Invoke that calls an extension action (ADR 0065)."""

from __future__ import annotations

import pytest

from core.application.extensions.composite_action_resolution import InstalledComposites
from core.application.extensions.composite_actions import CompositeAction
from core.application.workflows import workflow_action_validation as validation
from core.application.workflows.workflow_action_contract import (
    ActionImplementation,
    ConnectionSlot,
)

BASE = "acme.amazon"
ADDON = "acme.amazon-pro"
IMPLEMENTATION_ID = "extension.pi_addon.create_full"


class Store:
    """The connection port readiness reads; nothing else of the store is touched."""

    def __init__(self, connections: dict[str, dict]) -> None:
        self.connections = connections

    def get_extension_connection_metadata(self, connection_id: str) -> dict | None:
        return self.connections.get(connection_id)


def connection(**changes) -> dict:
    row = {
        "connection_id": "conn-1",
        "extension_id": BASE,
        "extension_install_id": "pi_base",
        "connection_type": "seller_account",
        "enabled": True,
        "runtime_status": "ready",
    }
    row.update(changes)
    return row


@pytest.fixture
def installs(monkeypatch):
    action = CompositeAction(
        ADDON,
        "orders.create_full",
        "create_full",
        ActionImplementation(
            "orders.create_full", (ConnectionSlot("amazon", BASE, ("seller_account",)),)
        ),
    )
    known: dict[str, InstalledComposites | None] = {
        "pi_addon": InstalledComposites(
            install_id="pi_addon",
            extension_id=ADDON,
            release_id="pr_1",
            active=True,
            actions={"orders.create_full": action},
            required_dependencies=frozenset({BASE}),
        ),
        "pi_base": InstalledComposites("pi_base", BASE, "pr_2", True),
    }
    monkeypatch.setattr(
        validation,
        "installed_composites_for_store",
        lambda db, *, install_id, account_id="": known.get(install_id),
    )
    monkeypatch.setattr(
        "core.application.workflows.extension_connection_validation.project_connection_is_viable",
        lambda row: bool(row.get("enabled")),
    )
    return known


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
    }
    raw.update(changes)
    return raw


IMPLEMENTATION = {
    "workflow_id": IMPLEMENTATION_ID,
    "source_install_id": "pi_addon",
    "settings": {"action_implementation": {"action_id": "orders.create_full"}},
}


def issues(config: dict, *, store: Store | None = None, child=IMPLEMENTATION, parent=None):
    found = validation.action_invoke_issues(
        store or Store({"conn-1": connection()}),
        spec=config,
        child_workflow=child,
        parent_workflow=parent,
        account_id="acct",
    )
    return [(row["code"], row["path"]) for row in found]


def test_a_complete_action_step_is_valid(installs) -> None:
    assert issues(spec()) == []


def test_a_plain_invoke_of_an_ordinary_workflow_is_not_touched(installs) -> None:
    assert issues({"workflow_id": "report"}, child={"workflow_id": "report", "settings": {}}) == []


def test_an_implementation_cannot_be_invoked_without_its_action(installs) -> None:
    assert issues({"workflow_id": IMPLEMENTATION_ID}) == [
        ("workflow.action.origin_required", "override.invoke_workflow.workflow_id")
    ]


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (
            spec(action={"extension_id": ADDON, "action_id": "orders.create_full"}),
            ("workflow.extension_install_missing", "override.invoke_workflow.action.install_id"),
        ),
        (
            spec(action={"extension_id": ADDON, "install_id": "pi_gone", "action_id": "a"}),
            ("workflow.extension_unavailable", "override.invoke_workflow.action.install_id"),
        ),
        (
            spec(action={"extension_id": ADDON, "install_id": "pi_addon", "action_id": "old"}),
            (
                "workflow.extension_operation_unavailable",
                "override.invoke_workflow.action.action_id",
            ),
        ),
        (
            spec(workflow_id="extension.pi_addon.other"),
            ("workflow.action.target_mismatch", "override.invoke_workflow.workflow_id"),
        ),
        (
            spec(installs={}),
            ("workflow.extension_install_missing", f"override.invoke_workflow.installs.{BASE}"),
        ),
        (
            spec(installs={BASE: "pi_gone"}),
            ("workflow.extension_unavailable", f"override.invoke_workflow.installs.{BASE}"),
        ),
        (
            spec(connections={}),
            (
                "workflow.connection_required",
                "override.invoke_workflow.connections.amazon.connection_id",
            ),
        ),
        (
            # Passing the caller's slot down is for a bundled implementation only.
            spec(connections={"amazon": {"slot": "store"}}),
            (
                "workflow.connection_required",
                "override.invoke_workflow.connections.amazon.connection_id",
            ),
        ),
        (
            spec(connections={"amazon": {"connection_id": "conn-missing"}}),
            (
                "workflow.connection_unavailable",
                "override.invoke_workflow.connections.amazon.connection_id",
            ),
        ),
        (
            spec(
                connections={"amazon": {"connection_id": "conn-1"}, "books": {"connection_id": "c"}}
            ),
            ("workflow.action.slot_unknown", "override.invoke_workflow.connections.books"),
        ),
        (
            spec(installs=["pi_base"]),
            ("workflow.action.mapping_invalid", "override.invoke_workflow.installs"),
        ),
    ],
)
def test_what_the_builder_must_fix_before_the_step_can_run(installs, config, expected) -> None:
    assert expected in issues(config)


def test_an_inactive_install_is_reported_as_unavailable(installs) -> None:
    installs["pi_addon"] = InstalledComposites("pi_addon", ADDON, "pr_1", False)

    assert issues(spec()) == [
        ("workflow.extension_unavailable", "override.invoke_workflow.action.install_id")
    ]


@pytest.mark.parametrize(
    "row",
    [
        connection(extension_install_id="pi_other"),
        connection(extension_id="acme.books"),
        connection(connection_type="vendor_account"),
        connection(enabled=False),
    ],
)
def test_a_slot_connection_must_belong_to_the_chosen_install(installs, row) -> None:
    assert issues(spec(), store=Store({"conn-1": row})) == [
        (
            "workflow.connection_unavailable",
            "override.invoke_workflow.connections.amazon.connection_id",
        )
    ]


def test_the_authors_own_implementation_is_left_to_the_bundle_rules(installs) -> None:
    author = {"workflow_id": "outer", "settings": {"action_implementation": {"action_id": "x"}}}
    nested = {
        "action": {"extension_id": BASE, "action_id": "orders.sync"},
        "connections": {"account": {"slot": "amazon"}},
    }

    assert issues(nested, child=None, parent=author) == []
    # Once installed it is checked like any other caller.
    installed = {**author, "source_install_id": "pi_addon"}
    assert issues(nested, child=None, parent=installed) == [
        ("workflow.extension_install_missing", "override.invoke_workflow.action.install_id")
    ]
