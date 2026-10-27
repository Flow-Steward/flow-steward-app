"""Contract tests for schema-driven job commands."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from core.application.management.scope import ScopeContext
from core.entrypoints.cli.platform.service_registry import ServiceRegistry


class _JobQuery:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def list_jobs(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("list_jobs", (), kwargs))
        per_page = int(kwargs.get("per_page") or 25)
        page = int(kwargs.get("page") or 1)
        start = int(kwargs.get("offset") or (page - 1) * per_page)
        return {
            "jobs": [
                {"job_id": f"job_{index}", "status": "in_progress"}
                for index in range(start, start + per_page)
            ],
            "total": 1000,
            "page": page,
            "per_page": per_page,
            "offset": start,
            "total_pages": max(1, -(-1000 // per_page)),
            "project_id": kwargs.get("project_id"),
            "status": kwargs.get("status"),
        }

    def get_job_detail_contract(self, job_id: str, **kwargs: object) -> dict[str, object]:
        self.calls.append(("get_job_detail_contract", (job_id,), kwargs))
        return {"id": job_id, "status": "awaiting_approval", "thread_id": "thread_1"}

    async def get_job_status(self, job_id: str, **kwargs: object) -> dict[str, object]:
        self.calls.append(("get_job_status", (job_id,), kwargs))
        return {"id": job_id, "status": "in_progress", "task_summary": "Run task"}

    def list_logs(self, job_id: str, **kwargs: object) -> dict[str, object]:
        self.calls.append(("list_logs", (job_id,), kwargs))
        return {"logs": [{"id": "1", "message": "hello"}], "status": "in_progress"}

    def list_messages(self, job_id: str, **kwargs: object) -> dict[str, object]:
        self.calls.append(("list_messages", (job_id,), kwargs))
        return {"messages": [{"id": "1", "summary": "hello"}], "last_id": "1"}

    def list_errors(self, job_id: str, **kwargs: object) -> dict[str, object]:
        self.calls.append(("list_errors", (job_id,), kwargs))
        return {"job_id": job_id, "errors": [{"id": "err_1"}]}


class _JobCommand:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def acknowledge_job(self, job_id: str, command: object, **kwargs: object) -> dict[str, object]:
        self.calls.append(("acknowledge_job", (job_id, command), kwargs))
        return {"job_id": job_id, "status": "acknowledged"}

    def unacknowledge_job(self, job_id: str, **kwargs: object) -> dict[str, object]:
        self.calls.append(("unacknowledge_job", (job_id,), kwargs))
        return {"job_id": job_id, "status": "unacknowledged"}

    def cancel_job(self, job_id: str, **kwargs: object) -> dict[str, object]:
        self.calls.append(("cancel_job", (job_id,), kwargs))
        return {"job_id": job_id, "status": "cancelled", "cancelled": True}

    def restart_job(self, job_id: str, **kwargs: object) -> dict[str, object]:
        self.calls.append(("restart_job", (job_id,), kwargs))
        return {"job_id": job_id, "status": "pending", "new_job_id": "job_2", "enqueued": True}


class _ApprovalCommand:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def resume_task(self, *, thread_id: str, scope: object, decision: object) -> dict[str, object]:
        self.calls.append(
            (
                "resume_task",
                {
                    "thread_id": thread_id,
                    "scope": scope,
                    "decision": decision,
                },
            )
        )
        return {
            "job_id": "job_1",
            "thread_id": thread_id,
            "approval_id": str(getattr(decision, "approval_id", "")),
            "approved": bool(getattr(decision, "approved", False)),
            "action": getattr(decision, "action", None),
            "comment": getattr(decision, "comment", ""),
            "selected_stories": [],
            "override_content": "",
            "actor": "operator",
            "status": "in_progress",
        }


@dataclass
class _HealthSnapshots:
    calls: list[dict[str, object]]

    def collect(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(dict(kwargs))
        return {"ok": True, "service": {"ok": True}, "database": {"ok": True}}


@dataclass
class _Runtime:
    job_query_service: _JobQuery
    job_command_service: _JobCommand
    approval_command_service: _ApprovalCommand
    ops_health_snapshot_service: _HealthSnapshots
    project_query_service: object = object()
    project_command_service: object = object()
    reporting_service: object = object()
    approval_query_service: object = object()
    workflow_operator_service: object = object()
    user_query_service: object = object()
    user_command_service: object = object()
    extension_query_service: object = object()
    extension_binding_service: object = object()
    extension_scaffold_service: object = object()


def _app() -> tuple[object, _Runtime]:
    from core.entrypoints.cli.platform.click_adapter import build_click_app

    runtime = _Runtime(
        job_query_service=_JobQuery(),
        job_command_service=_JobCommand(),
        approval_command_service=_ApprovalCommand(),
        ops_health_snapshot_service=_HealthSnapshots(calls=[]),
    )
    return build_click_app(ServiceRegistry(runtime), SimpleNamespace()), runtime


def _assert_cli_scope(value: object, *, project_id: str | None = None) -> ScopeContext:
    assert isinstance(value, ScopeContext)
    assert value.actor_id == "flow-steward-cli"
    assert value.principal_type == "system_actor"
    assert value.project_id == project_id
    if project_id:
        assert value.allowed_project_ids == frozenset({project_id})
    return value


def test_jobs_read_commands_delegate_to_shared_query_service() -> None:
    app, runtime = _app()
    runner = CliRunner()

    cases = [
        (
            ["jobs", "list", "--project-id", "proj_1", "--status", "in_progress", "--limit", "5"],
            {
                "method": "list_jobs",
                "args": (),
                "kwargs": {
                    "project_id": "proj_1",
                    "status": "in_progress",
                    "limit": 5,
                    "offset": 0,
                },
                "scope_project_id": "proj_1",
            },
        ),
        (
            ["jobs", "show", "job_1"],
            {
                "method": "get_job_status",
                "args": ("job_1",),
                "kwargs": {},
            },
        ),
        (
            ["jobs", "show", "job_1", "--include-details"],
            {
                "method": "get_job_detail_contract",
                "args": ("job_1",),
                "kwargs": {"user_role": "operator"},
            },
        ),
        (
            ["jobs", "status", "job_1"],
            {"method": "get_job_status", "args": ("job_1",), "kwargs": {}},
        ),
        (
            ["jobs", "logs", "job_1", "--since-id", "10", "--limit", "5"],
            {
                "method": "list_logs",
                "args": ("job_1",),
                "kwargs": {"since_id": "10", "before_id": "", "newest": False, "limit": 5},
            },
        ),
        (
            ["jobs", "messages", "job_1", "--step-id", "step_1", "--producer", "planner"],
            {
                "method": "list_messages",
                "args": ("job_1",),
                "kwargs": {
                    "since_id": "",
                    "before_id": "",
                    "newest": False,
                    "step_id": "step_1",
                    "producer": "planner",
                    "skill": "",
                    "status": "",
                    "limit": 25,
                },
            },
        ),
        (
            ["jobs", "errors", "job_1", "--limit", "3"],
            {"method": "list_errors", "args": ("job_1",), "kwargs": {"limit": 3}},
        ),
    ]

    for args, expected in cases:
        result = runner.invoke(app, [*args, "--plain"])
        assert result.exit_code == 0, (args, result.output)
        method, call_args, kwargs = runtime.job_query_service.calls[-1]
        scope = kwargs.pop("scope")
        assert method == expected["method"]
        assert call_args == expected["args"]
        assert kwargs == expected["kwargs"]
        _assert_cli_scope(scope, project_id=expected.get("scope_project_id"))


def test_jobs_logs_preserves_opaque_backward_cursor_in_json() -> None:
    app, runtime = _app()
    cursor = "22222222-2222-4222-8222-222222222222"
    runtime.job_query_service.list_logs = lambda *_args, **_kwargs: {
        "logs": [{"id": cursor}],
        "status": "completed",
        "last_id": cursor,
        "next_before_id": cursor,
        "has_more": True,
    }
    result = CliRunner().invoke(app, ["jobs", "logs", "job_1", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert payload["next_before_id"] == cursor
    assert payload["last_id"] == cursor
    assert payload["has_more"] is True


def test_jobs_list_offset_is_forwarded_to_the_shared_service() -> None:
    app, runtime = _app()

    result = CliRunner().invoke(
        app,
        ["jobs", "list", "--offset", "10", "--limit", "25", "--json"],
    )

    assert result.exit_code == 0, result.output
    method, _, kwargs = runtime.job_query_service.calls[-1]
    assert method == "list_jobs"
    assert kwargs["offset"] == 10
    assert kwargs["limit"] == 25
    _assert_cli_scope(kwargs["scope"])


def test_jobs_list_serializes_database_datetimes_as_rest_compatible_iso_values() -> None:
    app, runtime = _app()
    timestamp = datetime(2026, 8, 3, 12, 30, tzinfo=UTC)
    runtime.job_query_service.list_jobs = lambda **_kwargs: {
        "jobs": [
            {
                "job_id": "job_datetime",
                "status": "in_progress",
                "queued_at": timestamp,
                "started_at": timestamp,
                "finished_at": None,
                "heartbeat_at": timestamp,
            }
        ],
        "total": 1,
        "page": 1,
        "per_page": 25,
        "offset": 0,
        "total_pages": 1,
    }

    result = CliRunner().invoke(app, ["jobs", "list", "--json"])

    assert result.exit_code == 0, result.output
    assert "2026-08-03T12:30:00Z" in result.output


def test_jobs_list_large_offset_is_forwarded_without_binding_pagination() -> None:
    app, runtime = _app()

    result = CliRunner().invoke(
        app,
        ["jobs", "list", "--offset", "510", "--limit", "25", "--json"],
    )

    assert result.exit_code == 0, result.output
    method, _, kwargs = runtime.job_query_service.calls[-1]
    assert method == "list_jobs"
    assert kwargs["offset"] == 510
    assert kwargs["limit"] == 25
    _assert_cli_scope(kwargs["scope"])


def test_jobs_list_offset_is_delegated_once_to_the_shared_query_service() -> None:
    app, runtime = _app()

    result = CliRunner().invoke(
        app,
        ["jobs", "list", "--offset", "490", "--limit", "25", "--json"],
    )

    assert result.exit_code == 0, result.output
    calls = [call for call in runtime.job_query_service.calls if call[0] == "list_jobs"]
    assert len(calls) == 1
    assert calls[0][2]["offset"] == 490
    assert calls[0][2]["limit"] == 25


def test_jobs_lifecycle_commands_delegate_to_shared_command_services() -> None:
    app, runtime = _app()
    runner = CliRunner()

    cases = [
        (["jobs", "ack", "job_1", "--actor", "cli", "--confirm"], "acknowledge_job"),
        (["jobs", "unack", "job_1", "--confirm"], "unacknowledge_job"),
        (
            ["jobs", "cancel", "job_1", "--actor", "operator", "--comment", "stop", "--confirm"],
            "cancel_job",
        ),
        (["jobs", "restart", "job_1", "--confirm"], "restart_job"),
    ]
    for args, name in cases:
        result = runner.invoke(app, [*args, "--plain"], input="y\n")
        assert result.exit_code == 0, (args, result.output)
        assert runtime.job_command_service.calls[-1][0] == name

    cancel_call = runtime.job_command_service.calls[-2]
    method, args, kwargs = cancel_call
    scope = kwargs.pop("scope")
    assert (method, args, kwargs) == (
        "cancel_job",
        ("job_1",),
        {"actor": "operator", "comment": "stop"},
    )
    _assert_cli_scope(scope)


def test_jobs_approve_reject_use_in_process_approval_service_without_http() -> None:
    app, runtime = _app()
    runner = CliRunner()

    approved = runner.invoke(
        app,
        [
            "jobs",
            "approve",
            "thread_1",
            "--approval-id",
            "approval_1",
            "--comment",
            "ship it",
            "--action",
            "approve",
            "--confirm",
            "--plain",
        ],
        input="y\n",
    )
    assert approved.exit_code == 0, approved.output
    decision = runtime.approval_command_service.calls[-1][1]["decision"]
    assert runtime.approval_command_service.calls[-1][1]["thread_id"] == "thread_1"
    assert decision.approved is True
    assert decision.approval_id == "approval_1"
    assert decision.comment == "ship it"

    rejected = runner.invoke(
        app,
        [
            "jobs",
            "reject",
            "thread_1",
            "--approval-id",
            "approval_1",
            "--comment",
            "needs changes",
            "--confirm",
            "--plain",
        ],
        input="y\n",
    )
    assert rejected.exit_code == 0, rejected.output
    decision = runtime.approval_command_service.calls[-1][1]["decision"]
    assert decision.approved is False
    assert decision.approval_id == "approval_1"
    assert decision.action == "reject"


def test_jobs_health_snapshot_delegates_to_application_health_collector(
    tmp_path: Path, monkeypatch: object
) -> None:
    from core.entrypoints.cli.platform import click_adapter

    app, runtime = _app()
    secret_root = tmp_path / ".flow-steward" / "secret-input"
    secret_root.mkdir(parents=True)
    dsn_file = secret_root / "db-dsn"
    dsn_file.write_text("postgres://example\n", encoding="utf-8")
    dsn_file.chmod(0o600)
    monkeypatch.setattr(click_adapter, "DEFAULT_SECRET_FILE_ROOT", secret_root)

    result = CliRunner().invoke(
        app,
        [
            "jobs",
            "health-snapshot",
            "--base-url",
            "https://flow.example",
            "--verify-ssl",
            "--timeout-seconds",
            "5",
            "--dsn-file",
            str(dsn_file),
            "--stale-heartbeat-seconds",
            "60",
            "--plain",
        ],
    )

    assert result.exit_code == 0, result.output
    assert runtime.ops_health_snapshot_service.calls[-1] == {
        "base_url": "https://flow.example",
        "verify_ssl": True,
        "timeout_seconds": 5,
        "dsn": "postgres://example",
        "stale_heartbeat_seconds": 60,
    }
    assert "postgres://example" not in result.output


def test_jobs_health_snapshot_returns_dependency_exit_when_snapshot_is_unhealthy() -> None:
    app, runtime = _app()
    runtime.ops_health_snapshot_service = SimpleNamespace(
        collect=lambda **_kwargs: {
            "ok": False,
            "service": {"ok": False, "error": "connection refused"},
            "database": {"ok": True},
        }
    )

    result = CliRunner().invoke(
        app,
        [
            "jobs",
            "health-snapshot",
            "--base-url",
            "http://unavailable.example",
            "--plain",
        ],
    )

    assert result.exit_code == 7
    assert "dependency_unavailable" in result.output
    assert "service health check failed" in result.output


def test_jobs_health_snapshot_exposes_only_file_or_stdin_secret_sources() -> None:
    app, _runtime = _app()

    help_result = CliRunner().invoke(
        app,
        ["jobs", "health-snapshot", "--help"],
        color=True,
        terminal_width=80,
    )
    raw_result = CliRunner().invoke(
        app,
        ["jobs", "health-snapshot", "--dsn", "postgres://example", "--plain"],
    )

    assert help_result.exit_code == 0, help_result.output
    plain_help = re.sub(r"\x1b\[[0-9;]*m", "", help_result.output)
    secret_policy = re.sub(r"\s+", " ", plain_help.split("Secret input policy:", maxsplit=1)[1])
    assert "--dsn " not in plain_help
    assert "--dsn-file" in plain_help
    assert "--dsn-stdin" in plain_help
    assert "--dsn-file is limited to .flow-steward/secret-input/" in secret_policy
    assert "use --dsn-stdin for external secret managers" in secret_policy
    assert "--password-file" not in secret_policy
    assert raw_result.exit_code == 2
    assert "No such option" in raw_result.output
    assert "--dsn" in raw_result.output
    assert "postgres://example" not in raw_result.output


def test_jobs_binding_has_no_http_approval_boundary() -> None:
    binding_path = (
        Path(__file__).parents[3] / "core/entrypoints/cli/platform/service_bindings/jobs.py"
    )
    runtime_path = Path(__file__).parents[3] / "core/entrypoints/cli/runtime.py"
    source = binding_path.read_text(encoding="utf-8")
    runtime_source = runtime_path.read_text(encoding="utf-8")

    assert "requests" not in source
    assert "httpx" not in source
    assert "urllib" not in source
    assert "/api/" not in source
    assert "http_app" not in runtime_source
    assert '["_approval_command_service"]' not in runtime_source


def test_approval_runtime_uses_durable_resume_ports_not_private_http_app_symbols() -> None:
    source = (
        Path(__file__).parents[3] / "core/entrypoints/cli/approval_command_runtime.py"
    ).read_text(encoding="utf-8")

    assert "**workflow_resume_command_ports()" in source
    assert "build_graph_resume_runtime_hooks" not in source
    assert "start_graph_resume_thread" not in source
    assert "state_reader.get_checkpointer" in source
    assert "state_reader._get_checkpointer" not in source
    assert "build_run_graph_resume_handler" not in source
    assert "build_start_graph_resume_thread_handler" not in source
    assert "_set_job_status_by_thread_id" not in source
    assert "_resume_shutdown_in_progress" not in source
    assert "http_app_events_resume" not in source
    assert "http_app_lifecycle" not in source


@pytest.mark.parametrize("include_details", [False, True])
def test_jobs_show_keeps_typed_public_results_and_redacts_only_credentials(include_details) -> None:
    app, runtime = _app()
    payload = {
        "id": "job_1",
        "status": "completed",
        "workflow_run": None,
        "workflow_result": {
            "outputs": {
                "result": "Hello-World",
                "accepted": False,
                "count": 0,
                "optional": None,
                "record": {"nested": [False, 0, None]},
                "token": "private-test-marker",
            },
            "artifacts": {},
        },
    }
    calls = []

    def selected_query(*_args, **_kwargs):
        calls.append("detail" if include_details else "status")
        return payload

    def unexpected_query(*_args, **_kwargs):
        raise AssertionError("jobs.show selected the wrong query")

    runtime.job_query_service.get_job_status = (
        unexpected_query if include_details else selected_query
    )
    runtime.job_query_service.get_job_detail_contract = (
        selected_query if include_details else unexpected_query
    )
    args = ["jobs", "show", "job_1", "--json"]
    if include_details:
        args.append("--include-details")
    response = CliRunner().invoke(app, args)
    assert response.exit_code == 0, response.output
    assert calls == ["detail" if include_details else "status"]
    result = json.loads(response.output)["result"]
    if not include_details:
        assert result.get("workflow_result") is None
        assert "Hello-World" not in response.output
        return
    outputs = result["workflow_result"]["outputs"]
    assert outputs["result"] == "Hello-World"
    assert outputs["accepted"] is False
    assert type(outputs["count"]) is int
    assert outputs["count"] == 0
    assert outputs["optional"] is None
    assert outputs["record"] == {"nested": [False, 0, None]}
    assert outputs["token"] == "[REDACTED]"
    assert "private-test-marker" not in response.output
