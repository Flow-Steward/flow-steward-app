"""Every built-in system tool action has a readable name in the step editor's catalog.

The registry stored a built-in tool's interface through ToolFunction, which kept only the
operation's ``name``: its ``display_name`` and ``description`` were dropped on save. The
catalog then named each action by its id (``stringify``, ``pick``), which the step editor
never shows, so every System tools action read "Unavailable action #1", "#2" (item 10).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.application.registry.builtin_standard_tools import (
    builtin_standard_tool_manifests,
    sync_builtin_standard_tools,
)
from core.application.registry.tool_manifest import ToolManifest
from core.entrypoints.admin import workflows_routes_read
from core.entrypoints.admin.workflows_connector_catalog import _registry_tool_connector_rows


class _RegistryStore:
    """Keeps what save_tool persists: the canonical registry payload."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def get_tool(self, tool_id: str):
        return self.rows.get(tool_id)

    def save_tool(self, payload: dict, author: str = "system") -> None:
        self.rows[payload["tool_id"]] = ToolManifest.model_validate(payload).to_registry_payload()

    def list_tools(self, enabled_only: bool = True) -> list[dict]:
        return list(self.rows.values())


def _synced_store() -> _RegistryStore:
    store = _RegistryStore()
    sync_builtin_standard_tools(store)
    return store


def _request(locale: str) -> SimpleNamespace:
    return SimpleNamespace(
        query_params={"locale": locale}, cookies={}, headers={}, state=SimpleNamespace()
    )


def _catalog(store: _RegistryStore, locale: str) -> list[dict]:
    with (
        patch.object(workflows_routes_read, "_request_scope", return_value=object()),
        patch.object(workflows_routes_read, "_get_db", return_value=store),
        patch.object(workflows_routes_read, "_list_connector_catalog_rows", return_value=[]),
        patch.object(workflows_routes_read, "_enforce_project_scope", return_value=""),
        patch.object(
            workflows_routes_read, "_list_project_connections_for_catalog", return_value=[]
        ),
        patch.object(workflows_routes_read, "_extension_action_connector_rows", return_value=[]),
    ):
        payload = workflows_routes_read.list_connector_catalog(_request(locale), "", locale)
    return payload["connectors"]


def test_saved_builtin_tools_keep_their_operation_names() -> None:
    store = _synced_store()

    for tool_id, manifest in builtin_standard_tool_manifests().items():
        saved = {fn["name"]: fn for fn in store.rows[tool_id]["interface"]["functions"]}
        for function in manifest["interface"]["functions"]:
            assert saved[function["name"]]["display_name"] == function["display_name"]
            assert saved[function["name"]]["description"] == function["description"]


def test_every_builtin_system_tool_action_has_a_readable_name() -> None:
    connectors = _catalog(_synced_store(), "en")

    builtin = {row["tool_id"]: row for row in connectors if row["source"] == "standard_tool_action"}
    assert set(builtin) == set(builtin_standard_tool_manifests())
    for tool_id, row in builtin.items():
        manifest = builtin_standard_tool_manifests()[tool_id]
        assert row["display_name"] == manifest["display_name"]
        declared = [function["name"] for function in manifest["interface"]["functions"]]
        assert [operation["operation_id"] for operation in row["operations"]] == declared
        for operation in row["operations"]:
            name = operation["display_name"].strip()
            assert name, operation
            assert name != operation["operation_id"], operation
            assert name not in {row["connector_id"], operation["runtime_connector_id"], tool_id}
            assert not name.lower().startswith("unavailable"), operation
            assert operation["description"].strip(), operation


def test_builtin_names_follow_the_ui_language() -> None:
    connectors = _catalog(_synced_store(), "lt")

    by_tool = {row["tool_id"]: row for row in connectors}
    assert by_tool["std.numbers"]["display_name"] == "Skaičiai"
    names = {op["operation_id"]: op["display_name"] for op in by_tool["std.text"]["operations"]}
    assert names["upper"] == "Didžiosios raidės"
    assert names["html_to_text"] == "HTML į tekstą"
    json_ops = {op["operation_id"]: op for op in by_tool["std.json"]["operations"]}
    assert json_ops["pick"]["display_name"] == "Paimti reikšmę"
    assert json_ops["pick"]["description"].startswith("Nuskaito")


@pytest.mark.parametrize("locale", ["en", "lt"])
def test_every_builtin_name_has_a_translation(locale: str) -> None:
    from core.application.settings.ui_i18n import reset_ui_i18n_cache, ui_messages_for_locale

    reset_ui_i18n_cache()
    messages = ui_messages_for_locale(locale)
    for manifest in builtin_standard_tool_manifests().values():
        for source in [manifest, *manifest["interface"]["functions"]]:
            for key in (source["label_key"], source["description_key"]):
                assert messages.get(key, "").strip(), key


