"""Background polls filter by connection type and resolve credentials per connection.

A ``connection_poll`` action that declares ``connection_type_ids`` is called
only for those types; a connection with its own secrets gets them, an OAuth
connection its token; a connection that cannot be resolved is skipped before the
call; a connection whose poll mode was never switched on is not polled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.infrastructure.runtime.worker import worker_inbound_polling as polling

EXT = "fixture.items-poll"
ITEM_TYPES = ["items_user", "items_app"]

POLL_ACTION = {
    "action_id": "poll_items",
    "connection_type_ids": ITEM_TYPES,
    "handler": {"config": {"runtime_resources": {"project_connection": True}}},
    "contract": {
        "serialization": {"resource": "item_sync"},
        "background": {
            "kind": "connection_poll",
            "interval_field": "poll_interval_seconds",
            "mode_field": "sync_mode",
            "transport_policy": "webhook_preferred",
        },
    },
}

MODE_OFF_SCHEMA = {
    "type": "object",
    "properties": {"sync_mode": {"type": "string", "default": "off"}},
}

EXTENSION = {
    "extension_key": EXT,
    "extension_install_id": "install-1",
    "connection_types": [
        {"connection_type_id": "items_user", "ingress_schema": MODE_OFF_SCHEMA},
        {
            "connection_type_id": "items_app",
            "ingress_schema": MODE_OFF_SCHEMA,
            "secret_schema": {"type": "object", "required": ["client_secret"]},
        },
        {"connection_type_id": "files_user"},
        {"connection_type_id": "excel_user"},
    ],
    "action_definitions": [POLL_ACTION],
}


def _connection(connection_id: str, connection_type: str, **ingress: Any) -> dict[str, Any]:
    return {
        "connection_id": connection_id,
        "connection_type": connection_type,
        "project_id": "project-1",
        "account_id": "account-1",
        "extension_id": EXT,
        "extension_install_id": "install-1",
        "connection_kind": "extension",
        "state_version": 3,
        "config_generation": 2,
        "ingress_config": {"sync_mode": "polling", **ingress},
    }


@dataclass
class _Resolution:
    connection: dict[str, Any] = field(default_factory=dict)
    skip_reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.skip_reason


@dataclass
class _Harness:
    """Fake ports and runtime; records what reached the extension."""

    outcomes: dict[str, _Resolution] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    processed: list[dict[str, Any]] = field(default_factory=list)
    skips: list[tuple[str, str]] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    skip_until: dict[tuple[str, str], float] = field(default_factory=dict)

    def ports(self) -> polling.BackgroundPollPorts:
        from core.application.extensions.background_poll_connection import ingress_defaults

        return polling.BackgroundPollPorts(
            ingress_defaults=ingress_defaults,
            resolve_connection=self._resolve,
            record_skip=lambda _bl, **kw: self.skips.append((kw["connection_id"], kw["reason"])),
            clear_skip=lambda _bl, **_kw: None,
        )

    def _resolve(self, _bl, *, connection_row, **_kwargs):
        connection_id = connection_row["connection_id"]
        self.resolved.append(connection_id)
        return self.outcomes.get(connection_id) or _Resolution(
            connection={
                "connection_id": connection_id,
                "secrets": {"access_token": f"token-{connection_id}"},
                "state_version": 3,
                "config_generation": 2,
                "ingress_config": connection_row["ingress_config"],
            }
        )

    def _run(self, _extension, **kwargs):
        self.calls.append(kwargs)
        return {"status": "succeeded", "response": {"ok": True, "result": {}}}

    def poll(self, connection: dict[str, Any], *, extension=EXTENSION) -> None:
        polling._poll_connection_once(
            object(),
            worker_id="worker-test",
            extension=extension,
            extension_id=EXT,
            install_id="install-1",
            account_id="account-1",
            project_id="project-1",
            connection=connection,
            background_action=polling._background_poll_actions(extension)[0],
            run_extension_action=self._run,
            process_extension_response_envelope=lambda *a, **k: self.processed.append(k),
            skip_until=self.skip_until,
            ports=self.ports(),
        )


def test_background_action_declares_its_connection_types() -> None:
    [declaration] = polling._background_poll_actions(EXTENSION)
    assert declaration["connection_type_ids"] == ITEM_TYPES


def test_delegated_connection_polls_in_a_project_without_app_credentials() -> None:
    harness = _Harness()
    harness.poll(_connection("conn-user", "items_user"))
    [call] = harness.calls
    assert call["target"]["connection_id"] == "conn-user"
    assert call["target"]["connection"]["secrets"] == {"access_token": "token-conn-user"}
    [processed] = harness.processed
    assert processed["poll_context"] == {
        "connection_id": "conn-user",
        "state_version": 3,
        "config_generation": 2,
        "mode_field": "sync_mode",
        "mode_default": "off",
    }
    assert processed["audit"]["connection_id"] == "conn-user"


def test_files_and_excel_connections_cause_no_extension_call() -> None:
    harness = _Harness()
    harness.poll(_connection("conn-files", "files_user"))
    harness.poll(_connection("conn-excel", "excel_user"))
    assert harness.calls == []
    assert harness.resolved == []


def test_app_connection_without_secrets_is_skipped_with_status_others_still_poll() -> None:
    harness = _Harness(outcomes={"conn-app": _Resolution(skip_reason="connection_secrets_missing")})
    harness.poll(_connection("conn-app", "items_app"))
    harness.poll(_connection("conn-user", "items_user"))
    assert harness.skips == [("conn-app", "connection_secrets_missing")]
    assert [call["target"]["connection_id"] for call in harness.calls] == ["conn-user"]


def test_revoked_grant_is_not_called() -> None:
    harness = _Harness(outcomes={"conn-user": _Resolution(skip_reason="authorization_required")})
    harness.poll(_connection("conn-user", "items_user"))
    assert harness.calls == []
    assert harness.skips == [("conn-user", "authorization_required")]


def test_temporary_refresh_failure_skips_only_until_the_next_interval(monkeypatch) -> None:
    import time

    harness = _Harness(
        outcomes={"conn-user": _Resolution(skip_reason=polling.SKIP_REFRESH_UNAVAILABLE)}
    )
    connection = _connection("conn-user", "items_user", poll_interval_seconds=60)
    harness.poll(connection)
    assert harness.calls == []
    # No visible status: the connection keeps its state.
    assert harness.skips == []
    # Within the interval nothing is retried.
    harness.outcomes.clear()
    harness.poll(connection)
    assert harness.calls == []
    # After the interval the next poll goes ahead.
    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now + 61)
    harness.poll(connection)
    assert [call["target"]["connection_id"] for call in harness.calls] == ["conn-user"]


def test_new_connection_does_not_poll_until_switched_on() -> None:
    harness = _Harness()
    connection = _connection("conn-new", "items_app")
    connection["ingress_config"] = {}
    harness.poll(connection)
    assert harness.calls == []
    connection["ingress_config"] = {"sync_mode": "off"}
    harness.poll(connection)
    assert harness.calls == []
    connection["ingress_config"] = {"sync_mode": "polling"}
    harness.poll(connection)
    assert len(harness.calls) == 1


def test_missing_mode_without_declared_default_keeps_auto() -> None:
    declaration = polling._background_poll_actions(EXTENSION)[0]
    assert polling._binding_poll_due({"ingress_config": {}}, declaration) is True
    assert (
        polling._binding_poll_due({"ingress_config": {}}, declaration, mode_default="off") is False
    )


def test_typed_action_without_ports_is_never_called() -> None:
    calls: list[Any] = []
    polling._poll_connection_once(
        object(),
        worker_id="worker-test",
        extension=EXTENSION,
        extension_id=EXT,
        install_id="install-1",
        account_id="account-1",
        project_id="project-1",
        connection=_connection("conn-user", "items_user"),
        background_action=polling._background_poll_actions(EXTENSION)[0],
        run_extension_action=lambda *a, **k: calls.append(k) or {"status": "succeeded"},
        process_extension_response_envelope=lambda *a, **k: None,
        skip_until={},
        ports=None,
    )
    assert calls == []
