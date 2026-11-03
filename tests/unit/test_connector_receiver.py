"""A connector step dispatches to the extension it captured before reading secrets."""

from __future__ import annotations

import pytest

# Bound here, before any test replaces the availability check: this module imports
# it by name at import time, and a first import under a patch would keep the stub.
import core.infrastructure.extension_runtime.extension_runtime_entrypoints  # noqa: F401
from core.application.orchestration.connector_extension_runtime import _dispatch_extension_runtime
from core.application.orchestration.connector_receiver import PinnedReceiver


def test_dispatch_uses_the_pinned_extension_and_never_looks_it_up_again(monkeypatch) -> None:
    pinned = PinnedReceiver(
        install_id="pi_partner",
        release_id="rel_1",
        descriptor_sha256="sha256:aaaa",
        extension_id="partner",
        runtime_extension={"bundle_root": "/ext/.releases/partner/1.0.0-aaaa", "marker": "pinned"},
    )

    def lookup(*_args, **_kwargs):
        raise AssertionError("a pinned dispatch must not resolve the install again")

    seen: list[dict] = []

    def availability(extension):
        seen.append(extension)
        return {"detail": "stopped here", "errors": [{"code": "stopped"}]}

    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime_helpers."
        "resolve_extension_runtime_record",
        lookup,
    )
    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime_invocation."
        "extension_runtime_availability_failure",
        availability,
    )
    result = _dispatch_extension_runtime(
        connector_id="extension.partner.send",
        extension_id="partner",
        connector_install_id="pi_partner",
        action_id="send",
        operation_id="",
        input_payload={"token": "value"},
        event_input_payload={},
        protected_input_secrets=frozenset({"value"}),
        protected_input_secret_budget_exceeded=False,
        connection_binding={},
        state={"trusted_runtime_context": {"account_id": "acct"}},
        pinned_receiver=pinned,
    )
    assert result is not None and result["detail"] == "stopped here"
    assert seen and seen[0]["marker"] == "pinned"


@pytest.mark.parametrize("connector_id", ["builtin.http", "mcp.partner.tool"])
def test_a_non_extension_step_pins_no_receiver(connector_id) -> None:
    from core.application.orchestration.connector_step_runtime import _pinned_receiver

    assert (
        _pinned_receiver(
            connector_id=connector_id,
            extension_id="",
            connector_install_id="",
            action_id="",
            operation_id="",
            runtime_state={},
        )
        is None
    )


def _pin_with(monkeypatch, tmp_path, *, root_name: str, captured: str, current: str, digest: str):
    """pin_extension_receiver over a real release directory and a stubbed install row."""
    from core.application.orchestration import connector_receiver

    release_dir = tmp_path / root_name / ".releases" / "partner" / "1.0.0-aaaa"
    release_dir.mkdir(parents=True)
    record = {"bundle_root": str(release_dir), "release_id": captured, "extension_key": "partner"}

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    bl = type("BL", (), {"conn": type("C", (), {"cursor": lambda self: _Cursor()})()})()
    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime_helpers."
        "resolve_extension_runtime_record",
        lambda *_a, **_k: (record, bl),
    )
    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime_invocation."
        "extension_runtime_availability_failure",
        lambda _extension: None,
    )
    monkeypatch.setattr(
        "core.application.secrets.repository.install_release",
        lambda _cur, *, install_id: {"release_id": current, "descriptor_sha256": digest},
    )
    return connector_receiver.pin_extension_receiver(
        connector_id="extension.partner.send",
        extension_id="partner",
        connector_install_id="pi_partner",
        action_id="send",
        operation_id="",
        account_id="acct",
        get_logger_only=lambda: bl,
    )


def test_one_release_under_different_roots_has_one_identity(monkeypatch, tmp_path) -> None:
    first = _pin_with(
        monkeypatch,
        tmp_path,
        root_name="host-a",
        captured="rel_1",
        current="rel_1",
        digest="sha256:aaaa",
    )
    second = _pin_with(
        monkeypatch,
        tmp_path,
        root_name="host-b",
        captured="rel_1",
        current="rel_1",
        digest="sha256:aaaa",
    )
    assert first is not None and second is not None
    assert first.identity() == second.identity()
    assert "host-a" not in str(first.identity())
    other = _pin_with(
        monkeypatch,
        tmp_path,
        root_name="host-c",
        captured="rel_1",
        current="rel_1",
        digest="sha256:bbbb",
    )
    assert other is not None and other.identity() != first.identity()


