"""Phase 2 testing/quality/localization CLI migration regression tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from click.testing import CliRunner

from core.entrypoints.cli.platform.service_registry import ServiceRegistry

EXPECTED = {
    "testing": (
        "smoke-test",
        "nightfire-regression",
        "fuzz-api",
        "fuzz-mcp",
        "fuzz-ui",
        "cutover-preflight",
        "cutover-smoke",
    ),
    "quality": (
        "verify",
        "lint",
        "check-ci-excludes",
        "check-naming",
        "release-gate",
        "sonar-export-issues",
        "sonar-ensure-rules",
        "sonar-generate-backlog",
    ),
    "localization": (
        "bootstrap-catalog",
        "rebuild-pot",
        "init-locale",
        "update",
        "compile",
        "validate-keys",
    ),
}


@dataclass
class _Runtime:
    testing_tooling_service: Any = field(default_factory=lambda: _Service("testing"))
    quality_tooling_service: Any = field(default_factory=lambda: _Service("quality"))
    localization_tooling_service: Any = field(default_factory=lambda: _Service("localization"))
    user_query_service: object = object()
    user_command_service: object = object()
    project_query_service: object = object()
    project_command_service: object = object()
    job_query_service: object = object()
    job_command_service: object = object()
    approval_command_service: object = object()
    reporting_service: object = object()
    ops_health_snapshot_service: object = object()
    workflow_operator_service: object = object()
    extension_query_service: object = object()
    extension_binding_service: object = object()
    extension_scaffold_service: object = object()
    database_tooling_service: object = object()


class _Service:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        def record(command: object) -> dict[str, Any]:
            payload = command.model_dump(mode="json")
            self.calls.append((name, payload))
            return {
                "operation": f"{self.prefix}.{name.replace('_', '-')}",
                "status": "completed",
                "exit_code": 0,
                "command": [],
                "stdout": "",
                "stderr": "",
                "artifacts": [],
                "details": payload,
            }

        return record


def _cli_app(runtime: _Runtime | None = None) -> tuple[Any, _Runtime]:
    from core.entrypoints.cli.platform.click_adapter import build_click_app

    runtime = runtime or _Runtime()
    return build_click_app(ServiceRegistry(runtime), SimpleNamespace()), runtime


def test_tooling_domains_phase2_tree_contains_only_approved_leaves() -> None:
    from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS

    tree: dict[str, list[str]] = {}
    for spec in PUBLIC_COMMAND_SPECS:
        if len(spec.path) == 2:
            tree.setdefault(spec.path[0], []).append(spec.path[1])

    for domain, leaves in EXPECTED.items():
        assert tuple(tree[domain]) == leaves


def test_legacy_tooling_aliases_are_not_public_routes() -> None:
    app, runtime = _cli_app()

    for args in (
        ["tooling", "testing", "smoke-test"],
        ["tooling", "quality", "lint"],
        ["tooling", "i18n", "validate-keys"],
        ["test", "smoke-test"],
        ["tests", "fuzz-api"],
        ["i18n", "validate-keys"],
        ["l10n", "compile"],
        ["operations", "quality", "lint"],
    ):
        result = CliRunner().invoke(app, args)
        assert result.exit_code == 2, (args, result.output)

    assert runtime.testing_tooling_service.calls == []
    assert runtime.quality_tooling_service.calls == []
    assert runtime.localization_tooling_service.calls == []


def test_tooling_domain_help_is_side_effect_free() -> None:
    app, runtime = _cli_app()
    runner = CliRunner()

    for domain, leaves in EXPECTED.items():
        group = runner.invoke(app, [domain, "--help"])
        assert group.exit_code == 0, group.output
        for leaf in leaves:
            assert leaf in group.output
            help_result = runner.invoke(app, [domain, leaf, "--help"])
            assert help_result.exit_code == 0, (domain, leaf, help_result.output)
            assert "Usage:" in help_result.output

    assert runtime.testing_tooling_service.calls == []
    assert runtime.quality_tooling_service.calls == []
    assert runtime.localization_tooling_service.calls == []
