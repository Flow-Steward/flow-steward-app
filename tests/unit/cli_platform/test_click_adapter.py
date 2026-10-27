"""Contract tests for the generated public Click command tree."""

from __future__ import annotations

import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner


class _Registry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def choices(self, _provider: str) -> tuple[object, ...]:
        return ()

    async def invoke(
        self,
        key: str,
        value: object,
        *,
        progress_sink: object | None = None,
        technical_sink: object | None = None,
    ) -> dict[str, object]:
        del progress_sink, technical_sink
        self.calls.append((key, value))
        if key == "users.create":
            return {"status": "created", "user": {"id": "u_1"}}
        return {"users": [], "total": 0}


class _PermissionRegistry(_Registry):
    async def invoke(
        self,
        key: str,
        value: object,
        *,
        progress_sink: object | None = None,
        technical_sink: object | None = None,
    ) -> dict[str, object]:
        del key, value, progress_sink, technical_sink
        raise PermissionError("not allowed")


def _app() -> tuple[object, _Registry]:
    from core.entrypoints.cli.platform.click_adapter import build_click_app

    registry = _Registry()
    return build_click_app(registry, SimpleNamespace()), registry


def test_generated_tree_and_help_are_public_and_structured() -> None:
    app, _registry = _app()
    runner = CliRunner()

    root = runner.invoke(app, ["--help"])
    users = runner.invoke(app, ["users", "--help"])
    create = runner.invoke(app, ["users", "create", "--help"])
    extension = runner.invoke(app, ["extensions", "create", "--help"])

    assert root.exit_code == 0
    assert "users" in root.output
    assert "extensions" in root.output
    assert "projects" in root.output
    assert "tooling" not in root.output
    assert " db" not in root.output
    assert "list" in users.output
    assert "create" in users.output
    assert "reindex" not in users.output
    assert "Required inputs" in create.output
    assert "Optional inputs" in create.output
    assert "Advanced inputs" in create.output
    assert "Required" in create.output
    assert "Optional" in create.output
    assert "admin" in create.output
    assert "operator" in create.output
    assert "viewer" in create.output
    assert "--password " not in create.output
    assert "--password\n" not in create.output
    assert "--password-file" in create.output
    assert "--password-stdin" in create.output
    for expected in (
        "--json",
        "--plain",
        "--tui",
        "--no-color",
        "--non-interactive",
        "--input-json",
        "--help",
        "-h",
    ):
        assert expected in create.output
    assert ".flow-steward/secret-input/" in create.output
    assert "stdin" in create.output
    assert "lowercase dot-separated" in extension.output
    assert "acme.example" in extension.output


def test_help_alias_and_version_are_public_root_options() -> None:
    app, _registry = _app()
    runner = CliRunner()

    short_help = runner.invoke(app, ["-h"])
    version = runner.invoke(app, ["--version"])

    assert short_help.exit_code == 0
    assert "Flow Steward public management commands." in short_help.output
    assert version.exit_code == 0
    assert "flow-steward" in version.output


def test_global_options_are_accepted_at_each_supported_depth() -> None:
    app, _registry = _app()
    runner = CliRunner()

    for arguments in (
        ["--json", "users", "list"],
        ["users", "--json", "list"],
        ["users", "list", "--json"],
        ["--plain", "users", "list"],
        ["--tui", "users", "list"],
        ["users", "--tui", "list"],
        ["users", "list", "--tui"],
        ["users", "--no-color", "list"],
        ["users", "list", "--verbose"],
        ["--non-interactive", "users", "list"],
        ["users", "--non-interactive", "list"],
        ["users", "list", "--non-interactive"],
    ):
        result = runner.invoke(app, arguments)
        assert result.exit_code == 0, result.output


