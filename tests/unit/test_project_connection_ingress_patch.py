"""`project_connection_ingress_patch`: declared fields, and nothing else, into `ingress_config`.

A messaging extension's inbound settings were routed to `project_connection_config_patch`,
which writes `connection_config`. The runtime reads inbound mode and polling
interval from `ingress_config`. The setting was saved where nobody read it.
"""

from __future__ import annotations

import copy
import json

import pytest

from core.application.extensions.project_connection_ingress import (
    IngressPatchRejected,
    patch_project_connection_ingress,
)

_DECLARED = {"fields": ["inbound_mode", "polling_interval_seconds"]}
_INGRESS_SCHEMA = {
    "type": "object",
    "properties": {
        "inbound_mode": {"type": "string", "enum": ["auto", "webhook", "polling"]},
        "polling_interval_seconds": {
            "type": "integer",
            "enum": [15, 30, 60, 120, 300],
        },
    },
    "additionalProperties": True,
}

_EXISTING = {
    "connection_id": "conn_17",
    "project_id": "proj_1",
    "extension_id": "acme.chat",
    "account_id": "acct_9",
    "connection_kind": "extension",
    "connection_type": "chat_bot",
    "display_name": "Ops bot",
    "credential_revision": 3,
    "extension_install_id": "install_4",
    "enabled": True,
    "connection_config": {"parse_mode": "HTML", "channels": [{"channel_id": "ops"}]},
    "secret_refs": {"refs": {"bot_token": "bot_token"}},
    "connection_routes": [{"route_id": "r1"}],
    "ingress_config": {
        "inbound_mode": "webhook",
        "polling_interval_seconds": 60,
        "webhook_status": "active",
        "last_update_id": 42,
        "discovered_chats": [{"chat_id": "-100123", "title": "Release Room"}],
    },
    "workflow_mappings": {"inbound": "wf_1"},
    "tags": ["prod"],
}


class _Store:
    def __init__(self, connection: dict | None) -> None:
        self.connection = copy.deepcopy(connection)
        self.writes: list[dict] = []

    def get_project_connection(self, *, project_id, extension_id="", **_kw):
        if self.connection is None:
            return None
        return copy.deepcopy(self.connection)

    def upsert_project_connection(self, **fields):
        self.writes.append(copy.deepcopy(fields))
        merged = dict(self.connection or {})
        merged.update(fields)
        if not merged.get("connection_id"):
            merged["connection_id"] = "conn_new"
        self.connection = merged
        return copy.deepcopy(merged)


def _patch(store: _Store, payload: dict, config: dict | None = None) -> dict:
    return patch_project_connection_ingress(
        store,
        project_id="proj_1",
        extension_id="acme.chat",
        config=_DECLARED if config is None else config,
        input_payload=payload,
        ingress_schema=_INGRESS_SCHEMA,
    )


def test_declared_fields_land_in_ingress_config_not_connection_config():
    store = _Store(_EXISTING)

    result = _patch(store, {"inbound_mode": "polling", "polling_interval_seconds": 30})

    assert store.connection["ingress_config"]["inbound_mode"] == "polling"
    assert store.connection["ingress_config"]["polling_interval_seconds"] == 30
    assert store.connection["connection_config"] == _EXISTING["connection_config"]
    assert result["values"] == {"inbound_mode": "polling", "polling_interval_seconds": 30}
    assert result["written_fields"] == ["inbound_mode", "polling_interval_seconds"]
    assert result["connection_id"] == "conn_17"


def test_runtime_owned_ingress_state_survives_the_patch():
    store = _Store(_EXISTING)

    _patch(store, {"inbound_mode": "polling"})

    ingress = store.connection["ingress_config"]
    assert ingress["webhook_status"] == "active"
    assert ingress["last_update_id"] == 42
    assert ingress["discovered_chats"] == [{"chat_id": "-100123", "title": "Release Room"}]
    assert ingress["polling_interval_seconds"] == 60


def test_undeclared_payload_keys_are_ignored_even_when_they_name_ingress_state():
    store = _Store(_EXISTING)

    _patch(store, {"inbound_mode": "polling", "webhook_status": "forged", "last_update_id": 0})

    ingress = store.connection["ingress_config"]
    assert ingress["webhook_status"] == "active"
    assert ingress["last_update_id"] == 42
    assert "forged" not in json.dumps(store.connection)


@pytest.mark.parametrize(
    "column",
    [
        "connection_id",
        "account_id",
        "connection_kind",
        "connection_type",
        "display_name",
        "credential_revision",
        "extension_install_id",
        "enabled",
        "connection_config",
        "secret_refs",
        "connection_routes",
        "workflow_mappings",
        "tags",
    ],
)
def test_every_other_connection_column_is_carried_across(column):
    store = _Store(_EXISTING)

    _patch(store, {"polling_interval_seconds": 120})

    (write,) = store.writes
    assert write[column] == _EXISTING[column]


