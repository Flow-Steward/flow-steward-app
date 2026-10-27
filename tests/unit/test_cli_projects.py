"""Projects/workflows public CLI is schema-driven only."""

from __future__ import annotations

import importlib.util
from types import SimpleNamespace
from typing import Any

from click.testing import CliRunner

from core.application.management.command_specs.projects import PROJECT_COMMAND_SPECS
from core.application.management.command_specs.workflows import WORKFLOW_COMMAND_SPECS


class _Registry:
    async def invoke(self, key: str, value: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError(f"Unexpected service invocation: {key} {value!r}")

    async def choices(self, _key: str) -> tuple[Any, ...]:
        return ()


def _app() -> Any:
    from core.entrypoints.cli.platform.click_adapter import build_click_app

    return build_click_app(_Registry(), SimpleNamespace())


def test_legacy_project_and_workflow_parser_modules_are_removed() -> None:
    assert importlib.util.find_spec("core.entrypoints.cli.projects") is None
    assert importlib.util.find_spec("core.entrypoints.cli.workflows") is None


def test_projects_and_workflows_specs_are_canonical() -> None:
    assert PROJECT_COMMAND_SPECS[0].path == ("projects", "list")
    assert PROJECT_COMMAND_SPECS[-1].path == ("projects", "dispatch-notification")
    assert tuple(spec.path for spec in WORKFLOW_COMMAND_SPECS) == (
        ("workflows", "convert-group"),
        ("workflows", "list"),
        ("workflows", "entrypoints"),
        ("workflows", "validate"),
        ("workflows", "contract"),
        ("workflows", "suggestions"),
        ("workflows", "hooks-list"),
        ("workflows", "hook-get"),
        ("workflows", "hook-preview"),
        ("workflows", "hook-bind"),
        ("workflows", "hook-unbind"),
        ("workflows", "hook-create-and-bind"),
        ("workflows", "where-used"),
        ("workflows", "authoring-schema"),
        ("workflows", "draft-validate"),
        ("workflows", "draft-save"),
        ("workflows", "definition"),
        ("workflows", "apply-changes"),
        ("workflows", "export"),
        ("workflows", "import"),
        ("workflows", "run"),
        ("workflows", "stats"),
    )


def test_removed_project_parser_options_are_rejected() -> None:
    result = CliRunner().invoke(
        _app(),
        ["projects", "list", "--project-name", "demo", "--show-config"],
    )

    assert result.exit_code == 2, result.output
