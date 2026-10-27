"""Tests for CommandDispatchService — unified command execution path."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.command_models import (
    CommandBatchInput,
    CommandDispatchInput,
)
from core.application.management.command_services import CommandDispatchService
from core.application.management.errors import (
    ServiceConflictError,
    ServiceError,
    ServiceForbiddenError,
    ServiceNotFoundError,
    ServiceValidationError,
)
from core.application.management.scope import normalize_scope

# Job-lifecycle commands (pause/resume/cancel) now require operator-level authority.
_OPERATOR_SCOPE = normalize_scope(account_id=DEFAULT_ACCOUNT_ID, principal_type="service_account")


class _DispatchStore:
    """Canonical in-memory audit/job ports with explicit external-I/O doubles."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.audits: dict[str, dict[str, Any]] = {}
        self.get_job = MagicMock(side_effect=self.get_job)
        self.get_project = MagicMock(side_effect=self.get_project)
        self.update_job = MagicMock(side_effect=self.update_job)
        self.log_command_execution = MagicMock(side_effect=self.log_command_execution)
        self.update_command_execution = MagicMock(side_effect=self.update_command_execution)
        self.get_extension = MagicMock(return_value=None)
        self.get_project_connection = MagicMock(return_value=None)
        self.log_extension_invocation = MagicMock(return_value={})
        self.project_commands = _ProjectCommands()
        self.job_commands = _JobCommands(self)
        self.approval_commands = _ApprovalCommands()

    @staticmethod
    def cursor(**options: Any) -> Any:
        raise AssertionError("Unexpected unmocked database I/O in dispatch test")

    def get_job(self, job_id: str) -> dict[str, Any]:
        return dict(
            self.jobs.setdefault(
                str(job_id),
                {
                    "id": str(job_id),
                    "project_id": "11",
                    "account_id": DEFAULT_ACCOUNT_ID,
                    "thread_id": f"thread-{job_id}",
                    "status": "in_progress",
                },
            )
        )

    @staticmethod
    def get_project(project_id: str) -> dict[str, Any]:
        return {
            "id": str(project_id),
            "workspace_id": "workspace-default",
            "account_id": DEFAULT_ACCOUNT_ID,
        }

    def update_job(self, job_id: str, *, status: str) -> dict[str, Any]:
        self.jobs[str(job_id)]["status"] = status
        return dict(self.jobs[str(job_id)])

    def log_command_execution(self, **fields: Any) -> dict[str, Any]:
        assert "source" not in fields
        assert "correlation_id" not in fields
        self.audits[fields["command_id"]] = dict(fields)
        return dict(fields)

    def update_command_execution(
        self, *, command_id: str, status: str, result: dict[str, Any], error_text: str | None
    ) -> dict[str, Any]:
        row = self.audits[command_id]
        row.update(status=status, result=dict(result), error_text=error_text)
        return dict(row)


class _ProjectCommands:
    def __init__(self) -> None:
        self.enqueue_manual_task = MagicMock(side_effect=self.enqueue_manual_task)
        self.requests: list[tuple[Any, dict[str, Any]]] = []

    def enqueue_manual_task(self, command: Any, **options: Any) -> Any:
        self.requests.append((command, options))
        return SimpleNamespace(job_id="42", status="pending")


class _JobCommands:
    def __init__(self, store: _DispatchStore) -> None:
        self.store = store
        self.cancel_job = MagicMock(side_effect=self.cancel_job)

    def cancel_job(self, job_id: str, *, scope: Any) -> Any:
        self.store.update_job(job_id, status="cancelled")
        return SimpleNamespace(job_id=job_id, status="cancelled")


class _ApprovalCommands:
    def __init__(self) -> None:
        self.resume_task = MagicMock(side_effect=self.resume_task)
        self.decisions: list[dict[str, Any]] = []

    def resume_task(
        self, *, thread_id: str, scope: Any, decision: Any, idempotency_key: str = ""
    ) -> Any:
        self.decisions.append(
            {
                "thread_id": thread_id,
                "scope": scope,
                "decision": decision,
                "idempotency_key": idempotency_key,
            }
        )
        return SimpleNamespace(status="in_progress")


def _make_service():
    store = _DispatchStore()
    return CommandDispatchService(
        get_logger=lambda: store,
        get_extension_store=lambda: store,
        project_command_service_factory=lambda: store.project_commands,
        job_command_service_factory=lambda: store.job_commands,
        approval_command_service_factory=lambda: store.approval_commands,
    ), store


