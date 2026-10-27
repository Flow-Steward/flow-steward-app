"""Contract tests for schema-driven project commands."""

from __future__ import annotations

import inspect
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from core.application.management.errors import ServiceUnavailableError
from core.application.management.scope import ScopeContext
from core.entrypoints.cli.platform.modes import TerminalFacts
from core.entrypoints.cli.platform.service_registry import ServiceRegistry

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


class _ProjectQuery:
    async def list_projects(
        self,
        *,
        page: int,
        per_page: int,
        search: str = "",
        scope: object = None,
    ) -> dict[str, object]:
        self.page = page
        self.per_page = per_page
        self.search = search
        self.list_scope = scope
        return {
            "projects": [
                {"id": "proj_1", "name": "Project One"},
                {"id": "proj_2", "name": "Project Two"},
            ]
        }

    def get_project_detail(self, project_id: str, **kwargs: object) -> dict[str, object]:
        self.detail_project_id = project_id
        self.detail_kwargs = kwargs
        return {"project": {"id": project_id, "name": "Project One"}, "jobs": []}

    def list_notification_routes(self, project_id: str, **kwargs: object) -> dict[str, object]:
        self.routes_call = (project_id, kwargs)
        return {"project_id": project_id, "notification_routes": [{"id": "route_1"}]}

    def list_notification_deliveries(self, project_id: str, **kwargs: object) -> dict[str, object]:
        self.deliveries_call = (project_id, kwargs)
        return {"project_id": project_id, "notification_deliveries": [{"id": "delivery_1"}]}


class _ProjectCommand:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.fail: BaseException | None = None

    def dispatch_project_notification(
        self,
        project_id: str,
        command: object,
        **kwargs: object,
    ) -> dict[str, object]:
        if self.fail is not None:
            raise self.fail
        self.calls.append((project_id, command))
        self.dispatch_kwargs = kwargs
        return {"project_id": project_id, "notification_deliveries": [{"id": "delivery_1"}]}


@dataclass
class _Runtime:
    project_query_service: _ProjectQuery
    project_command_service: _ProjectCommand
    user_query_service: object = object()
    user_command_service: object = object()
    extension_scaffold_service: object = object()


def _app() -> tuple[object, _ProjectQuery, _ProjectCommand]:
    from core.entrypoints.cli.platform.click_adapter import build_click_app

    query = _ProjectQuery()
    command = _ProjectCommand()
    return (
        build_click_app(
            ServiceRegistry(_Runtime(project_query_service=query, project_command_service=command)),
            SimpleNamespace(),
        ),
        query,
        command,
    )


def _log_path(output: str) -> Path:
    for line in output.splitlines():
        plain_line = _strip_ansi(line)
        if plain_line.startswith("Log: "):
            return Path(plain_line.removeprefix("Log: ").strip())
    raise AssertionError(output)


def _assert_cli_scope(
    value: object, *, project_id: str | None = None, workflow_id: str | None = None
) -> ScopeContext:
    assert isinstance(value, ScopeContext)
    assert value.actor_id == "flow-steward-cli"
    assert value.principal_type == "system_actor"
    assert value.project_id == project_id
    assert value.workflow_id == workflow_id
    if project_id:
        assert value.allowed_project_ids == frozenset({project_id})
    if workflow_id:
        assert value.allowed_workflow_ids == frozenset({workflow_id})
    return value


def _without_scope(
    kwargs: dict[str, object], *, project_id: str | None = None
) -> dict[str, object]:
    copied = dict(kwargs)
    _assert_cli_scope(copied.pop("scope"), project_id=project_id)
    return copied


def test_projects_available_provider_uses_labels_and_canonical_values() -> None:
    import asyncio

    app, query, _command = _app()
    del app
    registry = ServiceRegistry(
        _Runtime(project_query_service=query, project_command_service=_ProjectCommand())
    )

    choices = asyncio.run(registry.choices("projects.available"))

    assert [(choice.value, choice.label) for choice in choices] == [
        ("proj_1", "Project One"),
        ("proj_2", "Project Two"),
    ]
    assert query.per_page == 200
    _assert_cli_scope(query.list_scope)


def test_plain_project_selector_uses_loaded_labels_and_canonical_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.entrypoints.cli.platform import click_adapter

    monkeypatch.setattr(
        click_adapter,
        "_terminal_facts",
        lambda: TerminalFacts(
            stdin_isatty=True,
            stdout_isatty=True,
            columns=70,
            rows=22,
            term="xterm-256color",
            no_color_env=False,
        ),
    )
    app, _query, command = _app()

    result = CliRunner().invoke(
        app,
        ["projects", "dispatch-notification", "--event-type", "workflow.completed", "--plain"],
        input="unknown\n2\ny\n",
    )

    assert result.exit_code == 0, result.output
    assert "Project One" in result.output
    assert "Project Two" in result.output
    assert "'unknown' is not one of" in result.output
    assert command.calls[0][0] == "proj_2"


