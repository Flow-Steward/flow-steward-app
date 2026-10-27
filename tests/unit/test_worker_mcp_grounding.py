from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from core.application.orchestration.tools import mcp_tools


def _select_scalable_worker_environment(monkeypatch) -> None:
    monkeypatch.setenv("FS_DEPLOYMENT_PROFILE", "scalable")
    for name in tuple(os.environ):
        if name.startswith("FS_COMPACT_") or name == "FS_WORKER_PROCESSES":
            monkeypatch.delenv(name, raising=False)


def test_worker_compose_requires_a_dedicated_mcp_machine_token() -> None:
    local = yaml.safe_load(Path("infra/docker/docker-compose.yaml").read_text())
    production = yaml.safe_load(Path("infra/docker/docker-compose.prod.yaml").read_text())

    local_env = local["services"]["fs-worker"]["environment"]
    prod_env = production["services"]["fs-worker"]["environment"]
    assert local_env["MCP_BEARER_TOKEN"] == "${FS_WORKER_MCP_BEARER_TOKEN:-}"
    assert prod_env["MCP_BEARER_TOKEN"].startswith("${FS_WORKER_MCP_BEARER_TOKEN:?")
    assert local_env["FS_MCP_GROUNDING_REQUIRED"] == "1"
    assert prod_env["FS_MCP_GROUNDING_REQUIRED"] == "1"


def test_worker_grounding_preflight_requires_explicit_token(monkeypatch) -> None:
    monkeypatch.delenv("MCP_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("FS_SESSION_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="worker MCP machine token is required"):
        mcp_tools.verify_worker_mcp_grounding()


def test_worker_grounding_preflight_probes_projects_read_scope(monkeypatch) -> None:
    monkeypatch.setenv("MCP_BEARER_TOKEN", "worker-machine-token")

    with patch.object(mcp_tools, "_call_platform_mcp", return_value="{}") as call:
        mcp_tools.verify_worker_mcp_grounding()

    call.assert_called_once_with("project_list", {})


