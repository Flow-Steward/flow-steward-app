"""The platform-owned channel list operations a messaging extension's channel actions route to.

`upsert_channel`, `toggle_channel` and `delete_channel` are dispatched to
`project_connection_list_replace` / `_toggle` / `_delete`. These pins are the
parity coverage the platform path needs before the extension-side duplicates of
those actions are removed.
"""

from __future__ import annotations

import copy

import pytest

from core.application.extensions.platform_connection_operations import (
    install_platform_connection_operation,
)
from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

_CHANNELS = [
    {"channel_id": "ops", "display_name": "Ops", "chat_id": "-100123", "enabled": True},
    {"channel_id": "alerts", "display_name": "Alerts", "chat_id": "-100456", "enabled": True},
]

_EXISTING = {
    "connection_id": "conn_1",
    "project_id": "project_1",
    "extension_id": "acme.chat",
    "connection_kind": "extension",
    "enabled": True,
    "connection_config": {"parse_mode": "HTML", "channels": copy.deepcopy(_CHANNELS)},
    "secret_refs": {"refs": {"bot_token": "bot_token"}},
    "connection_routes": [{"route_id": "r1"}],
    "ingress_config": {"inbound_mode": "webhook"},
    "workflow_mappings": {"inbound": "wf_1"},
}


class _Store:
    def __init__(self) -> None:
        self.connection = copy.deepcopy(_EXISTING)
        self.writes = 0

    def get_project_connection(self, **_kw):
        return copy.deepcopy(self.connection)

    def upsert_project_connection(self, **payload):
        self.writes += 1
        self.connection = {**self.connection, **payload}
        return copy.deepcopy(self.connection)


def _extension(operation: str, config: dict) -> dict:
    return {
        "extension_key": "acme.chat",
        "runtime": {"contract_version": "extension_host_v2"},
        "bundle_root": "/tmp/extensions/acme_chat",
        "action_definitions": [
            {
                "action_id": "channel_action",
                "handler": {"mode": "platform", "operation": operation, "config": config},
            }
        ],
    }


_LIST = {"list_key": "channels", "item_key": "channel_id"}
_REPLACE = _extension(
    "project_connection_list_replace",
    {**_LIST, "duplicate_error": "duplicate_channel_id", "immutable_error": "immutable_channel_id"},
)
_TOGGLE = _extension(
    "project_connection_list_toggle",
    {**_LIST, "toggle_field": "enabled", "not_found_error": "channel_not_found"},
)
_DELETE = _extension(
    "project_connection_list_delete", {**_LIST, "not_found_error": "channel_not_found"}
)
_STRICT_REPLACE = {
    **copy.deepcopy(_REPLACE),
    "connection_types": [
        {
            "connection_type_id": "chat_bot",
            "config_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "parse_mode": {"type": "string"},
                    "channels": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["channel_id", "display_name", "chat_id", "enabled"],
                            "properties": {
                                "channel_id": {"type": "string", "minLength": 1},
                                "display_name": {"type": "string", "minLength": 1},
                                "chat_id": {"type": "string", "minLength": 1},
                                "enabled": {"type": "boolean"},
                            },
                        },
                    },
                },
            },
        }
    ],
}
_STRICT_REPLACE["action_definitions"][0]["handler"]["config"]["connection_type_id"] = "chat_bot"


def _run(
    extension: dict, store: _Store, *, payload: dict | None = None, target: dict | None = None
):
    runtime_context = {"logger": store}
    install_platform_connection_operation(runtime_context)
    return run_extension_action(
        extension,
        action_id="channel_action",
        context={"project_id": "project_1"},
        target=target,
        input_payload=payload,
        runtime_context=runtime_context,
    )


def _channels(store: _Store) -> list[dict]:
    return store.connection["connection_config"]["channels"]


# --- replace -----------------------------------------------------------------


def test_replace_stores_the_submitted_channel_list_and_keeps_the_rest_of_the_config():
    store = _Store()
    submitted = [{"channel_id": "release", "display_name": "Release", "chat_id": "-100789"}]

    result = _run(_REPLACE, store, payload={"channels": submitted})

    assert result["status"] == "succeeded", result
    assert _channels(store) == submitted
    assert store.connection["connection_config"]["parse_mode"] == "HTML"
    assert result["response"]["values"] == {"channels": submitted}