def test_leaf_only_input_sources_and_root_only_version_are_not_blindly_duplicated() -> None:
    app, _registry = _app()
    runner = CliRunner()

    root_help = runner.invoke(app, ["--help"]).output
    group_help = runner.invoke(app, ["users", "--help"]).output
    read_leaf_help = runner.invoke(app, ["users", "list", "--help"]).output
    secret_leaf_help = runner.invoke(app, ["users", "create", "--help"]).output
    extension_leaf_help = runner.invoke(app, ["extensions", "enable", "--help"]).output

    assert "--version" in root_help
    assert "--version" not in group_help
    assert "--version" not in read_leaf_help
    assert "--version" not in extension_leaf_help
    assert "--extension-version" in extension_leaf_help
    assert "--input-json" not in root_help
    assert "--input-json" not in group_help
    assert "--input-json" not in read_leaf_help
    assert "--input-json" in secret_leaf_help
    assert "--password-file" not in read_leaf_help
    assert "--password-stdin" not in read_leaf_help
    assert "--password-file" in secret_leaf_help
    assert "--password-stdin" in secret_leaf_help
    assert "--confirm" not in root_help
    assert "--confirm" not in group_help
    assert "--confirm" not in read_leaf_help
    assert "--confirm" in secret_leaf_help


def test_input_json_help_explains_format_precedence_and_secret_boundary() -> None:
    app, _registry = _app()

    help_text = CliRunner().invoke(app, ["users", "create", "--help"]).output

    assert "Read non-secret input fields from a JSON object file" in help_text
    assert "Explicit command options override values from this file" in help_text
    assert "Secrets are not accepted" in help_text


def test_approved_cli_spec_documents_flag_availability_by_level() -> None:
    spec_path = Path(__file__).parents[3] / "docs/contracts/cli.md"
    text = spec_path.read_text(encoding="utf-8")

    assert "Every command inherits" not in text
    assert "| CLI level | Available parameters |" in text
    assert "| Root (`flow-steward`) |" in text
    assert "| Command group (`flow-steward users`) |" in text
    assert "| Leaf command (`flow-steward users create`) |" in text
    assert "`--input-json PATH` is leaf-only" in text
    assert "global flags behave identically at every command depth" not in text
    assert "presentation/runtime flags behave identically at every command depth" in text


def test_textual_form_receives_color_and_animation_mode_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("textual")

    from core.application.management.command_specs.users import USERS_CREATE_SPEC
    from core.entrypoints.cli.platform import click_adapter
    from core.entrypoints.cli.tui import form_app

    captured: dict[str, object] = {}

    class FakeRegistry:
        async def choices(self, _provider: str) -> tuple[object, ...]:
            return ()

    class FakeCommandFormApp:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def run(self) -> form_app.FormOutcome:
            return form_app.FormOutcome(status="cancelled")

    monkeypatch.setattr(form_app, "CommandFormApp", FakeCommandFormApp)

    outcome = click_adapter._run_textual_form(
        spec=USERS_CREATE_SPEC,
        initial_values={},
        registry=FakeRegistry(),
        terminal_size=(80, 24),
        color_enabled=False,
        animation_enabled=False,
    )

    assert outcome == form_app.FormOutcome(status="cancelled")
    assert captured["color_enabled"] is False
    assert captured["animation_enabled"] is False


def test_valid_always_review_command_uses_textual_review_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.entrypoints.cli.platform import click_adapter
    from core.entrypoints.cli.platform.modes import TerminalFacts

    app, registry = _app()
    textual_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        click_adapter,
        "_terminal_facts",
        lambda: TerminalFacts(
            stdin_isatty=True,
            stdout_isatty=True,
            columns=100,
            rows=30,
            term="xterm-256color",
            no_color_env=False,
        ),
    )

    def _fake_textual_form(**kwargs: object) -> object:
        textual_calls.append(dict(kwargs))
        return SimpleNamespace(status="confirmed", values={})

    monkeypatch.setattr(click_adapter, "_run_textual_form", _fake_textual_form)

    result = CliRunner().invoke(
        app,
        [
            "--tui",
            "users",
            "create",
            "--email",
            "operator@example.com",
            "--role",
            "viewer",
            "--password-stdin",
        ],
        input="StrongPass_123!\n",
    )

    assert result.exit_code == 0, result.output
    assert textual_calls
    assert registry.calls[0][0] == "users.create"