def test_worker_grounding_preflight_fails_closed_without_retrying_revoked_token(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MCP_BEARER_TOKEN", "revoked-worker-machine-token")

    with (
        patch.object(
            mcp_tools,
            "_call_platform_mcp",
            side_effect=RuntimeError("403 forbidden"),
        ) as call,
        patch.object(mcp_tools.time, "sleep") as sleep,
        pytest.raises(RuntimeError, match=r"revoked, invalid, or lacks projects\.read"),
    ):
        mcp_tools.verify_worker_mcp_grounding(retries=30, delay_seconds=1.0)

    call.assert_called_once_with("project_list", {})
    sleep.assert_not_called()


def test_compact_worker_grounding_retries_startup_auth_rejection(monkeypatch) -> None:
    monkeypatch.setenv("MCP_BEARER_TOKEN", "worker-machine-token")

    with (
        patch.object(
            mcp_tools,
            "_call_platform_mcp",
            side_effect=[RuntimeError("401 unauthorized"), "{}"],
        ) as call,
        patch.object(mcp_tools.time, "sleep") as sleep,
    ):
        mcp_tools.verify_worker_mcp_grounding(
            retries=30,
            delay_seconds=1.0,
            retry_rejected=True,
        )

    assert call.call_count == 2
    sleep.assert_called_once_with(1.0)


def test_scalable_worker_retries_transient_auth_rejection_before_queue_start(monkeypatch) -> None:
    from core.entrypoints.cli import worker_process

    _select_scalable_worker_environment(monkeypatch)
    monkeypatch.setenv("FS_MCP_GROUNDING_REQUIRED", "1")
    with (
        patch.object(worker_process, "verify_worker_mcp_grounding") as verify,
        patch.object(worker_process, "run_worker") as run,
    ):
        worker_process.main()

    verify.assert_called_once_with(retries=30, delay_seconds=1.0, retry_rejected=True)
    kwargs = run.call_args.kwargs
    assert kwargs["ensure_guardrails_seeded"] is worker_process.ensure_canonical_guardrails_seeded
    assert callable(kwargs["process_extension_response_envelope"])
    assert kwargs["maintenance_active"] is worker_process.public_submission_pause_active
    assert kwargs["curate_terminal_job"] is worker_process.curate_completed_job_to_memory


def test_compact_worker_retries_transient_auth_rejection_during_startup(monkeypatch) -> None:
    from core.entrypoints.cli import worker_process

    monkeypatch.setenv("FS_MCP_GROUNDING_REQUIRED", "1")
    monkeypatch.setenv("FS_DEPLOYMENT_PROFILE", "compact")
    with (
        patch.object(worker_process, "verify_worker_mcp_grounding") as verify,
        patch.object(worker_process, "run_worker"),
    ):
        worker_process.main()

    verify.assert_called_once_with(retries=30, delay_seconds=1.0, retry_rejected=True)


def test_worker_process_routes_inbound_extension_events_through_event_command_service(
    monkeypatch,
) -> None:
    from core.entrypoints.cli import worker_process

    _select_scalable_worker_environment(monkeypatch)
    monkeypatch.setenv("FS_MCP_GROUNDING_REQUIRED", "0")
    with (
        patch.object(worker_process, "_process_extension_response_envelope") as process,
        patch.object(worker_process, "run_worker") as run,
    ):
        worker_process.main()
        callback = run.call_args.kwargs["process_extension_response_envelope"]
        callback(
            {"status": "succeeded", "response": {"events": []}},
            {"extension_key": "acme.inbound-provider"},
            "acme.inbound-provider",
            audit={"project_id": "project-1"},
            bl=object(),
        )

    assert callable(process.call_args.kwargs.get("event_command_service_factory"))


def test_worker_event_ingress_composition_does_not_depend_on_http_runtime() -> None:
    paths = (
        Path("core/entrypoints/cli/worker_process.py"),
        Path("core/entrypoints/cli/worker_event_ingress.py"),
    )

    for path in paths:
        assert "http_app_" not in path.read_text(encoding="utf-8")


def test_worker_inbound_response_processor_is_not_used_by_normal_job_actions() -> None:
    worker_source = Path("core/infrastructure/runtime/worker/worker.py").read_text(encoding="utf-8")

    injection = (
        "inbound_poll_handle = _start_extension_inbound_poll_loop(\n"
        "        worker_id=wid,\n"
        "        project_id=args.project_id,\n"
        "        process_extension_response_envelope=process_extension_response_envelope,\n"
        "    )"
    )
    assert injection in worker_source
    assert (
        worker_source.count(
            "process_extension_response_envelope=process_extension_response_envelope"
        )
        == 1
    )


def test_worker_polling_passes_background_events_to_shared_envelope(monkeypatch) -> None:
    from core.entrypoints.cli import worker_process

    _select_scalable_worker_environment(monkeypatch)
    monkeypatch.setenv("FS_MCP_GROUNDING_REQUIRED", "0")
    with (
        patch.object(worker_process, "_process_extension_response_envelope") as process,
        patch.object(worker_process, "run_worker") as run,
    ):
        worker_process.main()
        callback = run.call_args.kwargs["process_extension_response_envelope"]
        callback(
            {
                "status": "succeeded",
                "response": {
                    "events": [{"event_type": "deployment.completed"}],
                    "host_mutations": [{"kind": "project_connection_upsert"}],
                },
            },
            {"extension_key": "acme.provider"},
            "acme.provider",
            audit={"project_id": "project-1"},
            bl=object(),
        )

    process.assert_called_once()


def test_worker_polling_rejects_unsupported_human_decision_action_before_state_mutation(
    monkeypatch,
) -> None:
    from core.entrypoints.cli import worker_process

    class Store:
        def get_job(self, _job_id):
            return {"meta": {"selected_workflow_id": "workflow-1"}}

        def list_workflow_reactions(self, _workflow_id, *, enabled_only):
            assert enabled_only is True
            return [
                {
                    "event_type": "notification.interaction_received",
                    "action": {"type": "notify"},
                }
            ]

    _select_scalable_worker_environment(monkeypatch)
    monkeypatch.setenv("FS_MCP_GROUNDING_REQUIRED", "0")
    with (
        patch.object(worker_process, "_process_extension_response_envelope") as process,
        patch.object(worker_process, "run_worker") as run,
    ):
        worker_process.main()
        callback = run.call_args.kwargs["process_extension_response_envelope"]
        _raises_input_197_1 = Store()
        with pytest.raises(ValueError, match="reaction action 'notify' is not supported"):
            callback(
                {
                    "status": "succeeded",
                    "response": {
                        "events": [
                            {"event_type": "notification.interaction_received", "job_id": "job-1"}
                        ],
                        "host_mutations": [{"kind": "project_connection_upsert"}],
                    },
                },
                {"extension_key": "acme.provider"},
                "acme.provider",
                audit={"project_id": "project-1"},
                bl=_raises_input_197_1,
            )

    process.assert_not_called()


def test_worker_polling_ignores_unsupported_rules_that_do_not_match_event(
    monkeypatch,
) -> None:
    from core.entrypoints.cli import worker_process

    class Store:
        def get_job(self, _job_id):
            return {"meta": {"selected_workflow_id": "workflow-1"}}

        def list_workflow_reactions(self, _workflow_id, *, enabled_only):
            assert enabled_only is True
            return [
                {
                    "reaction_key": "scope::group::other-group::notify",
                    "event_type": "notification.interaction_received",
                    "action": {"type": "notify"},
                },
                {
                    "event_type": "notification.interaction_received",
                    "conditions": {"payload.action": "reject"},
                    "action": {"type": "notify"},
                },
            ]

    _select_scalable_worker_environment(monkeypatch)
    monkeypatch.setenv("FS_MCP_GROUNDING_REQUIRED", "0")
    with (
        patch.object(worker_process, "_process_extension_response_envelope") as process,
        patch.object(worker_process, "run_worker") as run,
    ):
        worker_process.main()
        callback = run.call_args.kwargs["process_extension_response_envelope"]
        callback(
            {
                "status": "succeeded",
                "response": {
                    "events": [
                        {
                            "event_type": "notification.interaction_received",
                            "job_id": "job-1",
                            "payload": {"action": "approve"},
                        }
                    ]
                },
            },
            {"extension_key": "acme.provider"},
            "acme.provider",
            audit={"project_id": "project-1"},
            bl=Store(),
        )

    process.assert_called_once()


def test_grounding_failure_names_an_unreachable_server_instead_of_blaming_the_token(
    monkeypatch,
) -> None:
    """The diagnostic must not send the operator after a healthy credential.

    When fs-mcp refuses to start — for instance because its image revision does
    not match the deployment — the worker's probe never gets an answer. The
    message nevertheless claimed the machine token was "revoked, invalid, or
    lacks projects.read", so the visible symptom pointed at scopes and revocation
    while the real cause was a mismatched image pair. The underlying error was
    dropped entirely, leaving nothing to follow.
    """
    monkeypatch.setenv("MCP_BEARER_TOKEN", "a-perfectly-valid-token")

    with (
        patch.object(
            mcp_tools,
            "_call_platform_mcp",
            side_effect=RuntimeError("Connection refused by fs-mcp:8100"),
        ),
        patch.object(mcp_tools.time, "sleep"),
        pytest.raises(RuntimeError) as excinfo,
    ):
        mcp_tools.verify_worker_mcp_grounding(retries=2, delay_seconds=0.0)

    message = str(excinfo.value)
    assert "did not answer" in message
    assert "NOT rejected" in message
    assert "revoked" not in message, "a reachability failure must not be reported as a bad token"
    assert "Connection refused by fs-mcp:8100" in message, "underlying error was swallowed"


def test_grounding_failure_still_blames_the_token_when_it_is_actually_rejected(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MCP_BEARER_TOKEN", "revoked-worker-machine-token")

    with (
        patch.object(
            mcp_tools,
            "_call_platform_mcp",
            side_effect=RuntimeError("401 Unauthorized"),
        ),
        patch.object(mcp_tools.time, "sleep"),
        pytest.raises(RuntimeError) as excinfo,
    ):
        mcp_tools.verify_worker_mcp_grounding(retries=2, delay_seconds=0.0)

    message = str(excinfo.value)
    assert "rejected the worker machine token" in message
    assert "revoked, invalid, or lacks projects.read" in message
    assert "401 Unauthorized" in message