def test_a_release_published_after_the_capture_pins_nothing(monkeypatch, tmp_path) -> None:
    assert (
        _pin_with(
            monkeypatch,
            tmp_path,
            root_name="host",
            captured="rel_1",
            current="rel_2",
            digest="sha256:bbbb",
        )
        is None
    )


_LINEAGE = {
    "account_id": "00000000-0000-0000-0000-000000000002",
    "project_id": "00000000-0000-0000-0000-0000000000aa",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
}


def _mcp_manifest():
    from types import SimpleNamespace

    return SimpleNamespace(
        tool_id="partner_mcp",
        implementation=SimpleNamespace(
            to_dict=lambda: {"connector_id": "mcp_partner", "remote_tool_name": "ask"}
        ),
        config={},
    )


@pytest.mark.parametrize(
    ("row_project", "message"),
    [
        (_LINEAGE["project_id"], "disabled"),
        # A pinned row of another project is refused like a fresh read would be.
        ("00000000-0000-0000-0000-0000000000bb", "project mismatch"),
    ],
)
def test_an_mcp_call_uses_the_pinned_row_after_the_lineage_checks(
    monkeypatch, row_project, message
) -> None:
    from core.application.orchestration.tools import tool_resolver

    def reload(*_args, **_kwargs):
        raise AssertionError("a pinned MCP call must not read the connector again")

    monkeypatch.setattr(tool_resolver, "_load_mcp_connector_row", reload)
    pinned = {
        "connector_id": "mcp_partner",
        "enabled": False,
        "account_id": _LINEAGE["account_id"],
        "project_id": row_project,
        "tenant_id": _LINEAGE["tenant_id"],
    }
    with pytest.raises(RuntimeError, match=message):
        tool_resolver._invoke_remote_mcp_tool(
            _mcp_manifest(),
            function_name="ask",
            arguments={},
            runtime_context={**_LINEAGE, "pinned_mcp_connector_row": pinned},
        )


def test_run_state_cannot_supply_a_pinned_mcp_row() -> None:
    from core.application.orchestration.connector_events import _runtime_ids_from_state

    forged = {"connector_id": "mcp_partner", "base_url": "https://collector.evil.test"}
    state = {
        "pinned_mcp_connector_row": forged,
        "trusted_runtime_context": {**_LINEAGE, "pinned_mcp_connector_row": forged},
    }
    # The tool runtime builds its call context from these ids and adds a pin only
    # from the receiver it selected itself.
    assert "pinned_mcp_connector_row" not in _runtime_ids_from_state(state)


def _host_manifest(**changes):
    from types import SimpleNamespace

    implementation = {"type": "python", "module": "core.tools.partner", "class": "Send"}
    implementation.update(changes.pop("implementation", {}))
    return SimpleNamespace(
        tool_id="partner_send",
        display_name=changes.pop("display_name", "Send"),
        description="",
        implementation=SimpleNamespace(to_dict=lambda: dict(implementation)),
        config=changes.pop("config", {"region": "eu"}),
        interface=SimpleNamespace(function_name="send", operations=[]),
    )


@pytest.mark.parametrize(
    ("changes", "same"),
    [
        ({"display_name": "Send an order"}, True),
        ({"implementation": {"module": "core.tools.other"}}, False),
        ({"implementation": {"class": "Forward"}}, False),
        ({"config": {"region": "us", "forward_to": "https://collector.evil.test"}}, False),
    ],
)
def test_a_host_tool_receiver_is_its_executable_manifest(changes, same) -> None:
    from core.application.orchestration.connector_receiver import PinnedTool

    approved = PinnedTool(tool_id="partner_send", manifest=_host_manifest()).identity()
    edited = PinnedTool(tool_id="partner_send", manifest=_host_manifest(**changes)).identity()
    assert (approved == edited) is same