def test_small_textual_terminal_announces_plain_wizard_before_missing_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.entrypoints.cli.platform import click_adapter
    from core.entrypoints.cli.platform.modes import TerminalFacts

    app, registry = _app()
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

    result = CliRunner().invoke(app, ["users", "create", "--tui"])

    assert result.exit_code != 0
    assert "using the plain interactive wizard" in result.output
    assert registry.calls == []


def test_default_interactive_command_uses_plain_input_and_refuses_cancelled_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.entrypoints.cli.platform import click_adapter
    from core.entrypoints.cli.platform.modes import TerminalFacts

    app, registry = _app()
    monkeypatch.setattr(
        click_adapter, "_terminal_facts", lambda: TerminalFacts(True, True, 100, 30, "xterm", False)
    )

    def forbidden_form(**_kwargs: object) -> None:
        pytest.fail("Default CLI must not open a full-screen form")

    monkeypatch.setattr(click_adapter, "_run_textual_form", forbidden_form)
    result = CliRunner().invoke(app, ["users", "create"], input="")
    assert result.exit_code != 0, result.output
    assert "Email" in result.output
    assert "\x1b[" not in result.output
    assert registry.calls == []


def test_syntax_and_validation_fail_without_invoking_a_service() -> None:
    app, registry = _app()
    runner = CliRunner()

    for command in ("tool", "tooling", "db", "ext", "i18n"):
        result = runner.invoke(app, [command])
        assert result.exit_code == 2
    assert runner.invoke(app, ["users", "list", "--", "opaque"]).exit_code == 2
    assert runner.invoke(app, ["users", "create", "--password", "nope"]).exit_code == 2
    assert runner.invoke(app, ["users", "create", "--email", "not-an-email"]).exit_code == 3
    assert registry.calls == []


def test_click_values_populate_resolution_and_json_syntax_errors_are_enveloped() -> None:
    app, registry = _app()
    runner = CliRunner()

    success = runner.invoke(app, ["users", "list", "--search", "operator"])
    assert success.exit_code == 0, success.output
    assert registry.calls[0][0] == "users.list"
    assert registry.calls[0][1].model_dump() == {
        "search": "operator",
        "include_inactive": False,
        "include_deleted": False,
        "limit": 50,
        "offset": 0,
    }

    failure = runner.invoke(app, ["users", "list", "--json", "--unknown"])
    assert failure.exit_code == 2
    assert '"ok": false' in failure.output
    payload = json.loads(failure.output)
    assert payload["command"] == "users.list"
    assert payload["operation_id"] is None
    assert payload["log_path"] is None

    nested_failure = runner.invoke(app, ["--json", "users", "create", "--unknown"])
    assert nested_failure.exit_code == 2
    assert json.loads(nested_failure.output)["command"] == "users.create"


def test_all_json_pre_execution_failures_use_the_normative_failure_envelope() -> None:
    app, _registry = _app()
    runner = CliRunner()

    results = (
        runner.invoke(app, ["users", "create", "--json", "--email", "not-an-email"]),
        runner.invoke(
            app,
            [
                "users",
                "create",
                "--json",
                "--input-json",
                "-",
                "--password-stdin",
            ],
            input='{"email":"operator@example.com"}',
        ),
    )

    for result in results:
        payload = json.loads(result.output)
        assert payload["schema"] == "flow-steward.cli.v1"
        assert payload["ok"] is False
        assert set(payload) == {"schema", "ok", "command", "error", "operation_id", "log_path"}
        assert payload["error"]["retryable"] is False
        assert payload["error"]["field_issues"]
        assert payload["operation_id"] is None
        assert payload["log_path"] is None