def test_a_renamed_builtin_tool_keeps_the_admin_name() -> None:
    store = _synced_store()
    store.rows["std.json"] = {**store.rows["std.json"], "display_name": "Payload helpers"}

    rows = {row["tool_id"]: row for row in _registry_tool_connector_rows(store, locale="lt")}

    assert rows["std.json"]["display_name"] == "Payload helpers"
    assert rows["std.text"]["display_name"] == "Tekstas"


def test_a_registry_tool_operation_keeps_the_name_its_function_declares() -> None:
    db = MagicMock()
    db.list_tools.return_value = [
        ToolManifest.model_validate(
            {
                "tool_id": "acme.lookup",
                "display_name": "Acme lookup",
                "interface": {
                    "functions": [
                        {"name": "find", "display_name": "Find a record", "description": "Find."}
                    ]
                },
                "implementation": {"type": "internal", "module": "acme.tools"},
            }
        ).to_registry_payload()
    ]

    (row,) = _registry_tool_connector_rows(db, locale="en")

    assert row["operations"][0]["display_name"] == "Find a record"
    assert row["operations"][0]["description"] == "Find."


# This inventory may only shrink: a newly introduced action must be declared in
# a connector manifest or explicitly excluded with a product reason below.
_CONNECTOR_ACTION_GAPS: frozenset[str] = frozenset()
_CONNECTOR_ACTION_EXCLUSIONS = {
    "map.values": "Mapping policy belongs to transform/tabular configuration, not a system connector.",
}


def test_transform_catalog_cannot_outgrow_connector_manifests() -> None:
    from core.application.orchestration.transform_actions import list_transform_action_contracts

    manifests = builtin_standard_tool_manifests()
    missing = set()
    actions = list_transform_action_contracts()
    for action in actions:
        action_id = action["action_id"]
        if action_id in _CONNECTOR_ACTION_EXCLUSIONS:
            assert _CONNECTOR_ACTION_EXCLUSIONS[action_id].strip()
            continue
        functions = manifests.get(action["tool_id"], {}).get("interface", {}).get("functions", [])
        if action["tool_action_id"] not in {function["name"] for function in functions}:
            missing.add(action_id)
    # Exact equality detects both undocumented growth and stale gap entries.
    assert missing == _CONNECTOR_ACTION_GAPS
    assert set(_CONNECTOR_ACTION_EXCLUSIONS) <= {action["action_id"] for action in actions}


def test_number_action_token_matches_persisted_connector_identity() -> None:
    from core.application.orchestration.standard_tool_tokens import NUMBER

    assert NUMBER == "std.numbers"
    assert NUMBER in builtin_standard_tool_manifests()


def test_catalog_guard_rejects_an_unrecorded_action(monkeypatch) -> None:
    from core.application.orchestration import transform_actions

    original = transform_actions.list_transform_action_contracts
    monkeypatch.setattr(
        transform_actions,
        "list_transform_action_contracts",
        lambda: [
            *original(),
            {"action_id": "text.future", "tool_id": "std.text", "tool_action_id": "future"},
        ],
    )
    with pytest.raises(AssertionError):
        test_transform_catalog_cannot_outgrow_connector_manifests()


def test_tabular_nested_actions_have_an_explicit_exclusion() -> None:
    from core.application.orchestration.transform_actions import list_transform_action_contracts
    from core.application.tabular_feed.contracts import (
        TABULAR_ACTION_EXCLUSIONS,
        TABULAR_ACTION_PARAMS,
    )

    nested = {
        action["action_id"]
        for action in list_transform_action_contracts()
        if action["category"] in {"array", "json"}
    }
    assert nested == set(TABULAR_ACTION_EXCLUSIONS)
    assert not nested.intersection(TABULAR_ACTION_PARAMS)
    assert all(reason.strip() for reason in TABULAR_ACTION_EXCLUSIONS.values())


@pytest.mark.parametrize(
    ("operation", "payload", "expected"),
    [
        ("round", {"value": 0.125, "decimals": 2}, 0.12),
        ("upper", {"value": None}, ""),
        ("lower", {"value": None}, ""),
        ("length", {"value": [1, 2]}, 2),
        ("length", {"value": 17}, 0),
        ("stringify", {"value": {"b": 2, "a": 1}}, '{"a": 1, "b": 2}'),
        ("pick", {"value": {"items": ["first", "last"]}, "path": "items.-1"}, "last"),
        ("pick", {"value": {}, "path": "missing"}, None),
    ],
)
def test_saved_connector_operations_keep_their_behavior(operation, payload, expected) -> None:
    from core.application.orchestration.tools import standard_tools

    assert getattr(standard_tools, operation).invoke(payload) == {"result": expected}
