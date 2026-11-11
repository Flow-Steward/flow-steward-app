"""A run names a tool step by what it called: an extension, an MCP server or a system tool."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.application.extensions import display_labels
from core.application.reports.job_detail_contract import _name_step_extensions
from core.application.reports.ui_projection_helpers import _build_message_steps
from core.application.workflows.workflow_step_kinds import (
    connector_step_summary,
    connector_tool_kind,
)

EXTENSION_CONNECTOR = "extension.acme.store"
MCP_CONNECTOR = "connector.mcp.mcp_7d5037bc0dee4ec9.update_spreadsheet"


@pytest.mark.parametrize(
    ("connector_id", "extension_id", "kind"),
    [
        (EXTENSION_CONNECTOR, "", "extension"),
        ("legacy_store", "acme.store", "extension"),
        (MCP_CONNECTOR, "", "mcp"),
        ("text.join", "", "system"),
        ("", "", "system"),
    ],
)
def test_a_tool_step_is_one_of_three_kinds(connector_id, extension_id, kind) -> None:
    assert connector_tool_kind(connector_id, extension_id) == kind


def test_the_step_line_never_says_connector_or_a_runtime_mode() -> None:
    done = connector_step_summary(connector_id=EXTENSION_CONNECTOR, name="Sync stock", ok=True)
    failed = connector_step_summary(
        connector_id=MCP_CONNECTOR, name="Add tabs", ok=False, detail="rate limited"
    )

    assert done == "Extension action 'Sync stock' completed"
    assert failed == "MCP tool 'Add tabs' failed: rate limited"
    assert connector_step_summary(connector_id="text.join", ok=False) == "System tool failed"
    for line in (done, failed):
        assert "onnector" not in line
        assert "extension_action" not in line


def _steps(artifacts: dict) -> dict:
    message = {
        "id": "m1",
        "step_id": "sync",
        "from_agent": "connector_step",
        "skill": "connector",
        "status": "pass",
        "timestamp": "2026-11-11T10:00:00Z",
        "artifacts": {"step_kind": "connector", **artifacts},
    }
    return _build_message_steps([message])[0]


def test_an_extension_step_shows_its_extension_not_a_tool() -> None:
    step = _steps({"connector_id": EXTENSION_CONNECTOR, "action_id": "update_inventory"})

    assert step["step_type_label"] == "Extension action"
    assert step["step_type_label_key"] == "react.jobs.kind_extension_tool"
    assert step["tool_kind"] == "extension"
    # The extension's own id, without the connector prefix, until its name is read.
    assert step["tool_label"] == step["tool_extension_id"] == "acme.store"


def test_mcp_and_system_tools_keep_their_own_words() -> None:
    mcp = _steps({"connector_id": MCP_CONNECTOR})
    system = _steps({"connector_id": "text.join"})

    assert (mcp["step_type_label"], mcp["step_type_label_key"], mcp["tool_kind"]) == (
        "MCP tool",
        "react.jobs.kind_mcp_tool",
        "mcp",
    )
    assert (system["step_type_label"], system["step_type_label_key"]) == (
        "System tool",
        "react.jobs.kind_system_tool",
    )
    assert mcp["tool_extension_id"] == system["tool_extension_id"] == ""


class _Names:
    def __init__(self, names: dict[str, str]) -> None:
        self.names = names
        self.asked: list[list[str]] = []

    def extension_display_names(self, extension_ids, *, scope=None):
        _ = scope
        self.asked.append(list(extension_ids))
        return self.names


def test_extension_steps_are_shown_by_the_extension_name() -> None:
    known = {"tool_label": "acme.store", "tool_extension_id": "acme.store"}
    unknown = {"tool_label": "acme.gone", "tool_extension_id": "acme.gone"}
    system = {"tool_label": "text.join", "tool_extension_id": ""}
    service = _Names({"acme.store": "Acme Store"})

    asyncio.run(
        _name_step_extensions(
            [known, unknown, system, None, dict(known)], job_query_service=service, scope=None
        )
    )

    assert known["tool_label"] == "Acme Store"
    # An extension that is no longer installed keeps its id; other tools are untouched.
    assert unknown["tool_label"] == "acme.gone"
    assert system["tool_label"] == "text.join"
    assert service.asked == [["acme.gone", "acme.store"]]


def test_no_names_are_read_for_a_run_without_extension_steps() -> None:
    service = _Names({})

    asyncio.run(
        _name_step_extensions(
            [{"tool_label": "text.join", "tool_extension_id": ""}],
            job_query_service=service,
            scope=None,
        )
    )
    # A service that cannot name extensions leaves the rows as they are.
    row = {"tool_label": "acme.store", "tool_extension_id": "acme.store"}
    asyncio.run(_name_step_extensions([row], job_query_service=object(), scope=None))

    assert service.asked == []
    assert row["tool_label"] == "acme.store"


def test_installed_names_come_from_the_release_each_install_pins(monkeypatch) -> None:
    installs = [
        SimpleNamespace(extension_id="acme.store", release_id="r1"),
        SimpleNamespace(extension_id="acme.store", release_id="r0"),
        SimpleNamespace(extension_id="acme.other", release_id="r2"),
        SimpleNamespace(extension_id="acme.unnamed", release_id="r3"),
    ]
    releases = {
        "r1": SimpleNamespace(descriptor={"display_name": "Acme Store"}),
        "r0": SimpleNamespace(descriptor={"display_name": "Old name"}),
        "r2": SimpleNamespace(descriptor={"display_name": "Other"}),
        "r3": SimpleNamespace(descriptor={}),
    }
    asked: list[tuple] = []

    class _Installs:
        def __init__(self, connection) -> None:
            _ = connection

        def list_for_account(self, account_id, **kwargs):
            asked.append((account_id, kwargs))
            return installs

    class _Releases:
        def __init__(self, connection) -> None:
            _ = connection

        def get_release(self, release_id):
            return releases.get(release_id)

    monkeypatch.setattr("core.application.extensions.install.DBExtensionInstallStore", _Installs)
    monkeypatch.setattr(
        "core.application.extensions.runtime_store_db.DBExtensionRuntimeStore", _Releases
    )

    names = display_labels.installed_extension_names(
        object(), account_id="acct", extension_ids=["acme.store", "acme.unnamed", ""]
    )

    assert names == {"acme.store": "Acme Store"}
    assert asked == [("acct", {"include_global": True, "include_inactive_owned": True})]
    assert display_labels.installed_extension_names(None, account_id="a", extension_ids=["x"]) == {}
    assert (
        display_labels.installed_extension_names(object(), account_id="a", extension_ids=[]) == {}
    )