def test_callback_returns_the_central_mapped_exit_code() -> None:
    from core.entrypoints.cli.platform.click_adapter import build_click_app

    runner = CliRunner()
    result = runner.invoke(
        build_click_app(_PermissionRegistry(), SimpleNamespace()), ["users", "list", "--json"]
    )

    assert result.exit_code == 5
    payload = json.loads(result.output)
    assert payload["error"]["code"] == "permission_denied"
    assert payload["error"]["retryable"] is False


@pytest.mark.parametrize(
    "command",
    [
        "users issue-bootstrap-token",
        "extensions install",
        "extensions uninstall",
        "extensions test",
        "testing smoke-test",
        "testing nightfire-regression",
        "testing fuzz-api",
        "testing fuzz-mcp",
        "testing fuzz-ui",
        "testing cutover-preflight",
        "testing cutover-smoke",
        "quality lint",
        "quality release-gate",
    ],
)
def test_delegated_command_help_does_not_initialize_or_invoke_the_runtime(command: str) -> None:
    from core.entrypoints.cli.click_app import _LazyCLIRuntime
    from core.entrypoints.cli.platform.click_adapter import build_click_app
    from core.entrypoints.cli.platform.service_registry import ServiceRegistry

    runtime = _LazyCLIRuntime()
    result = CliRunner().invoke(
        build_click_app(ServiceRegistry(runtime), runtime), [*command.split(), "--help"]
    )

    assert result.exit_code == 0, result.output
    assert f"Usage: flow-steward {command}" in result.output
    assert runtime._runtime is None


def test_click_module_entrypoint_uses_public_prog_name_and_clean_stderr() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "core.entrypoints.cli.click_app", "--help"],
        cwd=Path(__file__).parents[3],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0
    assert "Usage: flow-steward" in result.stdout
    assert "python -m core.entrypoints.cli.click_app" not in result.stdout
    assert "UserWarning" not in result.stderr
    assert result.stderr == ""


@pytest.mark.parametrize("setup_failure", ("construction", "initial_write"))
def test_json_log_setup_failures_are_emitted_as_one_central_failure_envelope(
    monkeypatch: pytest.MonkeyPatch, setup_failure: str
) -> None:
    from core.application.management.command_specs.contracts import OperationType
    from core.application.management.command_specs.users import USERS_LIST_SPEC
    from core.entrypoints.cli.platform import click_adapter

    spec = USERS_LIST_SPEC.model_copy(update={"operation_type": OperationType.CHANGE})
    monkeypatch.setattr(click_adapter, "PUBLIC_COMMAND_SPECS", (spec,))

    if setup_failure == "construction":

        def fail_construction(_command_id: str) -> None:
            raise OSError("operation log is unavailable")

        monkeypatch.setattr(click_adapter, "OperationLog", fail_construction)
    else:

        class FailingInitialWriteLog:
            operation_id = "partial-operation"
            path = Path("partial-operation.log")

            def __init__(self, _command_id: str) -> None:
                pass

            def write(self, _message: str) -> None:
                raise OSError("operation log is unavailable")

            def close(self) -> None:
                pass

        monkeypatch.setattr(click_adapter, "OperationLog", FailingInitialWriteLog)

    registry = _Registry()
    result = CliRunner().invoke(
        click_adapter.build_click_app(registry, SimpleNamespace()), ["users", "list", "--json"]
    )

    # A mutating command still refuses to run without its own record, but the
    # refusal names the reason: an unwritable state directory is an environment
    # problem an operator can fix, not an unexpected internal error.
    assert result.exit_code == 4
    assert registry.calls == []
    lines = result.output.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["error"]["code"] == "operation_log_unavailable"
    assert "operation log is unavailable" in payload["error"]["message"]
    assert ".flow-steward/logs" in payload["error"]["message"]
    assert payload["error"]["hint"]
    assert payload["operation_id"] is None
    assert payload["log_path"] is None
    assert "Traceback" not in result.output
    assert "running:" not in result.output


