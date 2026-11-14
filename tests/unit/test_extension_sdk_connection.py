"""The SDK reads an operation's project connection through one accessor (SDK 0.5.0)."""

from __future__ import annotations

import pytest

from core.infrastructure.extension_sdk.public.flowsteward_extension_sdk import (
    RuntimeConnection,
    connection_from_payload,
)

SECRET = "tok-2b4d6f8h0j"  # pragma: allowlist secret
_CONNECTION = {
    "connection_id": "conn-1",
    "account_id": "acct-1",
    "project_id": "7",
    "display_name": "Production service",
    "connection_type": "service_profile",
    "environment": "production",
    "credential_revision": 3,
    "config": {"base_url": "https://service.example"},
    "secret_refs": {},
    "secrets": {"service_token": SECRET},
}
_EXPECTED = RuntimeConnection(
    connection_id="conn-1",
    connection_type="service_profile",
    display_name="Production service",
    environment="production",
    credential_revision=3,
    config={"base_url": "https://service.example"},
    secrets={"service_token": SECRET},
)


def test_a_query_reads_its_connection_from_the_envelope_root():
    payload = {
        "contract_version": "extension_host_v2",
        "mode": "query",
        "query": {"query_id": "runs.list"},
        "target": {"connection_id": "conn-1", "connection": dict(_CONNECTION)},
    }

    assert connection_from_payload(payload) == _EXPECTED


def test_an_action_reads_its_connection_from_the_action_target():
    payload = {
        "contract_version": "extension_host_v2",
        "mode": "action",
        "action": {
            "action_id": "runs.start",
            "target": {"connection_id": "conn-1", "connection": dict(_CONNECTION)},
        },
        # A root target is never an action's connection.
        "target": {"connection": {"connection_id": "conn-other"}},
    }

    assert connection_from_payload(payload) == _EXPECTED


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"mode": "query", "target": {}},
        {"mode": "query", "target": {"connection_id": "conn-1"}},
        {"mode": "query", "target": {"connection": {"connection_id": ""}}},
        {"mode": "action", "action": {"target": {}}},
        {"mode": "action", "action": {}, "target": {"connection": dict(_CONNECTION)}},
    ],
)
def test_no_resolved_connection_reads_as_none(payload):
    assert connection_from_payload(payload) is None


def test_the_repr_of_a_connection_never_shows_its_secrets():
    connection = connection_from_payload({"mode": "query", "target": {"connection": _CONNECTION}})

    assert connection is not None
    assert SECRET not in repr(connection)
    assert SECRET not in str(connection)
    assert connection.secrets["service_token"] == SECRET


def test_malformed_fields_read_as_empty_values():
    connection = connection_from_payload(
        {
            "mode": "query",
            "target": {
                "connection": {
                    "connection_id": "conn-1",
                    "credential_revision": "not-a-number",
                    "config": ["base_url"],
                    "secrets": "not-an-object",  # pragma: allowlist secret
                }
            },
        }
    )

    assert connection == RuntimeConnection(connection_id="conn-1")
