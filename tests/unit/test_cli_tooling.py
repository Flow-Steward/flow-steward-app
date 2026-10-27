"""Migrated tooling paths are no longer public argparse aliases."""

from __future__ import annotations

import importlib.util
from types import SimpleNamespace
from typing import Any

from click.testing import CliRunner


class _Registry:
    async def invoke(self, key: str, value: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError(f"Unexpected service invocation: {key} {value!r}")

    async def choices(self, _key: str) -> tuple[Any, ...]:
        return ()


def _app() -> Any:
    from core.entrypoints.cli.platform.click_adapter import build_click_app

    return build_click_app(_Registry(), SimpleNamespace())


def test_legacy_tooling_parser_module_is_removed() -> None:
    assert importlib.util.find_spec("core.entrypoints.cli.tooling") is None


def test_legacy_tooling_domains_and_aliases_are_rejected() -> None:
    runner = CliRunner()
    app = _app()

    for arguments in (
        ["tooling", "quality", "check-naming"],
        ["tooling", "db", "upgrade"],
        ["tooling", "testing", "smoke-test"],
        ["tooling", "i18n", "compile"],
        ["tool", "quality", "lint"],
        ["db", "status"],
        ["ext", "validate"],
        ["i18n", "compile"],
        ["l10n", "compile"],
        ["operations", "cleanup-eval-workflows"],
    ):
        result = runner.invoke(app, arguments)
        assert result.exit_code == 2, (arguments, result.output)