class TestCommandDispatch:
    _REMOVED_PROVIDER_STATUS_ACTION = "status_" + "update_" + "recorded"

    def test_dispatch_resume_job(self):
        svc, bl = _make_service()
        inp = CommandDispatchInput(
            command_type="resume_job",
            target={"job_id": 42},
            payload={"comment": "go"},
        )
        result = svc.dispatch(inp, scope=_OPERATOR_SCOPE)
        assert result.status == "executed"
        assert result.command_type == "resume_job"
        assert result.command_id.startswith("cmd_")
        bl.update_job.assert_called_once_with("42", status="in_progress")
        assert bl.jobs["42"]["status"] == "in_progress"

    def test_dispatch_pause_job(self):
        svc, bl = _make_service()
        inp = CommandDispatchInput(command_type="pause_job", target={"job_id": 7})
        result = svc.dispatch(inp, scope=_OPERATOR_SCOPE)
        assert result.status == "executed"
        bl.update_job.assert_called_once_with("7", status="paused")
        assert bl.jobs["7"]["status"] == "paused"

    def test_dispatch_cancel_job(self):
        svc, bl = _make_service()
        inp = CommandDispatchInput(command_type="cancel_job", target={"job_id": 10})
        result = svc.dispatch(inp, scope=_OPERATOR_SCOPE)
        assert result.status == "executed"
        bl.job_commands.cancel_job.assert_called_once_with("10", scope=_OPERATOR_SCOPE)
        assert bl.jobs["10"]["status"] == "cancelled"

    def test_dispatch_job_lifecycle_requires_operator_role(self):
        svc, bl = _make_service()
        non_operator = normalize_scope(account_id=DEFAULT_ACCOUNT_ID, principal_type="user")
        for command_type in ("pause_job", "resume_job", "cancel_job"):
            inp = CommandDispatchInput(command_type=command_type, target={"job_id": 7})
            with pytest.raises(ServiceForbiddenError):
                svc.dispatch(inp, scope=non_operator)
        bl.update_job.assert_not_called()
        bl.job_commands.cancel_job.assert_not_called()

    def test_dispatch_cancel_job_rejects_other_account_job(self):
        svc, bl = _make_service()
        bl.get_job.side_effect = None
        bl.get_job.return_value = {"id": "10", "project_id": "11", "account_id": "account-b"}
        inp = CommandDispatchInput(command_type="cancel_job", target={"job_id": 10})

        with pytest.raises(ServiceForbiddenError):
            svc.dispatch(inp, scope=_OPERATOR_SCOPE)

        bl.update_job.assert_not_called()
        bl.job_commands.cancel_job.assert_not_called()

    def test_dispatch_unknown_type_rejected(self):
        svc, _ = _make_service()
        inp = CommandDispatchInput(command_type="destroy_everything")
        with pytest.raises(ServiceValidationError, match="Unknown command_type"):
            svc.dispatch(inp, scope=None)

    def test_dispatch_empty_type_rejected(self):
        svc, _ = _make_service()
        inp = CommandDispatchInput(command_type="")
        with pytest.raises(ServiceValidationError, match="command_type is required"):
            svc.dispatch(inp, scope=None)

    def test_dispatch_start_workflow_executes(self):
        svc, bl = _make_service()
        inp = CommandDispatchInput(
            command_type="start_workflow",
            target={"workflow_id": "standard"},
        )
        result = svc.dispatch(inp, scope=None)
        assert result.status == "executed"
        assert result.result.get("action") == "started"
        assert result.result.get("job_id") == "42"
        bl.project_commands.enqueue_manual_task.assert_called_once()
        assert bl.project_commands.requests[0][0].workflow_id == "standard"

    def test_dispatch_start_workflow_runs_availability_gate(self):
        bl = _DispatchStore()
        bl.get_project.return_value = {"id": "11", "account_id": DEFAULT_ACCOUNT_ID}
        gate = MagicMock()
        svc = CommandDispatchService(
            get_logger=lambda: bl,
            workflow_availability_assert=gate,
            project_command_service_factory=lambda: bl.project_commands,
        )
        inp = CommandDispatchInput(
            command_type="start_workflow",
            target={"workflow_id": "standard", "project_id": "11"},
            payload={"account_id": "acc-11"},
        )
        result = svc.dispatch(inp, scope=None)
        assert result.status == "executed"
        gate.assert_called_once_with(
            workflow_id="standard",
            account_id="acc-11",
            project_id="11",
        )

    def test_dispatch_start_workflow_rejects_other_account_project(self):
        svc, bl = _make_service()
        bl.get_project.side_effect = None
        bl.get_project.return_value = {"id": "11", "account_id": "account-b"}
        inp = CommandDispatchInput(
            command_type="start_workflow",
            target={"workflow_id": "standard", "project_id": "11"},
        )

        with pytest.raises(ServiceForbiddenError):
            svc.dispatch(inp, scope=None)

        bl.project_commands.enqueue_manual_task.assert_not_called()

    def test_dispatch_start_workflow_rejects_disallowed_workflow_scope(self):
        svc, bl = _make_service()
        inp = CommandDispatchInput(
            command_type="start_workflow",
            target={"workflow_id": "blocked-wf"},
        )

        _raises_input_174_1 = normalize_scope(
            account_id=DEFAULT_ACCOUNT_ID, allowed_workflow_ids=frozenset({"allowed-wf"})
        )
        with pytest.raises(ServiceForbiddenError):
            svc.dispatch(inp, scope=_raises_input_174_1)

        bl.project_commands.enqueue_manual_task.assert_not_called()

    def test_dispatch_enqueue_followup_rejects_disallowed_workflow_scope(self):
        svc, bl = _make_service()
        inp = CommandDispatchInput(
            command_type="enqueue_followup_job",
            target={"workflow_id": "blocked-wf"},
        )

        _raises_input_192_1 = normalize_scope(
            account_id=DEFAULT_ACCOUNT_ID, allowed_workflow_ids=frozenset({"allowed-wf"})
        )
        with pytest.raises(ServiceForbiddenError):
            svc.dispatch(inp, scope=_raises_input_192_1)

        bl.project_commands.enqueue_manual_task.assert_not_called()

    def test_dispatch_start_workflow_raises_when_gate_blocks(self):
        bl = _DispatchStore()
        gate = MagicMock(side_effect=ServiceConflictError("blocked"))
        svc = CommandDispatchService(
            get_logger=lambda: bl,
            workflow_availability_assert=gate,
            project_command_service_factory=lambda: bl.project_commands,
        )
        inp = CommandDispatchInput(
            command_type="start_workflow",
            target={"workflow_id": "standard"},
        )
        with pytest.raises(ServiceConflictError):
            svc.dispatch(inp, scope=None)

    def test_dispatch_writes_audit_log(self):
        svc, bl = _make_service()
        inp = CommandDispatchInput(command_type="cancel_job", target={"job_id": 1})
        svc.dispatch(inp, scope=_OPERATOR_SCOPE)
        bl.log_command_execution.assert_called_once()
        call_kwargs = bl.log_command_execution.call_args
        assert call_kwargs[1]["command_type"] == "cancel_job"
        assert call_kwargs[1]["status"] == "accepted"

    def test_dispatch_batch_processes_all(self):
        svc, _ = _make_service()
        inp = CommandBatchInput(
            commands=[
                CommandDispatchInput(command_type="pause_job", target={"job_id": 1}),
                CommandDispatchInput(command_type="resume_job", target={"job_id": 2}),
                CommandDispatchInput(command_type="cancel_job", target={"job_id": 3}),
            ]
        )
        result = svc.dispatch_batch(inp, scope=_OPERATOR_SCOPE)
        assert len(result.results) == 3
        assert all(r.status == "executed" for r in result.results)

    def test_dispatch_batch_handles_errors_gracefully(self):
        svc, _ = _make_service()
        inp = CommandBatchInput(
            commands=[
                CommandDispatchInput(command_type="resume_job", target={"job_id": 1}),
                CommandDispatchInput(command_type="unknown_bad"),
            ]
        )
        result = svc.dispatch_batch(inp, scope=_OPERATOR_SCOPE)
        assert len(result.results) == 2
        assert result.results[0].status == "executed"
        assert result.results[1].status == "failed"

    def test_dispatch_pause_requires_job_id(self):
        svc, _ = _make_service()
        inp = CommandDispatchInput(command_type="pause_job", target={})
        with pytest.raises(ServiceValidationError, match="job_id"):
            svc.dispatch(inp, scope=_OPERATOR_SCOPE)

    def test_dispatch_invoke_extension_action_requires_ids(self):
        svc, _ = _make_service()
        inp = CommandDispatchInput(
            command_type="invoke_extension_action",
            target={"extension_id": "ext_1"},  # missing action_id
        )
        with pytest.raises(ServiceValidationError, match="action_id"):
            svc.dispatch(inp, scope=None)

    def test_dispatch_invoke_extension_action_executes(self):
        svc, bl = _make_service()
        bl.get_extension.return_value = {
            "extension_key": "test_ext",
            "account_id": DEFAULT_ACCOUNT_ID,
            "enabled": True,
            "compiled_manifest": {"supported_events": []},
        }
        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action"
        ) as mock_run:
            mock_run.return_value = {
                "status": "succeeded",
                "response": {"ok": True, "result": {"done": True}},
            }
            inp = CommandDispatchInput(
                command_type="invoke_extension_action",
                target={"extension_id": "ext_1", "action_id": "do_thing"},
            )
            result = svc.dispatch(inp, scope=None)
            assert result.status == "executed"
            assert result.result.get("action") == "executed"
            mock_run.assert_called_once()

    def test_dispatch_external_effect_action_resolves_connection_server_side(self):
        svc, bl = _make_service()
        bl.get_extension.return_value = {
            "extension_key": "example.host-fixture",
            "extension_install_id": "install-1",
            "account_id": DEFAULT_ACCOUNT_ID,
            "enabled": True,
            "action_definitions": [{"action_id": "publish_record"}],
            "compiled_manifest": {
                "external_effects": [
                    {
                        "operation_id": "publish_record",
                        "effect_kind": "message_delivery",
                        "channel": "api",
                        "idempotency": "required",
                        "test_mode_behavior": "suppress",
                    }
                ]
            },
        }
        bl.get_project_connection.return_value = {
            "connection_id": "conn_external",
            "account_id": DEFAULT_ACCOUNT_ID,
            "project_id": "7",
            "extension_id": "example.host-fixture",
            "enabled": True,
            "display_name": "External API",
            "connection_type": "api_token",
            "environment": "production",
            "credential_revision": 3,
            "connection_config": {
                "provider": "example_api",
                "client_id": "api-client",
                "endpoint": "https://api.example.com/records",
            },
            "secret_refs": {"api_token": "project_connection:conn_external:api_token"},
        }
        claim_result = {
            "declared": True,
            "should_spawn": True,
            "effect": {"effect_id": "eff_cmd_1", "status": "claimed"},
        }
        with (
            patch(
                "core.application.management.project_connection_secrets.resolve_connection_secrets",
                return_value={"api_token": "api-secret"},
            ),
            patch(
                "core.application.extensions.external_effects.claim_external_effect_for_action",
                return_value=claim_result,
            ) as claim,
            patch("core.application.extensions.external_effects.mark_external_effect_running"),
            patch(
                "core.application.extensions.external_effects.confirm_external_effect_from_invocation",
                return_value={
                    "effect_id": "eff_cmd_1",
                    "status": "succeeded",
                    "__confirmed_replay_invocation": {
                        "status": "succeeded",
                        "response": {"ok": True},
                    },
                },
            ),
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_action"
            ) as mock_run,
        ):
            mock_run.return_value = {"status": "succeeded", "response": {"ok": True}}
            result = svc.dispatch(
                CommandDispatchInput(
                    command_type="invoke_extension_action",
                    correlation_id="command:publish-record:record-a",
                    target={
                        "extension_id": "example.host-fixture",
                        "action_id": "publish_record",
                        "project_id": "7",
                        "connection_id": "conn_external",
                    },
                    payload={
                        "record_id": "record-a",
                        "title": "Hello",
                        "content": "Body",
                    },
                ),
                scope=normalize_scope(account_id=DEFAULT_ACCOUNT_ID, project_id="7"),
            )

        assert result.status == "executed"
        claim.assert_called_once()
        assert claim.call_args.kwargs["project_id"] == "7"
        assert claim.call_args.kwargs["connection_id"] == "conn_external"
        mock_run.assert_called_once()
        target = mock_run.call_args.kwargs["target"]
        assert target["connection_id"] == "conn_external"
        assert target["connection"]["config"]["endpoint"] == "https://api.example.com/records"
        assert target["connection"]["secrets"] == {"api_token": "api-secret"}
        assert "api_token" not in target["connection"]["config"]
        runtime_context = mock_run.call_args.kwargs["runtime_context"]
        assert runtime_context["external_effect_id"] == "eff_cmd_1"
        assert runtime_context["connection_id"] == "conn_external"

    def test_dispatch_external_effect_action_requires_durable_caller_identity(self):
        svc, bl = _make_service()
        bl.get_extension.return_value = {
            "extension_key": "example.host-fixture",
            "extension_install_id": "install-1",
            "account_id": DEFAULT_ACCOUNT_ID,
            "enabled": True,
            "action_definitions": [{"action_id": "publish_record"}],
            "compiled_manifest": {
                "external_effects": [
                    {
                        "operation_id": "publish_record",
                        "effect_kind": "message_delivery",
                        "channel": "api",
                        "idempotency": "required",
                    }
                ]
            },
        }
        bl.get_project_connection.return_value = {
            "connection_id": "conn-external",
            "account_id": DEFAULT_ACCOUNT_ID,
            "project_id": "7",
            "extension_id": "example.host-fixture",
            "enabled": True,
            "connection_config": {},
            "secret_refs": {},
        }
        _raises_input_422_1 = CommandDispatchInput(
            command_type="invoke_extension_action",
            target={
                "extension_id": "example.host-fixture",
                "action_id": "publish_record",
                "project_id": "7",
                "connection_id": "conn-external",
            },
            payload={"record_id": "record-a"},
        )
        _raises_input_422_2 = normalize_scope(account_id=DEFAULT_ACCOUNT_ID, project_id="7")
        with (
            patch(
                "core.application.management.project_connection_secrets.resolve_connection_secrets",
                return_value={},
            ),
            patch(
                "core.application.extensions.external_effects.claim_external_effect_for_action"
            ) as claim,
            pytest.raises(ServiceValidationError, match=r"durable.*correlation"),
        ):
            svc.dispatch(_raises_input_422_1, scope=_raises_input_422_2)

        claim.assert_not_called()

    def test_dispatch_external_effect_duplicate_returns_hydrated_result_without_spawn(self):
        svc, bl = _make_service()
        bl.get_extension.return_value = {
            "extension_key": "example.host-fixture",
            "account_id": DEFAULT_ACCOUNT_ID,
            "enabled": True,
            "action_definitions": [{"action_id": "publish_record"}],
        }
        claim = {
            "declared": True,
            "should_spawn": False,
            "effect": {"effect_id": "eff_cmd_1", "status": "succeeded"},
        }
        replay_invocation = {
            "status": "succeeded",
            "response": {"ok": True, "result": {"message_id": "message-1"}},
        }
        with (
            patch(
                "core.application.extensions.external_effects.claim_external_effect_for_action",
                return_value=claim,
            ),
            patch(
                "core.application.extensions.external_effect_replay.hydrate_external_effect_claim",
                return_value={**claim, "replay_invocation": replay_invocation},
            ),
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_action"
            ) as provider,
        ):
            result = svc.dispatch(
                CommandDispatchInput(
                    command_type="invoke_extension_action",
                    target={
                        "extension_id": "example.host-fixture",
                        "action_id": "publish_record",
                    },
                ),
                scope=normalize_scope(account_id=DEFAULT_ACCOUNT_ID),
            )

        assert result.result["invocation"] == replay_invocation
        assert result.result["status"] == "succeeded"
        provider.assert_not_called()

    @pytest.mark.parametrize(
        "error_code",
        [
            "external_effect_replay_missing",
            "external_effect_replay_corrupt",
            "external_effect_replay_expired",
        ],
    )
    def test_dispatch_external_effect_duplicate_typed_failure_never_spawns(
        self,
        error_code,
    ):
        svc, bl = _make_service()
        bl.get_extension.return_value = {
            "extension_key": "example.host-fixture",
            "account_id": DEFAULT_ACCOUNT_ID,
            "enabled": True,
            "action_definitions": [{"action_id": "publish_record"}],
        }
        claim = {
            "declared": True,
            "should_spawn": False,
            "effect": {"effect_id": "eff_cmd_1", "status": "succeeded"},
        }
        _raises_input_517_1 = CommandDispatchInput(
            command_type="invoke_extension_action",
            target={"extension_id": "example.host-fixture", "action_id": "publish_record"},
        )
        _raises_input_517_2 = normalize_scope(account_id=DEFAULT_ACCOUNT_ID)
        with (
            patch(
                "core.application.extensions.external_effects.claim_external_effect_for_action",
                return_value=claim,
            ),
            patch(
                "core.application.extensions.external_effect_replay.hydrate_external_effect_claim",
                return_value={**claim, "error": error_code},
            ),
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_action"
            ) as provider,
            pytest.raises(ServiceError, match=error_code),
        ):
            svc.dispatch(_raises_input_517_1, scope=_raises_input_517_2)

        provider.assert_not_called()

    def test_dispatch_external_effect_persist_failure_never_returns_success(self):
        svc, bl = _make_service()
        bl.get_extension.return_value = {
            "extension_key": "example.host-fixture",
            "account_id": DEFAULT_ACCOUNT_ID,
            "enabled": True,
            "action_definitions": [{"action_id": "publish_record"}],
        }
        _raises_input_552_1 = CommandDispatchInput(
            command_type="invoke_extension_action",
            target={"extension_id": "example.host-fixture", "action_id": "publish_record"},
        )
        _raises_input_552_2 = normalize_scope(account_id=DEFAULT_ACCOUNT_ID)
        with (
            patch(
                "core.application.extensions.external_effects.claim_external_effect_for_action",
                return_value={
                    "declared": True,
                    "should_spawn": True,
                    "effect": {
                        "effect_id": "eff_cmd_1",
                        "status": "claimed",
                        "claim_token": "claim-1",
                    },
                },
            ),
            patch(
                "core.application.extensions.external_effects.mark_external_effect_running",
                return_value={"effect_id": "eff_cmd_1", "status": "running"},
            ),
            patch(
                "core.application.extensions.external_effects.confirm_external_effect_from_invocation",
                side_effect=RuntimeError("persistence unavailable"),
            ),
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
                return_value={"status": "succeeded", "response": {"ok": True}},
            ) as provider,
            pytest.raises(ServiceError, match="external_effect_replay_persist_failed"),
        ):
            svc.dispatch(_raises_input_552_1, scope=_raises_input_552_2)

        provider.assert_called_once()

    def test_dispatch_external_effect_provider_exception_is_confirmed_once_and_deduped(self):
        svc, bl = _make_service()
        bl.get_extension.return_value = {
            "extension_key": "example.host-fixture",
            "account_id": DEFAULT_ACCOUNT_ID,
            "enabled": True,
            "action_definitions": [{"action_id": "publish_record"}],
        }
        spawned_claim = {
            "declared": True,
            "should_spawn": True,
            "effect": {
                "effect_id": "eff_cmd_1",
                "status": "claimed",
                "claim_token": "claim-1",
            },
        }
        duplicate_claim = {
            "declared": True,
            "should_spawn": False,
            "effect": {"effect_id": "eff_cmd_1", "status": "timeout_unknown"},
        }
        with (
            patch(
                "core.application.extensions.external_effects.claim_external_effect_for_action",
                side_effect=[spawned_claim, duplicate_claim],
            ),
            patch(
                "core.application.extensions.external_effects.mark_external_effect_running",
                return_value={"effect_id": "eff_cmd_1", "status": "running"},
            ),
            patch(
                "core.application.extensions.external_effects.confirm_external_effect_from_invocation",
                return_value={"effect_id": "eff_cmd_1", "status": "timeout_unknown"},
            ) as confirm,
            patch(
                "core.application.extensions.external_effect_replay.hydrate_external_effect_claim",
                side_effect=lambda _db, result: result,
            ),
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
                side_effect=RuntimeError("provider transport exploded"),
            ) as provider,
        ):
            request = CommandDispatchInput(
                command_type="invoke_extension_action",
                target={
                    "extension_id": "example.host-fixture",
                    "action_id": "publish_record",
                },
                correlation_id="provider-exception-retry-1",
            )
            _raises_input_644_1 = normalize_scope(account_id=DEFAULT_ACCOUNT_ID)
            with pytest.raises(
                ServiceError,
                match="external_effect_provider_invocation_exception",
            ):
                svc.dispatch(request, scope=_raises_input_644_1)
            replayed = svc.dispatch(
                request,
                scope=normalize_scope(account_id=DEFAULT_ACCOUNT_ID),
            )

        provider.assert_called_once()
        confirm.assert_called_once()
        assert (
            confirm.call_args.kwargs["invocation"]["detail"]
            == "external_effect_provider_invocation_exception"
        )
        assert replayed.result["action"] == "deduped"
        assert replayed.result["status"] == "timeout_unknown"
        assert "invocation" not in replayed.result

    def test_dispatch_external_effect_command_intent_distinguishes_new_calls_and_retries(self):
        svc, bl = _make_service()
        bl.get_extension.return_value = {
            "extension_key": "example.host-fixture",
            "account_id": DEFAULT_ACCOUNT_ID,
            "enabled": True,
            "action_definitions": [{"action_id": "publish_record"}],
        }
        seen: dict[str, dict] = {}
        overrides: list[str] = []

        def claim(*_args, **kwargs):
            override = kwargs["idempotency_key_override"]
            payload = dict(kwargs["request_payload"])
            overrides.append(override)
            if override in seen and seen[override] != payload:
                return {
                    "declared": True,
                    "should_spawn": False,
                    "effect": {"effect_id": f"effect-{len(seen)}", "status": "succeeded"},
                    "error": "external_effect_idempotency_conflict",
                }
            first = override not in seen
            seen[override] = payload
            return {
                "declared": True,
                "should_spawn": first,
                "effect": {
                    "effect_id": f"effect-{len(seen)}",
                    "status": "claimed" if first else "succeeded",
                    "claim_token": "claim-1" if first else "",
                },
            }

        replay_invocation = {
            "status": "succeeded",
            "response": {"ok": True, "result": {"message_id": "replayed"}},
        }
        with (
            patch(
                "core.application.extensions.external_effects.claim_external_effect_for_action",
                side_effect=claim,
            ),
            patch(
                "core.application.extensions.external_effects.mark_external_effect_running",
                return_value={"status": "running"},
            ),
            patch(
                "core.application.extensions.external_effects.confirm_external_effect_from_invocation",
                return_value={
                    "status": "succeeded",
                    "__confirmed_replay_invocation": {
                        "status": "succeeded",
                        "response": {"ok": True, "result": {"message_id": "provider"}},
                    },
                },
            ),
            patch(
                "core.application.extensions.external_effect_replay.hydrate_external_effect_claim",
                side_effect=lambda _db, result: {
                    **result,
                    "replay_invocation": replay_invocation,
                },
            ),
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
                return_value={
                    "status": "succeeded",
                    "response": {"ok": True, "result": {"message_id": "provider"}},
                },
            ) as provider,
        ):
            for _attempt in range(2):
                svc.dispatch(
                    CommandDispatchInput(
                        command_type="invoke_extension_action",
                        target={
                            "extension_id": "example.host-fixture",
                            "action_id": "publish_record",
                            "project_id": "7",
                        },
                        payload={"record_id": "same-record"},
                    ),
                    scope=normalize_scope(account_id=DEFAULT_ACCOUNT_ID, project_id="7"),
                )

            retry_request = CommandDispatchInput(
                command_type="invoke_extension_action",
                target={
                    "extension_id": "example.host-fixture",
                    "action_id": "publish_record",
                },
                payload={"record_id": "retry-record"},
                correlation_id="caller-retry-1",
            )
            svc.dispatch(retry_request, scope=normalize_scope(account_id=DEFAULT_ACCOUNT_ID))
            replayed = svc.dispatch(
                retry_request,
                scope=normalize_scope(account_id=DEFAULT_ACCOUNT_ID),
            )
            _raises_input_767_1 = CommandDispatchInput(
                command_type="invoke_extension_action",
                target={"extension_id": "example.host-fixture", "action_id": "publish_record"},
                payload={"record_id": "changed-record"},
                correlation_id="caller-retry-1",
            )
            _raises_input_767_2 = normalize_scope(account_id=DEFAULT_ACCOUNT_ID)
            with pytest.raises(ServiceError, match="external_effect_idempotency_conflict"):
                svc.dispatch(_raises_input_767_1, scope=_raises_input_767_2)

        assert provider.call_count == 3
        assert overrides[0] != overrides[1]
        assert overrides[2] == overrides[3] == overrides[4]
        assert replayed.result["invocation"] == replay_invocation

    def test_dispatch_external_effect_retry_excludes_host_transport_timeout(self, monkeypatch):
        from core.application.extensions import external_effects

        monkeypatch.setenv("ENCRYPTION_KEY", "command-timeout-fingerprint-root")
        svc, bl = _make_service()
        bl.get_extension.return_value = {
            "extension_key": "example.host-fixture",
            "account_id": DEFAULT_ACCOUNT_ID,
            "enabled": True,
            "action_definitions": [{"action_id": "publish_record"}],
        }
        spawned_claim = {
            "declared": True,
            "should_spawn": True,
            "effect": {
                "effect_id": "eff-timeout-retry",
                "status": "claimed",
                "claim_token": "claim-1",
            },
        }
        duplicate_claim = {
            "declared": True,
            "should_spawn": False,
            "effect": {"effect_id": "eff-timeout-retry", "status": "succeeded"},
        }
        replay_invocation = {
            "status": "succeeded",
            "response": {"ok": True, "result": {"message_id": "replayed"}},
        }
        with (
            patch(
                "core.application.extensions.external_effects.claim_external_effect_for_action",
                side_effect=[spawned_claim, duplicate_claim],
            ) as claim,
            patch(
                "core.application.extensions.external_effects.mark_external_effect_running",
                return_value={"status": "running"},
            ),
            patch(
                "core.application.extensions.external_effects.confirm_external_effect_from_invocation",
                return_value={
                    "status": "succeeded",
                    "__confirmed_replay_invocation": replay_invocation,
                },
            ),
            patch(
                "core.application.extensions.external_effect_replay.hydrate_external_effect_claim",
                side_effect=lambda _db, result: {
                    **result,
                    "replay_invocation": replay_invocation,
                },
            ),
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
                return_value={
                    "status": "succeeded",
                    "response": {"ok": True, "result": {"message_id": "provider"}},
                },
            ) as provider,
        ):
            first = CommandDispatchInput(
                command_type="invoke_extension_action",
                target={
                    "extension_id": "example.host-fixture",
                    "action_id": "publish_record",
                },
                payload={"record_id": "same-record", "timeout_seconds": 30},
                correlation_id="timeout-only-retry-1",
            )
            retry = CommandDispatchInput(
                command_type="invoke_extension_action",
                target={
                    "extension_id": "example.host-fixture",
                    "action_id": "publish_record",
                },
                payload={"record_id": "same-record", "timeout_seconds": 60},
                correlation_id="timeout-only-retry-1",
            )
            svc.dispatch(first, scope=normalize_scope(account_id=DEFAULT_ACCOUNT_ID))
            replayed = svc.dispatch(retry, scope=normalize_scope(account_id=DEFAULT_ACCOUNT_ID))

        first_claim = claim.call_args_list[0].kwargs
        retry_claim = claim.call_args_list[1].kwargs
        assert (
            first_claim["request_payload"]
            == retry_claim["request_payload"]
            == {"input": {"record_id": "same-record"}, "target": {}}
        )
        identities = [
            external_effects.build_external_effect_identity(
                account_id=call["account_id"],
                project_id=call["project_id"],
                extension_id=call["extension_id"],
                extension_install_id=call["extension_install_id"],
                connection_id=call["connection_id"],
                operation_id=call["operation_id"],
                source_ref=call["source_ref"],
                request_payload=call["request_payload"],
                test_mode=call["test_mode"],
            )
            for call in (first_claim, retry_claim)
        ]
        assert identities[0]["request_fingerprint"] == identities[1]["request_fingerprint"]
        intent_key = first_claim["idempotency_key_override"]
        assert intent_key == retry_claim["idempotency_key_override"]
        assert intent_key.startswith("hmac-sha256:")
        assert (
            intent_key
            != hashlib.sha256(b"flow-steward-command-intent-v1:timeout-only-retry-1").hexdigest()
        )
        provider.assert_called_once()
        assert provider.call_args.kwargs["timeout_seconds"] == 30
        assert provider.call_args.kwargs["input_payload"] == {"record_id": "same-record"}
        assert replayed.result["invocation"] == replay_invocation

    def test_dispatch_external_effect_retry_conflicts_when_business_target_changes(self):
        svc, bl = _make_service()
        bl.get_extension.return_value = {
            "extension_key": "example.host-fixture",
            "account_id": DEFAULT_ACCOUNT_ID,
            "enabled": True,
            "action_definitions": [{"action_id": "publish_record"}],
        }
        seen: dict[str, dict] = {}

        def claim(*_args, **kwargs):
            intent = kwargs["idempotency_key_override"]
            fingerprint_payload = kwargs["request_payload"]
            if intent in seen and seen[intent] != fingerprint_payload:
                return {
                    "declared": True,
                    "should_spawn": False,
                    "effect": {"effect_id": "effect-target", "status": "succeeded"},
                    "error": "external_effect_idempotency_conflict",
                }
            first = intent not in seen
            seen[intent] = fingerprint_payload
            return {
                "declared": True,
                "should_spawn": first,
                "effect": {
                    "effect_id": "effect-target",
                    "status": "claimed" if first else "succeeded",
                    "claim_token": "claim-1" if first else "",
                },
            }

        with (
            patch(
                "core.application.extensions.external_effects.claim_external_effect_for_action",
                side_effect=claim,
            ),
            patch(
                "core.application.extensions.external_effects.mark_external_effect_running",
                return_value={"status": "running"},
            ),
            patch(
                "core.application.extensions.external_effects.confirm_external_effect_from_invocation",
                return_value={
                    "status": "succeeded",
                    "__confirmed_replay_invocation": {
                        "status": "succeeded",
                        "response": {"ok": True},
                    },
                },
            ),
            patch(
                "core.application.extensions.external_effect_replay.hydrate_external_effect_claim",
                side_effect=lambda _db, result: {
                    **result,
                    "replay_invocation": {
                        "status": "succeeded",
                        "response": {"ok": True},
                    },
                },
            ),
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_action",
                return_value={"status": "succeeded", "response": {"ok": True}},
            ) as provider,
        ):
            common = {
                "command_type": "invoke_extension_action",
                "payload": {"record_id": "same-record"},
                "correlation_id": "target-change-retry-1",
            }
            svc.dispatch(
                CommandDispatchInput(
                    **common,
                    target={
                        "extension_id": "example.host-fixture",
                        "action_id": "publish_record",
                        "chat_id": "chat-a",
                        "title": "First title",
                    },
                ),
                scope=normalize_scope(account_id=DEFAULT_ACCOUNT_ID),
            )
            _raises_input_984_1 = CommandDispatchInput(
                **common,
                target={
                    "extension_id": "example.host-fixture",
                    "action_id": "publish_record",
                    "chat_id": "chat-b",
                    "title": "Changed title",
                },
            )
            _raises_input_984_2 = normalize_scope(account_id=DEFAULT_ACCOUNT_ID)
            with pytest.raises(ServiceError, match="external_effect_idempotency_conflict"):
                svc.dispatch(_raises_input_984_1, scope=_raises_input_984_2)

        provider.assert_called_once()
        first_request = next(iter(seen.values()))
        assert first_request == {
            "input": {"record_id": "same-record"},
            "target": {"chat_id": "chat-a", "title": "First title"},
        }
        assert "connection" not in first_request["target"]
        assert "project_id" not in first_request["target"]

    def test_dispatch_external_effect_action_rejects_inline_connection_payload(self):
        svc, bl = _make_service()
        bl.get_extension.return_value = {
            "extension_key": "example.host-fixture",
            "account_id": DEFAULT_ACCOUNT_ID,
            "enabled": True,
            "action_definitions": [{"action_id": "publish_record"}],
            "compiled_manifest": {
                "external_effects": [
                    {
                        "operation_id": "publish_record",
                        "effect_kind": "message_delivery",
                        "channel": "api",
                        "idempotency": "required",
                        "test_mode_behavior": "suppress",
                    }
                ]
            },
        }
        _raises_input_1026_1 = CommandDispatchInput(
            command_type="invoke_extension_action",
            target={
                "extension_id": "example.host-fixture",
                "action_id": "publish_record",
                "project_id": "7",
                "connection_id": "conn_external",
                "connection": {
                    "connection_id": "conn_external",
                    "config": {"host": "evil.example"},
                    "secrets": {"api_token": "inline"},
                },
            },
            payload={"record_id": "record-a", "title": "Hello"},
        )
        _raises_input_1026_2 = normalize_scope(account_id=DEFAULT_ACCOUNT_ID, project_id="7")
        with (
            patch(
                "core.application.extensions.external_effects.claim_external_effect_for_action"
            ) as claim,
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_action"
            ) as mock_run,
            pytest.raises(ServiceValidationError, match="only target\\.connection_id"),
        ):
            svc.dispatch(_raises_input_1026_1, scope=_raises_input_1026_2)

        claim.assert_not_called()
        mock_run.assert_not_called()

    def test_dispatch_request_approval(self):
        svc, bl = _make_service()
        inp = CommandDispatchInput(
            command_type="request_approval",
            target={"job_id": 5},
        )
        result = svc.dispatch(inp, scope=None)
        assert result.status == "executed"
        assert result.result.get("action") == "approval_requested"
        bl.update_job.assert_called_once_with("5", status="awaiting_approval")
        assert bl.jobs["5"]["status"] == "awaiting_approval"

    def test_dispatch_resolve_approval(self):
        svc, bl = _make_service()
        inp = CommandDispatchInput(
            command_type="resolve_approval",
            correlation_id="approval-request-5",
            target={"job_id": 5, "approval_id": "approval-5"},
            payload={"approved": True},
        )
        result = svc.dispatch(inp, scope=None)
        assert result.status == "executed"
        assert result.result.get("approved") is True
        decision = bl.approval_commands.decisions[0]
        assert decision["thread_id"] == "thread-5"
        assert decision["decision"].approval_id == "approval-5"
        assert decision["decision"].approved is True
        assert decision["idempotency_key"] == "approval-request-5"
        bl.update_job.assert_not_called()

    def test_dispatch_update_external_status_requires_fields(self):
        svc, _ = _make_service()
        inp = CommandDispatchInput(
            command_type="update_external_status",
            target={"extension_id": "sample_tasks"},
            payload={},  # missing status
        )
        with pytest.raises(ServiceValidationError, match="external_id"):
            svc.dispatch(inp, scope=None)

    def test_dispatch_update_external_status_requires_registered_extension(self):
        svc, bl = _make_service()
        bl.get_extension.return_value = None
        inp = CommandDispatchInput(
            command_type="update_external_status",
            target={"extension_id": "sample_tasks", "external_id": "TASK-123"},
            payload={"status": "done"},
        )
        with pytest.raises(ServiceNotFoundError, match="Extension 'sample_tasks' not found"):
            svc.dispatch(inp, scope=None)

    def test_dispatch_update_external_status_via_extension(self):
        """With matching extension, routes through extension action."""
        svc, bl = _make_service()
        bl.get_extension.return_value = {
            "extension_key": "sample_tasks",
            "account_id": DEFAULT_ACCOUNT_ID,
            "enabled": True,
        }
        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action"
        ) as mock_run:
            mock_run.return_value = {
                "status": "succeeded",
                "response": {"ok": True, "status": "updated", "task_id": "TASK-123"},
            }
            inp = CommandDispatchInput(
                command_type="update_external_status",
                target={"extension_id": "sample_tasks", "external_id": "TASK-123"},
                payload={"status": "in_progress"},
            )
            result = svc.dispatch(inp, scope=None)
            assert result.status == "executed"
            assert result.result.get("action") == "status_updated"
            assert result.result.get("new_status") == "in_progress"
            assert result.result.get("result") == {
                "ok": True,
                "status": "updated",
                "task_id": "TASK-123",
            }
            input_payload = mock_run.call_args.kwargs["input_payload"]
            assert input_payload["task_id"] == "TASK-123"
            assert input_payload["final_status"] == "in_progress"
            mock_run.assert_called_once()

    def test_dispatch_platform_setup_failure_terminalizes_claim_before_provider(self):
        svc, bl = _make_service()
        bl.get_extension.return_value = {
            "extension_key": "example.host-fixture",
            "extension_install_id": "install-1",
            "account_id": DEFAULT_ACCOUNT_ID,
            "enabled": True,
            "action_definitions": [
                {"action_id": "publish_record", "handler": {"mode": "platform"}}
            ],
            "compiled_manifest": {
                "external_effects": [
                    {
                        "operation_id": "publish_record",
                        "effect_kind": "message_delivery",
                        "channel": "api",
                        "idempotency": "required",
                    }
                ]
            },
        }
        bl.get_project_connection.return_value = {
            "connection_id": "conn_external",
            "account_id": DEFAULT_ACCOUNT_ID,
            "project_id": "7",
            "extension_id": "example.host-fixture",
            "extension_install_id": "install-1",
            "enabled": True,
            "connection_type": "api_token",
            "connection_config": {},
            "secret_refs": {},
        }
        claim_result = {
            "declared": True,
            "should_spawn": True,
            "effect": {
                "effect_id": "eff_cmd_1",
                "claim_token": "claim-1",
                "status": "claimed",
            },
        }

        _raises_input_1177_1 = CommandDispatchInput(
            command_type="invoke_extension_action",
            target={
                "extension_id": "example.host-fixture",
                "action_id": "publish_record",
                "project_id": "7",
                "connection_id": "conn_external",
            },
            payload={"record_id": "record-a"},
            correlation_id="command:publish-record:setup-failure",
        )
        _raises_input_1177_2 = normalize_scope(
            account_id=DEFAULT_ACCOUNT_ID, project_id="7", principal_type="service_account"
        )
        with (
            patch(
                "core.application.extensions.external_effects.claim_external_effect_for_action",
                return_value=claim_result,
            ),
            patch(
                "core.application.extensions.platform_connection_operations.install_platform_connection_operation",
                side_effect=RuntimeError("host setup failed"),
            ),
            patch(
                "core.application.extensions.external_effects.fail_external_effect_before_spawn",
                return_value={"effect_id": "eff_cmd_1", "status": "failed"},
            ) as fail_before_spawn,
            patch(
                "core.application.extensions.external_effects.mark_external_effect_running"
            ) as mark_running,
            patch(
                "core.infrastructure.extension_runtime.extension_runtime.run_extension_action"
            ) as provider,
            pytest.raises(ServiceError, match="before provider invocation"),
        ):
            svc.dispatch(_raises_input_1177_1, scope=_raises_input_1177_2)

        fail_before_spawn.assert_called_once_with(
            bl,
            effect_id="eff_cmd_1",
            claim_token="claim-1",
            code="external_effect_pre_spawn_setup_failed",
        )
        mark_running.assert_not_called()
        provider.assert_not_called()

    def test_dispatch_update_external_status_rejects_semantic_failure(self):
        svc, bl = _make_service()
        bl.get_extension.return_value = {
            "extension_key": "sample_tasks",
            "account_id": DEFAULT_ACCOUNT_ID,
            "enabled": True,
        }
        with patch(
            "core.infrastructure.extension_runtime.extension_runtime.run_extension_action"
        ) as mock_run:
            mock_run.return_value = {
                "status": "succeeded",
                "response": {
                    "ok": False,
                    "errors": [{"code": "missing_task_id", "message": "missing_task_id"}],
                },
            }
            inp = CommandDispatchInput(
                command_type="update_external_status",
                target={"extension_id": "sample_tasks", "external_id": "TASK-123"},
                payload={"status": "completed"},
            )
            with pytest.raises(Exception, match="missing_task_id"):
                svc.dispatch(inp, scope=None)
