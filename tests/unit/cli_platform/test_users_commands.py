"""Contract tests for schema-driven ``users`` public CLI commands."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from core.application.auth.user_admin.contracts import (
    UserActiveUpdateInput,
    UserCreateInput,
    UserPasswordUpdateInput,
    UserRolesUpdateInput,
    UserSelectorInput,
)
from core.application.management.errors import (
    ServiceConflictError,
    ServiceUnavailableError,
    ServiceValidationError,
)
from core.entrypoints.cli.platform.modes import TerminalFacts


class _UsersRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.choice_calls: list[str] = []
        self.fail: BaseException | None = None

    async def invoke(
        self,
        key: str,
        value: object,
        *,
        progress_sink: object | None = None,
        technical_sink: object | None = None,
    ) -> dict[str, object]:
        del progress_sink, technical_sink
        if self.fail is not None:
            raise self.fail
        self.calls.append((key, value))
        if key == "users.list":
            return {
                "users": [
                    {
                        "id": "u_1",
                        "username": "operator@example.com",
                        "email": "operator@example.com",
                        "roles": ["admin", "viewer"],
                        "is_active": True,
                        "must_change_password": False,
                    }
                ],
                "total": 1,
            }
        if key == "users.create":
            command = value
            assert isinstance(command, UserCreateInput)
            return {
                "status": "created",
                "user": {
                    "id": "u_2",
                    "username": command.email,
                    "email": command.email,
                    "password_hash": "must-not-cross-application-boundary",
                    "roles": command.roles,
                },
            }
        if key == "users.set-password":
            assert isinstance(value, UserPasswordUpdateInput)
            return {
                "status": "password_updated",
                "user": {"id": value.user_id or "u_1", "email": value.email},
            }
        if key == "users.force-reset":
            assert isinstance(value, UserSelectorInput)
            return {
                "status": "password_reset_required",
                "user": {"id": value.user_id or "u_1", "email": value.email},
            }
        if key == "users.unlock":
            assert isinstance(value, UserSelectorInput)
            return {
                "status": "unlocked",
                "user": {"id": value.user_id or "u_1", "email": value.email},
            }
        if key == "users.set-active":
            assert isinstance(value, UserActiveUpdateInput)
            return {
                "status": "updated",
                "user": {
                    "id": value.user_id or "u_1",
                    "email": value.email,
                    "is_active": value.is_active,
                },
            }
        if key == "users.set-roles":
            assert isinstance(value, UserRolesUpdateInput)
            return {
                "status": "updated",
                "user": {
                    "id": value.user_id or "u_1",
                    "email": value.email,
                    "roles": value.roles,
                },
            }
        raise AssertionError(f"Unexpected service key {key!r}")

    async def choices(self, key: str) -> tuple[object, ...]:
        from core.application.management.command_specs.contracts import ChoiceItem

        self.choice_calls.append(key)
        if key == "accounts.available":
            return (
                ChoiceItem(value="acc.prod", label="Production"),
                ChoiceItem(value="acc.sandbox", label="Sandbox"),
            )
        return ()


def _app(registry: _UsersRegistry | None = None) -> tuple[object, _UsersRegistry]:
    from core.entrypoints.cli.platform.click_adapter import build_click_app

    registry = registry or _UsersRegistry()
    return build_click_app(registry, SimpleNamespace()), registry


def _payload(output: str) -> dict[str, Any]:
    return json.loads(output.splitlines()[-1])


def test_users_group_help_exposes_exact_phase2_leaves() -> None:
    app, _registry = _app()

    result = CliRunner().invoke(app, ["users", "--help"])

    assert result.exit_code == 0, result.output
    for command in (
        "list",
        "create",
        "set-password",
        "force-reset",
        "unlock",
        "set-active",
        "set-roles",
        "issue-bootstrap-token",
    ):
        assert command in result.output
    assert "force-password-reset" not in result.output


def test_users_role_help_distinguishes_create_default_from_required_set_roles() -> None:
    app, _registry = _app()
    runner = CliRunner()

    create_help = runner.invoke(app, ["users", "create", "--help"])
    set_roles_help = runner.invoke(app, ["users", "set-roles", "--help"])

    assert create_help.exit_code == 0, create_help.output
    assert set_roles_help.exit_code == 0, set_roles_help.output
    assert "Default: viewer." in create_help.output
    assert "('viewer',)" not in create_help.output
    assert "Default: viewer." not in set_roles_help.output
    assert "--role" in set_roles_help.output
    assert "Required" in set_roles_help.output


@pytest.mark.parametrize(
    "leaf",
    ("set-password", "force-reset", "unlock", "set-active", "set-roles"),
)
def test_existing_user_help_declares_required_exactly_one_target(leaf: str) -> None:
    app, _registry = _app()

    result = CliRunner().invoke(app, ["users", leaf, "--help"])

    assert result.exit_code == 0, result.output
    assert "Target — required, choose exactly one" in result.output
    assert "--user-id" in result.output
    assert "--email" in result.output


@pytest.mark.parametrize(
    ("arguments", "columns", "rows", "fallback_expected"),
    (
        (("users", "force-reset", "--plain"), 80, 24, False),
        (("users", "force-reset", "--tui"), 70, 22, True),
        (("users", "force-reset"), 40, 10, False),
    ),
)
def test_plain_user_wizard_collects_exactly_one_target(
    monkeypatch: pytest.MonkeyPatch,
    arguments: tuple[str, ...],
    columns: int,
    rows: int,
    fallback_expected: bool,
) -> None:
    from core.entrypoints.cli.platform import click_adapter

    monkeypatch.setattr(
        click_adapter,
        "_terminal_facts",
        lambda: TerminalFacts(
            stdin_isatty=True,
            stdout_isatty=True,
            columns=columns,
            rows=rows,
            term="xterm-256color",
            no_color_env=False,
        ),
    )
    app, registry = _app()

    result = CliRunner().invoke(
        app,
        list(arguments),
        input="2\noperator@example.com\ny\n",
    )

    assert result.exit_code == 0, result.output
    assert "Target — required, choose exactly one" in result.output
    assert "1. User ID" in result.output
    assert "2. Email" in result.output
    assert ("using the plain interactive wizard" in result.output) is fallback_expected
    key, command = registry.calls[0]
    assert key == "users.force-reset"
    assert isinstance(command, UserSelectorInput)
    assert command.user_id is None
    assert command.email == "operator@example.com"


@pytest.mark.parametrize(
    ("model", "extra"),
    (
        (UserSelectorInput, {}),
        (UserPasswordUpdateInput, {"password": "StrongPass_123!"}),
        (UserActiveUpdateInput, {"is_active": True}),
        (UserRolesUpdateInput, {"roles": ["viewer"]}),
    ),
)
@pytest.mark.parametrize(
    "selector",
    (
        {},
        {"user_id": "   ", "email": ""},
        {"user_id": "u_1", "email": "operator@example.com"},
    ),
)
def test_existing_user_contracts_require_exactly_one_nonblank_selector(
    model: type[object], extra: dict[str, object], selector: dict[str, object]
) -> None:
    with pytest.raises(ValidationError, match="exactly one of user_id or email"):
        model(**selector, **extra)


def test_user_role_contracts_keep_create_default_but_require_set_roles_values() -> None:
    created = UserCreateInput(email="operator@example.com", password="StrongPass_123!")

    assert created.roles == ["viewer"]
    with pytest.raises(ValidationError, match="at least one role"):
        UserRolesUpdateInput(user_id="u_1", roles=[])


@pytest.mark.parametrize(
    ("leaf", "required_args", "stdin"),
    (
        ("set-password", ["--password-stdin"], "StrongPass_123!\n"),
        ("force-reset", [], ""),
        ("unlock", [], ""),
        ("set-active", ["--is-active"], ""),
        ("set-roles", ["--role", "viewer"], ""),
    ),
)
@pytest.mark.parametrize(
    "selector_args",
    (
        [],
        ["--user-id", "u_1", "--email", "operator@example.com"],
    ),
)
def test_existing_user_mutations_reject_invalid_selectors_before_review_or_service(
    leaf: str,
    required_args: list[str],
    stdin: str,
    selector_args: list[str],
) -> None:
    app, registry = _app()

    result = CliRunner().invoke(
        app,
        [
            "--non-interactive",
            "--json",
            "users",
            leaf,
            *selector_args,
            *required_args,
            "--confirm",
        ],
        input=stdin,
    )

    assert result.exit_code == 3, result.output
    assert registry.calls == []
    assert "Review pending change" not in result.output
    failure = _payload(result.output)
    assert failure["error"]["field_issues"] == [
        {
            "field": "target",
            "message": "Target is required; choose exactly one of User ID or Email.",
            "code": "exactly_one_of",
        }
    ]


def test_set_roles_rejects_missing_or_empty_roles_before_service() -> None:
    app, registry = _app()
    runner = CliRunner()

    missing = runner.invoke(
        app,
        [
            "--non-interactive",
            "--json",
            "users",
            "set-roles",
            "--user-id",
            "u_1",
            "--confirm",
        ],
    )

    assert missing.exit_code == 3, missing.output
    assert registry.calls == []


def test_set_roles_aggregates_missing_roles_and_target_before_service() -> None:
    app, registry = _app()

    result = CliRunner().invoke(
        app,
        [
            "--non-interactive",
            "--json",
            "users",
            "set-roles",
            "--confirm",
        ],
    )

    assert result.exit_code == 3, result.output
    assert registry.calls == []
    assert [
        (issue["field"], issue["code"])
        for issue in _payload(result.output)["error"]["field_issues"]
    ] == [
        ("roles", "missing"),
        ("target", "exactly_one_of"),
    ]


def test_input_json_errors_aggregate_with_missing_required_issues_in_stable_order(
    tmp_path: Path,
) -> None:
    input_json = tmp_path / "invalid-input.json"
    input_json.write_text(json.dumps({"unknown": "value"}), encoding="utf-8")
    app, registry = _app()

    result = CliRunner().invoke(
        app,
        [
            "--non-interactive",
            "--json",
            "users",
            "create",
            "--input-json",
            str(input_json),
        ],
    )

    assert result.exit_code == 3, result.output
    assert registry.calls == []
    assert [
        (issue["field"], issue["code"])
        for issue in _payload(result.output)["error"]["field_issues"]
    ] == [
        ("unknown", "unknown_field"),
        ("email", "missing"),
        ("password", "missing"),
    ]


def test_non_utf8_input_json_returns_validation_without_service_call(
    tmp_path: Path,
) -> None:
    input_json = tmp_path / "non-utf8.json"
    input_json.write_bytes(b'{"email":"operator@example.com"}\xff')
    app, registry = _app()

    result = CliRunner().invoke(
        app,
        [
            "--non-interactive",
            "--json",
            "users",
            "create",
            "--input-json",
            str(input_json),
        ],
    )

    assert result.exit_code == 3
    assert registry.calls == []
    payload = _payload(result.stdout)
    assert ("input_json", "invalid_json") in {
        (issue["field"], issue["code"]) for issue in payload["error"]["field_issues"]
    }


def test_users_list_executes_immediately_and_renders_plain_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.entrypoints.cli.platform import click_adapter

    monkeypatch.setattr(
        click_adapter,
        "_run_textual_form",
        lambda *_args, **_kwargs: pytest.fail("users list must not open Textual"),
        raising=False,
    )
    app, registry = _app()

    result = CliRunner().invoke(
        app,
        [
            "users",
            "list",
            "--plain",
            "--search",
            "operator",
            "--include-inactive",
            "--limit",
            "25",
            "--offset",
            "50",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(registry.calls) == 1
    key, command = registry.calls[0]
    assert key == "users.list"
    assert command.model_dump() == {
        "search": "operator",
        "include_inactive": True,
        "include_deleted": False,
        "limit": 25,
        "offset": 50,
    }
    assert "u_1" in result.output
    assert "operator@example.com" in result.output
    assert "admin,viewer" in result.output
    assert "active=true" in result.output
    assert "reset=false" in result.output


def test_users_list_json_envelope_and_dependency_exit_code() -> None:
    app, registry = _app()
    success = CliRunner().invoke(app, ["users", "list", "--json", "--include-deleted"])

    assert success.exit_code == 0, success.output
    payload = _payload(success.output)
    assert payload["schema"] == "flow-steward.cli.v1"
    assert payload["ok"] is True
    assert payload["command"] == "users.list"
    assert payload["result"]["users"][0]["email"] == "operator@example.com"

    registry.fail = ServiceUnavailableError("database offline")
    failure = CliRunner().invoke(app, ["users", "list", "--json"])
    assert failure.exit_code == 7
    failure_payload = _payload(failure.output)
    assert failure_payload["error"]["code"] == "dependency_unavailable"
    assert failure_payload["error"]["retryable"] is True


def test_users_create_incomplete_interactive_command_uses_shared_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.entrypoints.cli.platform import click_adapter

    captured: dict[str, object] = {}

    def fake_form(**kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(
            status="confirmed",
            values={
                "email": "operator@example.com",
                "password": "StrongPass_123!",
                "roles": ["admin"],
                "is_active": True,
                "must_change_password": False,
                "account_id": "acc.prod",
                "account_ids": ["acc.prod", "acc.sandbox"],
                "actor": "cli",
            },
        )

    monkeypatch.setattr(click_adapter, "_run_textual_form", fake_form, raising=False)
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
    app, registry = _app()

    result = CliRunner().invoke(
        app, ["--tui", "users", "create", "--email", "operator@example.com"]
    )

    assert result.exit_code == 0, result.output
    initial_values = captured["initial_values"]
    assert isinstance(initial_values, dict)
    assert initial_values["email"] == "operator@example.com"
    assert initial_values["roles"] == ["viewer"]
    assert captured["terminal_size"] == (80, 24)
    assert captured["color_enabled"] is True
    assert captured["animation_enabled"] is True
    assert len(registry.calls) == 1
    key, command = registry.calls[0]
    assert key == "users.create"
    assert isinstance(command, UserCreateInput)
    assert command.account_id == "acc.prod"
    assert command.account_ids == ["acc.prod", "acc.sandbox"]
    assert "StrongPass_123!" not in result.output


def test_users_create_can_disable_default_true_boolean_with_no_flag() -> None:
    app, registry = _app()

    result = CliRunner().invoke(
        app,
        [
            "--non-interactive",
            "users",
            "create",
            "--email",
            "operator@example.com",
            "--role",
            "viewer",
            "--password-stdin",
            "--no-is-active",
            "--confirm",
        ],
        input="StrongPass_123!\n",
    )

    assert result.exit_code == 0, result.output
    key, command = registry.calls[0]
    assert key == "users.create"
    assert isinstance(command, UserCreateInput)
    assert command.is_active is False


def test_users_create_json_output_drops_private_authentication_fields() -> None:
    app, _registry = _app()

    result = CliRunner().invoke(
        app,
        [
            "--non-interactive",
            "--json",
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

    assert result.exit_code == 0, result.output
    assert "password_hash" not in _payload(result.output)["result"]["user"]


def test_phase2_user_mutation_leaves_delegate_to_shared_request_models() -> None:
    app, registry = _app()
    runner = CliRunner()

    cases = [
        (
            [
                "users",
                "set-password",
                "--email",
                "operator@example.com",
                "--password-stdin",
                "--must-change-password",
                "--revoke-sessions",
                "--confirm",
            ],
            "users.set-password",
            UserPasswordUpdateInput,
            {
                "email": "operator@example.com",
                "password": "StrongPass_123!",
                "must_change_password": True,
                "revoke_sessions": True,
            },
            "StrongPass_123!\n",
        ),
        (
            ["users", "force-reset", "--email", "operator@example.com", "--confirm"],
            "users.force-reset",
            UserSelectorInput,
            {"email": "operator@example.com"},
            "",
        ),
        (
            ["users", "unlock", "--email", "operator@example.com", "--confirm"],
            "users.unlock",
            UserSelectorInput,
            {"email": "operator@example.com"},
            "",
        ),
        (
            [
                "users",
                "set-active",
                "--email",
                "operator@example.com",
                "--no-is-active",
                "--confirm",
            ],
            "users.set-active",
            UserActiveUpdateInput,
            {"email": "operator@example.com", "is_active": False},
            "",
        ),
        (
            [
                "users",
                "set-roles",
                "--email",
                "operator@example.com",
                "--role",
                "operator",
                "--confirm",
            ],
            "users.set-roles",
            UserRolesUpdateInput,
            {"email": "operator@example.com", "roles": ["operator"]},
            "",
        ),
    ]

    for args, expected_key, expected_type, expected_values, stdin in cases:
        result = runner.invoke(app, args, input=stdin)
        assert result.exit_code == 0, result.output
        key, command = registry.calls[-1]
        assert key == expected_key
        assert isinstance(command, expected_type)
        for field, value in expected_values.items():
            assert command.model_dump()[field] == value
        assert "StrongPass_123!" not in result.output


def test_users_create_cancel_and_invalid_form_never_call_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.entrypoints.cli.platform import click_adapter

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
    app, registry = _app()

    monkeypatch.setattr(
        click_adapter,
        "_run_textual_form",
        lambda **_kwargs: SimpleNamespace(status="cancelled", values={}),
        raising=False,
    )
    cancelled = CliRunner().invoke(
        app, ["--tui", "users", "create", "--email", "operator@example.com"]
    )
    assert cancelled.exit_code == 10
    assert registry.calls == []

    monkeypatch.setattr(
        click_adapter,
        "_run_textual_form",
        lambda **_kwargs: SimpleNamespace(
            status="confirmed",
            values={"email": "bad", "password": "StrongPass_123!", "roles": ["viewer"]},
        ),
        raising=False,
    )
    invalid = CliRunner().invoke(
        app, ["--tui", "users", "create", "--email", "operator@example.com"]
    )
    assert invalid.exit_code == 3
    assert registry.calls == []


def test_users_create_non_interactive_password_sources_succeed_and_stay_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.entrypoints.cli.platform import click_adapter

    secret_root = tmp_path / ".flow-steward" / "secret-input"
    secret_root.mkdir(parents=True)
    secret_file = secret_root / "operator-password"
    secret_file.write_text("StrongPass_123!\n", encoding="utf-8")
    os.chmod(secret_file, 0o600)
    monkeypatch.setattr(click_adapter, "DEFAULT_SECRET_FILE_ROOT", secret_root)

    app, registry = _app()
    by_file = CliRunner().invoke(
        app,
        [
            "users",
            "create",
            "--json",
            "--email",
            "file@example.com",
            "--role",
            "viewer",
            "--password-file",
            str(secret_file),
            "--confirm",
        ],
    )
    assert by_file.exit_code == 0, by_file.output
    assert "StrongPass_123!" not in by_file.output
    assert registry.calls[-1][0] == "users.create"

    by_stdin = CliRunner().invoke(
        app,
        [
            "users",
            "create",
            "--json",
            "--email",
            "stdin@example.com",
            "--role",
            "operator",
            "--password-stdin",
            "--confirm",
        ],
        input="StrongPass_123!\n",
    )
    assert by_stdin.exit_code == 0, by_stdin.output
    assert "StrongPass_123!" not in by_stdin.output


def test_users_create_rejects_unsafe_or_ambiguous_password_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.entrypoints.cli.platform import click_adapter

    secret_root = tmp_path / ".flow-steward" / "secret-input"
    secret_root.mkdir(parents=True)
    safe_file = secret_root / "safe"
    safe_file.write_text("StrongPass_123!\n", encoding="utf-8")
    os.chmod(safe_file, 0o600)
    outside = tmp_path / "outside"
    outside.write_text("StrongPass_123!\n", encoding="utf-8")
    os.chmod(outside, 0o600)
    symlink = secret_root / "link"
    symlink.symlink_to(outside)
    readable = secret_root / "readable"
    readable.write_text("StrongPass_123!\n", encoding="utf-8")
    os.chmod(readable, 0o644)
    monkeypatch.setattr(click_adapter, "DEFAULT_SECRET_FILE_ROOT", secret_root)

    app, registry = _app()
    runner = CliRunner()
    for arguments in (
        ["--password-file", str(safe_file), "--password-stdin"],
        ["--password-file", str(outside)],
        ["--password-file", str(symlink)],
        ["--password-file", str(readable)],
        ["--input-json", "-", "--password-stdin"],
    ):
        result = runner.invoke(
            app,
            ["users", "create", "--json", "--email", "operator@example.com", *arguments],
            input='{"email":"operator@example.com"}' if "-" in arguments else "StrongPass_123!\n",
        )
        assert result.exit_code == 3, result.output
        assert "StrongPass_123!" not in result.output
    assert registry.calls == []

    secret_in_json = runner.invoke(
        app,
        ["users", "create", "--json", "--input-json", "-"],
        input='{"email":"operator@example.com","password":"StrongPass_123!"}',
    )
    assert secret_in_json.exit_code == 3
    assert "StrongPass_123!" not in secret_in_json.output


@pytest.mark.parametrize(
    ("exc", "exit_code", "code"),
    (
        (ServiceConflictError("already exists"), 6, "conflict"),
        (ServiceUnavailableError("accounts offline"), 7, "dependency_unavailable"),
        (ServiceValidationError("invalid account"), 3, "validation_failed"),
    ),
)
def test_users_create_service_failures_map_to_stable_exits(
    exc: BaseException, exit_code: int, code: str
) -> None:
    app, registry = _app()
    registry.fail = exc

    result = CliRunner().invoke(
        app,
        [
            "users",
            "create",
            "--json",
            "--email",
            "operator@example.com",
            "--role",
            "viewer",
            "--password-stdin",
            "--confirm",
        ],
        input="StrongPass_123!\n",
    )

    assert result.exit_code == exit_code
    assert _payload(result.output)["error"]["code"] == code