def test_projects_phase2_leaf_commands_delegate_to_shared_services() -> None:
    app, query, command = _app()
    runner = CliRunner()

    cases = [
        (
            ["projects", "list", "--page", "2", "--per-page", "5", "--search", "one"],
            lambda: (
                (query.page, query.per_page, query.search) == (2, 5, "one")
                and isinstance(query.list_scope, ScopeContext)
            ),
        ),
        (
            ["projects", "show", "--project-id", "proj_1"],
            lambda: (
                query.detail_project_id == "proj_1"
                and isinstance(query.detail_kwargs["scope"], ScopeContext)
            ),
        ),
        (
            ["projects", "notification-routes", "--project-id", "proj_1", "--enabled-only"],
            lambda: (
                query.routes_call[0] == "proj_1"
                and _without_scope(query.routes_call[1], project_id="proj_1")
                == {"enabled_only": True}
            ),
        ),
        (
            [
                "projects",
                "notification-deliveries",
                "--project-id",
                "proj_1",
                "--job-id",
                "job_1",
                "--limit",
                "5",
            ],
            lambda: (
                query.deliveries_call[0] == "proj_1"
                and _without_scope(query.deliveries_call[1], project_id="proj_1")
                == {"job_id": "job_1", "limit": 5}
            ),
        ),
        (
            [
                "projects",
                "dispatch-notification",
                "--project-id",
                "proj_1",
                "--event-type",
                "workflow.completed",
                "--payload",
                '{"ok": true}',
                "--confirm",
                "--plain",
            ],
            lambda: (
                command.calls[-1][0] == "proj_1"
                and command.calls[-1][1].payload == {"ok": True}
                and isinstance(command.dispatch_kwargs["scope"], ScopeContext)
            ),
        ),
    ]

    for args, assertion in cases:
        result = runner.invoke(app, args, input="y\n")
        assert result.exit_code == 0, (args, result.output)
        assert assertion(), args


def test_projects_dispatch_notification_rejects_nested_secret_payload_before_service_call() -> None:
    app, _query, command = _app()
    raw_secret = "notification-secret-value"

    result = CliRunner().invoke(
        app,
        [
            "projects",
            "dispatch-notification",
            "--json",
            "--project-id",
            "proj_1",
            "--event-type",
            "workflow.completed",
            "--payload",
            f'{{"delivery": {{"api_key": "{raw_secret}"}}}}',
        ],
        input="y\n",
    )

    assert result.exit_code == 3
    assert command.calls == []
    assert raw_secret not in result.output


_DISPATCH_ARGS = [
    "projects",
    "dispatch-notification",
    "--project-id",
    "proj_1",
    "--event-type",
    "workflow.completed",
]


def test_projects_dispatch_review_rejection_shows_notification_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.entrypoints.cli.platform import click_adapter

    app, _query, command = _app()
    monkeypatch.setattr(
        click_adapter,
        "_terminal_facts",
        lambda: TerminalFacts(
            stdin_isatty=True,
            stdout_isatty=True,
            columns=80,
            rows=24,
            term="xterm-256color",
            no_color_env=False,
        ),
    )

    result = CliRunner().invoke(
        app,
        [*_DISPATCH_ARGS, "--severity", "warning", "--plain"],
        input="n\n",
    )

    assert result.exit_code == 10
    assert "Project: proj_1" in result.output
    assert "Event type: workflow.completed" in result.output
    assert "Severity: warning" in result.output
    assert (
        "Side effect: project notification dispatch will run for matching routes." in result.output
    )
    assert command.calls == []


def test_projects_dispatch_no_color_passes_mode_decision_to_renderer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.entrypoints.cli.platform import click_adapter

    captured: dict[str, object] = {}

    class CapturingRenderer:
        def render_result(self, _spec: object, _result: object) -> None:
            return None

        def collect_missing(self, _fields: object, values: dict[str, object]) -> dict[str, object]:
            return values

        def confirm_review(self, _spec: object, _values: dict[str, object]) -> bool:
            return True

        @contextmanager
        def operation_progress(self, **_kwargs: object) -> object:
            yield lambda _event: None

    def fake_renderer_for(
        spec: object,
        *,
        json_requested: bool,
        plain_requested: bool,
        color_enabled: bool,
        animation_enabled: bool,
    ) -> CapturingRenderer:
        captured.update(
            {
                "command": getattr(spec, "command_id", ""),
                "json_requested": json_requested,
                "plain_requested": plain_requested,
                "color_enabled": color_enabled,
                "animation_enabled": animation_enabled,
            }
        )
        return CapturingRenderer()

    monkeypatch.setattr(click_adapter, "renderer_for", fake_renderer_for)
    app, _query, command = _app()

    result = CliRunner().invoke(app, [*_DISPATCH_ARGS, "--tui", "--no-color", "--confirm"])

    assert result.exit_code == 0, result.output
    assert command.calls[0][0] == "proj_1"
    _assert_cli_scope(command.dispatch_kwargs["scope"], project_id="proj_1")
    assert captured == {
        "command": "projects.dispatch-notification",
        "json_requested": False,
        "plain_requested": False,
        "color_enabled": False,
        "animation_enabled": False,
    }


def test_projects_dispatch_json_preserves_canonical_result() -> None:
    app, _query, command = _app()

    result = CliRunner().invoke(app, [*_DISPATCH_ARGS, "--json", "--confirm"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["result"]["project_id"] == "proj_1"
    assert payload["result"]["notification_deliveries"] == [{"id": "delivery_1"}]
    _assert_cli_scope(command.dispatch_kwargs["scope"], project_id="proj_1")


def test_projects_dispatch_failure_and_ctrl_c_map_to_stable_exits() -> None:
    app, _query, command = _app()
    command.fail = ServiceUnavailableError("notification runtime offline")
    failure = CliRunner().invoke(app, [*_DISPATCH_ARGS, "--confirm"])

    assert failure.exit_code == 7
    assert "Check the dependent service and retry." in failure.output
    assert _log_path(failure.output).is_file()

    app, _query, command = _app()
    command.fail = KeyboardInterrupt()
    interrupted = CliRunner().invoke(app, [*_DISPATCH_ARGS, "--confirm"])

    assert interrupted.exit_code == 10
    assert "Run the command again when ready." in interrupted.output


def test_projects_cli_does_not_import_subprocess() -> None:
    from core.entrypoints.cli.platform import click_adapter, service_registry

    source = inspect.getsource(click_adapter) + inspect.getsource(service_registry)

    assert "subprocess" not in source