def test_json_progress_setup_failure_is_emitted_as_one_central_failure_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.application.management.command_specs.contracts import OperationType
    from core.application.management.command_specs.users import USERS_LIST_SPEC
    from core.entrypoints.cli.platform import click_adapter
    from core.entrypoints.cli.platform.renderers import JSONRenderer

    spec = USERS_LIST_SPEC.model_copy(update={"operation_type": OperationType.CHANGE})
    monkeypatch.setattr(click_adapter, "PUBLIC_COMMAND_SPECS", (spec,))

    def fail_progress_setup(self: JSONRenderer, **_kwargs: object) -> object:
        raise RuntimeError("progress renderer is unavailable")

    monkeypatch.setattr(JSONRenderer, "operation_progress", fail_progress_setup)
    registry = _Registry()
    result = CliRunner().invoke(
        click_adapter.build_click_app(registry, SimpleNamespace()), ["users", "list", "--json"]
    )

    assert result.exit_code == 70
    assert registry.calls == []
    lines = result.output.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["error"]["code"] == "internal_error"
    assert payload["operation_id"] is None
    assert payload["log_path"] is None
    assert "Traceback" not in result.output
    assert "running:" not in result.output


@pytest.mark.parametrize(
    ("operation_type", "expected_exit_code"),
    (("read", 8), ("change", 9)),
)
def test_json_progress_teardown_failure_uses_post_invocation_operation_risk(
    monkeypatch: pytest.MonkeyPatch,
    operation_type: str,
    expected_exit_code: int,
) -> None:
    from core.application.management.command_specs.contracts import OperationType
    from core.application.management.command_specs.users import USERS_LIST_SPEC
    from core.entrypoints.cli.platform import click_adapter
    from core.entrypoints.cli.platform.renderers import JSONRenderer

    spec = USERS_LIST_SPEC.model_copy(update={"operation_type": OperationType(operation_type)})
    monkeypatch.setattr(click_adapter, "PUBLIC_COMMAND_SPECS", (spec,))

    @contextmanager
    def fail_after_progress(self: JSONRenderer, **_kwargs: object):
        del self
        yield lambda _event: None
        raise OSError("progress teardown failed")

    monkeypatch.setattr(JSONRenderer, "operation_progress", fail_after_progress)
    registry = _Registry()
    result = CliRunner().invoke(
        click_adapter.build_click_app(registry, SimpleNamespace()),
        ["users", "list", "--json"],
    )

    assert len(registry.calls) == 1
    assert result.exit_code == expected_exit_code
    lines = result.output.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["error"]["code"] == "internal_error"


@pytest.mark.parametrize(
    ("operation_type", "expected_exit_code"),
    (("read", 8), ("change", 9)),
)
def test_json_result_renderer_failure_uses_central_fallback_envelope(
    monkeypatch: pytest.MonkeyPatch,
    operation_type: str,
    expected_exit_code: int,
) -> None:
    from core.application.management.command_specs.contracts import OperationType
    from core.application.management.command_specs.users import USERS_LIST_SPEC
    from core.entrypoints.cli.platform import click_adapter
    from core.entrypoints.cli.platform.renderers import JSONRenderer

    spec = USERS_LIST_SPEC.model_copy(update={"operation_type": OperationType(operation_type)})
    monkeypatch.setattr(click_adapter, "PUBLIC_COMMAND_SPECS", (spec,))
    monkeypatch.setattr(
        JSONRenderer,
        "render_result",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("render failed")),
    )
    registry = _Registry()
    result = CliRunner().invoke(
        click_adapter.build_click_app(registry, SimpleNamespace()),
        ["users", "list", "--json"],
    )

    assert len(registry.calls) == 1
    assert result.exit_code == expected_exit_code
    lines = result.output.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["error"]["code"] == "internal_error"
    assert payload["error"]["message"] == "An unexpected internal error occurred."


