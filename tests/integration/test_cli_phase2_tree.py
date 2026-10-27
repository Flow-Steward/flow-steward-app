"""Frozen Phase 2 tree plus approved Phase 3 readiness leaves."""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from click.testing import CliRunner

from core.application.management.command_specs.registry import (
    ALLOWED_RESULT_RENDERER_REFS,
    ALLOWED_SERVICE_REFS,
    PUBLIC_COMMAND_SPECS,
)

EXPECTED: dict[str, tuple[str, ...]] = {
    "clone": ("create", "restore"),
    "users": (
        "list",
        "create",
        "set-password",
        "force-reset",
        "unlock",
        "set-active",
        "set-roles",
        "issue-bootstrap-token",
    ),
    "extensions": (
        "list",
        "show",
        "health",
        "install",
        "uninstall",
        "enable",
        "disable",
        "create",
        "validate",
        "test",
        "bundle",
        "lock-deps",
    ),
    "projects": (
        "list",
        "show",
        "notification-routes",
        "notification-deliveries",
        "dispatch-notification",
    ),
    "workflows": (
        "convert-group",
        "list",
        "entrypoints",
        "validate",
        "contract",
        "suggestions",
        "hooks-list",
        "hook-get",
        "hook-preview",
        "hook-bind",
        "hook-unbind",
        "hook-create-and-bind",
        "where-used",
        "authoring-schema",
        "draft-validate",
        "draft-save",
        "definition",
        "apply-changes",
        "export",
        "import",
        "run",
        "stats",
    ),
    "jobs": (
        "list",
        "show",
        "status",
        "logs",
        "messages",
        "errors",
        "approve",
        "reject",
        "ack",
        "unack",
        "cancel",
        "restart",
        "health-snapshot",
    ),
    "reports": ("generate", "get", "diagnostics"),
    "database": (
        "status",
        "upgrade",
        "bootstrap-seed",
        "generate-migration",
        "query-diagnostics",
        "tune-env",
        "restore-parity-capture",
        "restore-parity-verify",
    ),
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
PHASE3_ROOT_LEAVES = ("status", "doctor", "proxy-fragment")
APPROVED_PHASE4_DOMAINS = (
    "stack",
    "environment",
    "maintenance",
    "release",
    "backup",
    "config",
)
EXCLUDED_DOMAINS = (
    "tool",
    "tooling",
    "db",
    "test",
    "tests",
    "ext",
    "i18n",
    "l10n",
    "operations",
)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class _Registry:
    async def invoke(self, key: str, value: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError(f"Unexpected service invocation: {key} {value!r}")

    async def choices(self, _key: str) -> tuple[Any, ...]:
        return ()


def _app() -> Any:
    from core.entrypoints.cli.platform.click_adapter import build_click_app

    return build_click_app(_Registry(), SimpleNamespace())


def _strip_ansi(value: str) -> str:
    return ANSI_RE.sub("", value)


def _normalize_help_text(value: str) -> str:
    text = re.sub(r"-\n\s*", "-", _strip_ansi(value))
    return re.sub(r"\s+", " ", text).strip()


def _command_row(name: str) -> str:
    return rf"^\s{{2}}{re.escape(name)}(?:\s{{2,}}|$)"


def _phase2_only_tree(
    tree: Mapping[str, tuple[str, ...]],
) -> dict[str, tuple[str, ...]]:
    return {
        domain: leaves for domain, leaves in tree.items() if domain not in APPROVED_PHASE4_DOMAINS
    }


def test_phase2_inventory_filter_excludes_approved_phase4_domains() -> None:
    combined = {
        "users": ("list",),
        "stack": ("status",),
        "environment": ("show",),
        "maintenance": ("status",),
    }

    assert _phase2_only_tree(combined) == {"users": ("list",)}


def test_phase2_public_tree_is_exact_and_ordered() -> None:
    app = _app()

    assert tuple(command for command in app.commands if command not in APPROVED_PHASE4_DOMAINS) == (
        *PHASE3_ROOT_LEAVES,
        *EXPECTED,
    )
    for domain, leaves in EXPECTED.items():
        assert tuple(app.commands[domain].commands) == leaves

    by_path: dict[str, list[str]] = {}
    for spec in PUBLIC_COMMAND_SPECS:
        if len(spec.path) == 2:
            by_path.setdefault(spec.path[0], []).append(spec.path[1])

    assert (
        _phase2_only_tree({domain: tuple(leaves) for domain, leaves in by_path.items()}) == EXPECTED
    )
    assert sum(
        1 for spec in PUBLIC_COMMAND_SPECS if spec.path[0] not in APPROVED_PHASE4_DOMAINS
    ) == len(PHASE3_ROOT_LEAVES) + sum(len(leaves) for leaves in EXPECTED.values())


def test_root_and_group_help_list_only_the_phase2_public_tree() -> None:
    app = _app()
    runner = CliRunner()

    root = runner.invoke(app, ["--help"])
    assert root.exit_code == 0, root.output
    root_text = _strip_ansi(root.output)
    root_commands = tuple(app.list_commands(None))
    assert {command for command in root_commands if command not in APPROVED_PHASE4_DOMAINS} == {
        *EXPECTED,
        *PHASE3_ROOT_LEAVES,
    }
    for domain in (*PHASE3_ROOT_LEAVES, *EXPECTED):
        assert re.search(_command_row(domain), root_text, re.MULTILINE)
    for excluded in EXCLUDED_DOMAINS:
        assert not re.search(_command_row(excluded), root_text, re.MULTILINE)

    for domain, leaves in EXPECTED.items():
        result = runner.invoke(app, [domain, "--help"])
        assert result.exit_code == 0, (domain, result.output)
        text = _strip_ansi(result.output)
        assert set(app.commands[domain].list_commands(None)) == set(leaves)
        for leaf in leaves:
            assert re.search(_command_row(leaf), text, re.MULTILINE)
        for excluded in EXCLUDED_DOMAINS:
            if excluded in leaves:
                continue
            assert not re.search(_command_row(excluded), text, re.MULTILINE)


def test_each_leaf_has_real_help_examples_and_closed_bindings() -> None:
    app = _app()
    runner = CliRunner()

    for spec in PUBLIC_COMMAND_SPECS:
        result = runner.invoke(app, [*spec.path, "--help"])
        assert result.exit_code == 0, (spec.command_id, result.output)
        text = _strip_ansi(result.output)
        normalized = _normalize_help_text(result.output)
        assert spec.summary
        assert spec.description
        assert spec.examples
        assert spec.service_ref in ALLOWED_SERVICE_REFS
        assert spec.result_renderer_ref in ALLOWED_RESULT_RENDERER_REFS
        assert "TODO" not in text
        assert "placeholder" not in text.lower()
        assert "Examples:" in text
        for example in spec.examples:
            assert _normalize_help_text(example) in normalized


def test_option_relevance_matrix_is_not_blindly_duplicated() -> None:
    app = _app()
    runner = CliRunner()

    assert runner.invoke(app, ["--version"]).exit_code == 0

    root_help = _strip_ansi(runner.invoke(app, ["--help"]).output)
    users_help = _strip_ansi(runner.invoke(app, ["users", "--help"]).output)
    users_list_help = _strip_ansi(runner.invoke(app, ["users", "list", "--help"]).output)
    users_create_help = _strip_ansi(runner.invoke(app, ["users", "create", "--help"]).output)
    quality_lint_help = _strip_ansi(runner.invoke(app, ["quality", "lint", "--help"]).output)
    sonar_help = _strip_ansi(
        runner.invoke(app, ["quality", "sonar-export-issues", "--help"]).output
    )

    assert "--version" in root_help
    assert "--version" not in users_help
    assert "--version" not in users_list_help

    assert "--input-json" not in root_help
    assert "--input-json" not in users_help
    assert "--input-json" not in users_list_help
    assert "--input-json" in users_create_help
    assert "--input-json" in quality_lint_help

    assert "--password-file" not in users_list_help
    assert "--password-stdin" not in users_list_help
    assert "--password-file" in users_create_help
    assert "--password-stdin" in users_create_help
    assert "--sonar-token-file" in sonar_help
    assert "--sonar-token-stdin" in sonar_help

    for read_help in (users_list_help,):
        assert "--confirm" not in read_help
        assert "--review" not in read_help
        assert "--force" not in read_help

    for help_text in (root_help, users_help, users_list_help):
        assert "--json" in help_text
        assert "--plain" in help_text
        assert "--no-color" in help_text
        assert "--non-interactive" in help_text
