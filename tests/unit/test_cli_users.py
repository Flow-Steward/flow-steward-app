"""Users public CLI is served only by schema-driven CommandSpecs."""

from __future__ import annotations

import importlib.util
from types import SimpleNamespace
from typing import Any

from click.testing import CliRunner

from core.application.management.command_specs.users import USER_COMMAND_SPECS


class _Registry:
    async def invoke(self, key: str, value: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError(f"Unexpected service invocation: {key} {value!r}")

    async def choices(self, _key: str) -> tuple[Any, ...]:
        return ()


def _app() -> Any:
    from core.entrypoints.cli.platform.click_adapter import build_click_app

    return build_click_app(_Registry(), SimpleNamespace())


def test_legacy_users_parser_module_is_removed() -> None:
    assert importlib.util.find_spec("core.entrypoints.cli.users") is None


def test_users_specs_expose_only_phase2_leaves() -> None:
    assert [spec.path for spec in USER_COMMAND_SPECS] == [
        ("users", "list"),
        ("users", "create"),
        ("users", "set-password"),
        ("users", "force-reset"),
        ("users", "unlock"),
        ("users", "set-active"),
        ("users", "set-roles"),
        ("users", "issue-bootstrap-token"),
    ]


def test_old_user_spellings_and_raw_password_option_are_rejected() -> None:
    app = _app()
    runner = CliRunner()

    for arguments in (
        ["users", "force-password-reset", "--email", "operator@example.com"],
        ["users", "create", "--password", "StrongPass_123!"],
    ):
        result = runner.invoke(app, arguments)
        assert result.exit_code == 2, (arguments, result.output)