def test_a_manifest_without_declared_fields_is_rejected_before_any_write():
    store = _Store(_EXISTING)

    with pytest.raises(IngressPatchRejected) as excinfo:
        _patch(store, {"inbound_mode": "polling"}, config={})

    assert excinfo.value.code == "ingress_fields_undeclared"
    assert store.writes == []
    assert store.connection == _EXISTING


def test_nothing_supplied_reports_current_values_without_writing():
    store = _Store(_EXISTING)

    result = _patch(store, {})

    assert store.writes == []
    assert result["values"] == {"inbound_mode": "webhook", "polling_interval_seconds": 60}
    assert result["written_fields"] == []


@pytest.mark.parametrize(
    "payload",
    [
        {"inbound_mode": "sometimes"},
        {"polling_interval_seconds": 31},
        {"polling_interval_seconds": "30"},
    ],
)
def test_invalid_ingress_values_are_rejected_before_writing(payload):
    store = _Store(_EXISTING)

    with pytest.raises(IngressPatchRejected) as excinfo:
        _patch(store, payload)

    assert excinfo.value.code == "invalid_payload"
    assert store.writes == []


def test_a_missing_connection_is_created_disabled_with_only_the_declared_fields():
    store = _Store(None)

    result = _patch(store, {"inbound_mode": "polling", "polling_interval_seconds": 30})

    (write,) = store.writes
    assert write["ingress_config"] == {"inbound_mode": "polling", "polling_interval_seconds": 30}
    assert write["enabled"] is False
    assert write["connection_kind"] == "extension"
    assert write["connection_config"] == {}
    assert write["secret_refs"] == {}
    assert result["connection_id"] == "conn_new"


def test_dotted_fields_write_nested_ingress_values():
    store = _Store(_EXISTING)

    result = _patch(store, {"polling.window": 5}, config={"fields": ["polling.window"]})

    assert store.connection["ingress_config"]["polling"] == {"window": 5}
    assert result["values"] == {"polling.window": 5}


# --- dispatcher ---------------------------------------------------------------


def _extension(config: dict | None = None) -> dict:
    return {
        "extension_key": "acme.chat",
        "runtime": {"contract_version": "extension_host_v2"},
        "bundle_root": "/tmp/extensions/acme_chat",
        "action_definitions": [
            {
                "action_id": "save_inbound_settings",
                "handler": {
                    "mode": "platform",
                    "operation": "project_connection_ingress_patch",
                    "config": _DECLARED if config is None else config,
                },
            }
        ],
    }


def _run(extension: dict, *, context: dict, payload: dict, logger):
    from core.application.extensions.platform_connection_operations import (
        install_platform_connection_operation,
    )
    from core.infrastructure.extension_runtime.extension_runtime import run_extension_action

    runtime_context = {"logger": logger}
    install_platform_connection_operation(runtime_context)
    return run_extension_action(
        extension,
        action_id="save_inbound_settings",
        context=context,
        input_payload=payload,
        runtime_context=runtime_context,
    )


def test_dispatcher_routes_the_operation_to_ingress_config_and_reloads_the_form():
    store = _Store(_EXISTING)

    result = _run(
        _extension(),
        context={"project_id": "proj_1"},
        payload={"inbound_mode": "polling", "polling_interval_seconds": 30},
        logger=store,
    )

    assert result["status"] == "succeeded", result
    response = result["response"]
    assert response["ok"] is True
    assert response["values"] == {"inbound_mode": "polling", "polling_interval_seconds": 30}
    assert "health_panel" in response["reload"]
    assert store.connection["ingress_config"]["inbound_mode"] == "polling"
    assert store.connection["connection_config"] == _EXISTING["connection_config"]


def test_dispatcher_fails_closed_without_a_project():
    store = _Store(_EXISTING)

    result = _run(_extension(), context={}, payload={"inbound_mode": "polling"}, logger=store)

    assert result["status"] == "failed"
    assert result["detail"] == "project_context_required"
    assert store.writes == []


def test_dispatcher_fails_closed_when_the_store_cannot_write_connections():
    class _ReadOnly:
        def get_project_connection(self, **_kw):
            return copy.deepcopy(_EXISTING)

    result = _run(
        _extension(),
        context={"project_id": "proj_1"},
        payload={"inbound_mode": "polling"},
        logger=_ReadOnly(),
    )

    assert result["status"] == "failed"
    assert result["detail"] == "project_connection_storage_unavailable"


def test_dispatcher_surfaces_the_rejection_code():
    store = _Store(_EXISTING)

    result = _run(
        _extension(config={}),
        context={"project_id": "proj_1"},
        payload={"inbound_mode": "polling"},
        logger=store,
    )

    assert result["status"] == "failed"
    assert result["detail"] == "ingress_fields_undeclared"
    assert store.writes == []
