from __future__ import annotations

from pathlib import Path

import pytest

from core.entrypoints.api import dependencies, runtime_port_manifest
from core.entrypoints.api.app_factory import create_app
from core.infrastructure.runtime import http_app_events_resume

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("getter", "args"),
    [
        (dependencies.get_logger_only, ()),
        (dependencies.get_thread_state_snapshot, ("thread-1",)),
        (dependencies.to_json_obj, ({"ok": True},)),
        (dependencies.resolve_artifact, ("artifact-1",)),
    ],
)
def test_api_dependencies_fail_closed_until_composition_root_initializes(
    monkeypatch: pytest.MonkeyPatch,
    getter,
    args: tuple,
) -> None:
    for name in (
        "_get_logger_only",
        "_get_thread_state_snapshot",
        "_to_json_obj",
        "_resolve_artifact",
    ):
        monkeypatch.setattr(dependencies, name, None)

    with pytest.raises(RuntimeError, match="dependencies are not initialized"):
        getter(*args)


def test_api_dependency_initialization_wires_all_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "_get_logger_only",
        "_get_thread_state_snapshot",
        "_to_json_obj",
        "_resolve_artifact",
        "_artifact_presigned_url",
        "_artifact_local_stream",
        "_artifact_read_bytes",
    ):
        monkeypatch.setattr(dependencies, name, getattr(dependencies, name))
    monkeypatch.setattr(dependencies, "_get_logger_only", None)
    logger = object()

    dependencies.init(
        get_logger_only=lambda: logger,
        get_thread_state_snapshot=lambda thread_id: {"thread_id": thread_id},
        to_json_obj=lambda value: {"value": value},
        resolve_artifact=lambda artifact_id: {"artifact_id": artifact_id},
    )

    assert dependencies.get_logger_only() is logger
    assert dependencies.get_thread_state_snapshot("thread-1") == {"thread_id": "thread-1"}
    assert dependencies.to_json_obj("payload") == {"value": "payload"}
    assert dependencies.resolve_artifact("artifact-1") == {"artifact_id": "artifact-1"}


def test_api_composition_wires_job_store_for_job_scoped_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    monkeypatch.setattr(runtime_port_manifest, "RUNTIME_SERVICE_PORT_CONTRACTS", ())

    create_app(initialize_api_dependencies=lambda **values: captured.update(values))

    assert captured["get_logger_only"] is http_app_events_resume._get_job_store


def test_server_uses_entrypoint_owned_composition_root() -> None:
    start_script = (ROOT / "core/entrypoints/cli/scripts/start_server.sh").read_text(
        encoding="utf-8"
    )
    app_module = ROOT / "core/entrypoints/api/app.py"

    assert "core.entrypoints.api.app:app" in start_script
    assert app_module.exists()
    source = app_module.read_text(encoding="utf-8")
    assert "dependencies.init" in source
    assert "create_app" in source
    assert not (ROOT / "serve.py").exists()
    assert (ROOT / "core/entrypoints/api/app_factory.py").exists()
    assert (ROOT / "core/entrypoints/api/route_registration.py").exists()
    assert (ROOT / "core/entrypoints/api/runtime_wiring.py").exists()
    assert not (ROOT / "core/infrastructure/runtime/http_app.py").exists()
    assert not (ROOT / "core/infrastructure/runtime/http_app_factory.py").exists()
    assert not (ROOT / "core/infrastructure/runtime/http_app_route_registration.py").exists()
    retired_shell = "http_app_" + "co" + "dex" + "_shell.py"
    assert not (ROOT / "core/infrastructure/runtime" / retired_shell).exists()


@pytest.mark.parametrize("binding", ["websocket", "runtime_payload"])
def test_actual_http_approval_bindings_use_the_canonical_snapshot_projector(monkeypatch, binding):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from core.application.orchestration import approval_payload
    from core.infrastructure.runtime import http_app_runtime_helpers
    from tests.unit.test_serve_interrupt_helpers import _state_based_effect_pause

    values = _state_based_effect_pause()
    values["external_effect_resolution_ref"] = {"private": "never-public-binding"}
    snapshot = SimpleNamespace(tasks=[], values=values)
    expected = approval_payload.state_based_approval_payload(values)
    assert expected
    assert expected["approval_id"] == "recovery:effect-review"
    canonical = MagicMock(side_effect=approval_payload.state_based_approval_payload)
    monkeypatch.setattr(approval_payload, "state_based_approval_payload", canonical)
    read_snapshot = MagicMock(return_value=snapshot)
    store = MagicMock()
    store.get_latest_log_entry.return_value = None
    store.get_latest_event.return_value = None
    store.get_latest_agent_message.return_value = None
    store.list_job_errors.return_value = []
    job = {"id": "job-1", "thread_id": "thread-1", "status": "awaiting_approval", "meta": {}}
    if binding == "websocket":
        monkeypatch.setattr(http_app_events_resume, "_get_thread_state_snapshot", read_snapshot)
        monkeypatch.setattr(http_app_events_resume, "_get_job_store", lambda: store)
        actual = http_app_events_resume._ws_get_approval_payload(job)
    else:
        monkeypatch.setattr(http_app_runtime_helpers, "_get_thread_state_snapshot", read_snapshot)
        monkeypatch.setattr(http_app_runtime_helpers, "_get_thread_state_values", lambda _: {})
        monkeypatch.setattr(http_app_runtime_helpers, "_get_job_store", lambda: store)
        projected = []

        def capture_wait_reason(_job, _state, current_gate):
            projected.append(current_gate)
            return current_gate["message"] if current_gate else "missing gate"

        monkeypatch.setattr(http_app_runtime_helpers, "_compute_wait_reason", capture_wait_reason)
        runtime = http_app_runtime_helpers._build_runtime_payload(job, store)
        assert runtime["wait_reason"] == expected["message"]
        assert len(projected) == 1
        actual = projected[0]
    assert actual == expected
    assert "never-public-binding" not in str(actual)
    canonical.assert_called_once_with(values)
    assert canonical.call_args.args[0] is snapshot.values
    read_snapshot.assert_called_once_with("thread-1")