@pytest.mark.parametrize("failure_point", ("post_write", "close"))
def test_json_post_invocation_log_failure_uses_mutation_risk(
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    from core.application.management.command_specs.contracts import OperationType
    from core.application.management.command_specs.users import USERS_LIST_SPEC
    from core.entrypoints.cli.platform import click_adapter

    spec = USERS_LIST_SPEC.model_copy(update={"operation_type": OperationType.CHANGE})
    monkeypatch.setattr(click_adapter, "PUBLIC_COMMAND_SPECS", (spec,))

    class FailingPostInvocationLog:
        operation_id = "post-invocation-operation"
        path = Path("post-invocation-operation.log")

        def __init__(self, _command_id: str) -> None:
            pass

        def write(self, message: str) -> None:
            if failure_point == "post_write" and message.startswith("exit_code="):
                raise OSError("post-service operation log write failed")

        def close(self) -> None:
            if failure_point == "close":
                raise OSError("operation log close failed")

    monkeypatch.setattr(click_adapter, "OperationLog", FailingPostInvocationLog)
    registry = _Registry()
    result = CliRunner().invoke(
        click_adapter.build_click_app(registry, SimpleNamespace()),
        ["users", "list", "--json"],
    )

    assert len(registry.calls) == 1
    assert result.exit_code == 9
    lines = result.output.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["error"]["code"] == "internal_error"
    assert payload["operation_id"] == "post-invocation-operation"


def test_read_tooling_result_gets_operation_log_for_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.application.tooling.contracts import ToolRunResult
    from core.entrypoints.cli.platform import click_adapter

    writes: list[str] = []

    class FakeOperationLog:
        operation_id = "op-read-tooling"
        path = Path(".flow-steward/logs/op-read-tooling.log")

        def __init__(self, command_id: str) -> None:
            writes.append(f"init:{command_id}")

        def write(self, message: str) -> None:
            writes.append(message)

        def close(self) -> None:
            writes.append("close")

    class ToolingRegistry:
        async def invoke(
            self,
            key: str,
            value: object,
            *,
            progress_sink: object | None = None,
            technical_sink: object | None = None,
        ) -> object:
            del value, progress_sink, technical_sink
            assert key == "quality.check-ci-excludes"
            return ToolRunResult(
                operation="quality.check-ci-excludes",
                status="completed",
                exit_code=0,
                stdout="short diagnostic",
                stdout_full="full diagnostic line\n",
            )

    monkeypatch.setattr(click_adapter, "OperationLog", FakeOperationLog)

    result = CliRunner().invoke(
        click_adapter.build_click_app(ToolingRegistry(), SimpleNamespace()),
        ["quality", "check-ci-excludes", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["operation_id"] == "op-read-tooling"
    assert payload["log_path"] == ".flow-steward/logs/op-read-tooling.log"
    assert "stdout:" in writes
    assert any("full diagnostic line" in line for line in writes)


def test_json_verbose_keeps_one_stdout_document_and_redacts_stderr_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.application.tooling.contracts import ToolRunResult
    from core.entrypoints.cli.platform import click_adapter

    class FakeOperationLog:
        operation_id = "op-json-verbose"
        path = Path(".flow-steward/logs/op-json-verbose.log")

        def __init__(self, _command_id: str) -> None:
            pass

        def write(self, _message: str) -> None:
            pass

        def close(self) -> None:
            pass

    class ToolingRegistry:
        async def invoke(
            self,
            key: str,
            value: object,
            *,
            progress_sink: object | None = None,
            technical_sink: object | None = None,
        ) -> object:
            del value, progress_sink, technical_sink
            print('legacy payload={"api_key": "printed-secret"}')
            return ToolRunResult(
                operation=key,
                status="completed",
                exit_code=0,
                stdout='payload={"api_key": "json-secret"}',
            )

    monkeypatch.setattr(click_adapter, "OperationLog", FakeOperationLog)
    result = CliRunner().invoke(
        click_adapter.build_click_app(ToolingRegistry(), SimpleNamespace()),
        ["quality", "check-ci-excludes", "--json", "--verbose"],
    )

    assert result.exit_code == 0, result.output
    assert len(result.stdout.splitlines()) == 1
    assert json.loads(result.stdout)["ok"] is True
    assert "stdout:" in result.stderr
    assert "[REDACTED]" in result.stderr
    for secret in ("json-secret", "printed-secret"):
        assert secret not in result.stdout
        assert secret not in result.stderr


def test_json_verbose_internal_error_is_generic_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.entrypoints.cli.platform import click_adapter

    class FailingRegistry:
        async def invoke(
            self,
            _key: str,
            _value: object,
            *,
            progress_sink: object | None = None,
            technical_sink: object | None = None,
        ) -> object:
            del progress_sink, technical_sink
            raise RuntimeError(
                "input_value={'password': 'python-secret'}; payload={\"api_key\": \"json-secret\"}"
            )

    monkeypatch.setattr(
        click_adapter,
        "PUBLIC_COMMAND_SPECS",
        tuple(
            spec for spec in click_adapter.PUBLIC_COMMAND_SPECS if spec.command_id == "users.list"
        ),
    )
    result = CliRunner().invoke(
        click_adapter.build_click_app(FailingRegistry(), SimpleNamespace()),
        ["users", "list", "--json", "--verbose"],
    )

    assert result.exit_code == 8
    assert len(result.stdout.splitlines()) == 1
    payload = json.loads(result.stdout)
    assert payload["error"]["message"] == "An unexpected internal error occurred."
    assert "[REDACTED]" in result.stderr
    for secret in ("python-secret", "json-secret"):
        assert secret not in result.stdout
        assert secret not in result.stderr


def test_json_verbose_internal_error_redacts_escaped_mapping_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.entrypoints.cli.platform import click_adapter

    class FailingRegistry:
        async def invoke(
            self,
            _key: str,
            _value: object,
            *,
            progress_sink: object | None = None,
            technical_sink: object | None = None,
        ) -> object:
            del progress_sink, technical_sink
            raise RuntimeError(
                r'payload={"api\u005fkey": "json-secret"}; '
                r"input_value={'pass\x77ord': 'python-secret'}"
            )

    monkeypatch.setattr(
        click_adapter,
        "PUBLIC_COMMAND_SPECS",
        tuple(
            spec for spec in click_adapter.PUBLIC_COMMAND_SPECS if spec.command_id == "users.list"
        ),
    )
    result = CliRunner().invoke(
        click_adapter.build_click_app(FailingRegistry(), SimpleNamespace()),
        ["users", "list", "--json", "--verbose"],
    )

    assert result.exit_code == 8
    assert json.loads(result.stdout)["error"]["message"] == (
        "An unexpected internal error occurred."
    )
    assert "[REDACTED]" in result.stderr
    for secret in ("python-secret", "json-secret"):
        assert secret not in result.stdout
        assert secret not in result.stderr


def test_mutating_command_requires_confirmation_after_complete_validation() -> None:
    app, registry = _app()
    runner = CliRunner()

    incomplete = runner.invoke(app, ["users", "create", "--email", "operator@example.com"])
    assert incomplete.exit_code == 3
    assert registry.calls == []

    unconfirmed = runner.invoke(
        app,
        [
            "users",
            "create",
            "--email",
            "operator@example.com",
            "--role",
            "viewer",
            "--password-stdin",
        ],
        input="StrongPass_123!\n",
    )

    assert unconfirmed.exit_code == 10
    assert registry.calls == []
    assert "requires confirmation" in unconfirmed.output

    confirmed = runner.invoke(
        app,
        [
            "users",
            "create",
            "--email",
            "operator@example.com",
            "--role",
            "viewer",
            "--password-stdin",
            "--confirm",
        ],
        input="StrongPass_123!\n",
    )

    assert confirmed.exit_code == 0
    assert registry.calls[0][0] == "users.create"