@pytest.mark.parametrize(
    "column", ["secret_refs", "connection_routes", "ingress_config", "workflow_mappings"]
)
def test_replace_carries_every_other_connection_column(column):
    store = _Store()

    _run(_REPLACE, store, payload={"channels": [{"channel_id": "release"}]})

    assert store.connection[column] == _EXISTING[column]


def test_replace_rejects_a_duplicate_channel_id_without_writing():
    store = _Store()

    result = _run(
        _REPLACE, store, payload={"channels": [{"channel_id": "ops"}, {"channel_id": "ops"}]}
    )

    assert result["status"] == "failed"
    assert result["detail"] == "duplicate_channel_id"
    assert store.writes == 0


def test_replace_rejects_renaming_a_channel_id_without_writing():
    store = _Store()
    renamed = {"channel_id": "ops_renamed", "_meta": {"original_channel_id": "ops"}}

    result = _run(_REPLACE, store, payload={"channels": [renamed]})

    assert result["status"] == "failed"
    assert result["detail"] == "immutable_channel_id"
    assert store.writes == 0


@pytest.mark.parametrize("payload", [{}, {"channels": []}, {"channels": [{"display_name": "x"}]}])
def test_replace_rejects_an_empty_or_unidentified_list(payload):
    store = _Store()

    result = _run(_REPLACE, store, payload=payload)

    assert result["status"] == "failed"
    assert result["detail"] == "invalid_payload"
    assert store.writes == 0


@pytest.mark.parametrize(
    "channel",
    [
        {"channel_id": "ops", "display_name": "Ops", "enabled": True},
        {
            "channel_id": "ops",
            "display_name": "Ops",
            "chat_id": "-100123",
            "enabled": "yes",
        },
        {
            "channel_id": "ops",
            "display_name": "Ops",
            "chat_id": "-100123",
            "enabled": True,
            "unexpected": "value",
        },
    ],
)
def test_replace_validates_the_final_config_schema_before_writing(channel):
    store = _Store()

    result = _run(_STRICT_REPLACE, store, payload={"channels": [channel]})

    assert result["status"] == "failed"
    assert result["detail"] == "invalid_payload"
    assert result["response"]["errors"][0]["code"] == "invalid_payload"
    assert store.writes == 0


def test_replace_uses_ui_metadata_for_immutability_but_does_not_persist_it():
    store = _Store()
    submitted = [
        {
            "channel_id": "ops",
            "display_name": "Updated Ops",
            "chat_id": "-100123",
            "enabled": True,
            "_meta": {"existing": True, "original_channel_id": "ops"},
        }
    ]

    result = _run(_STRICT_REPLACE, store, payload={"channels": submitted})

    assert result["status"] == "succeeded", result
    assert _channels(store) == [
        {
            "channel_id": "ops",
            "display_name": "Updated Ops",
            "chat_id": "-100123",
            "enabled": True,
        }
    ]


# --- toggle ------------------------------------------------------------------


def test_toggle_flips_only_the_selected_channel():
    store = _Store()

    result = _run(_TOGGLE, store, target={"channel_id": "alerts"})

    assert result["status"] == "succeeded", result
    by_id = {row["channel_id"]: row for row in _channels(store)}
    assert by_id["alerts"]["enabled"] is False
    assert by_id["ops"]["enabled"] is True
    assert all("_meta" not in row for row in _channels(store))
    assert store.writes == 1


def test_toggle_reports_an_unknown_channel_without_writing():
    store = _Store()

    result = _run(_TOGGLE, store, target={"channel_id": "nope"})

    assert result["status"] == "failed"
    assert result["detail"] == "channel_not_found"
    assert store.writes == 0


# --- delete ------------------------------------------------------------------


def test_delete_removes_only_the_selected_channel():
    store = _Store()

    result = _run(_DELETE, store, target={"channel_id": "ops"})

    assert result["status"] == "succeeded", result
    assert [row["channel_id"] for row in _channels(store)] == ["alerts"]
    assert all("_meta" not in row for row in _channels(store))
    assert result["response"]["values"] == {"channels": _channels(store)}


def test_delete_reports_an_unknown_channel_without_writing():
    store = _Store()

    result = _run(_DELETE, store, target={"channel_id": "nope"})

    assert result["status"] == "failed"
    assert result["detail"] == "channel_not_found"
    assert store.writes == 0
