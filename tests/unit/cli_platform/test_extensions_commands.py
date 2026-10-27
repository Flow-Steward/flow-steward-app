"""CLI regression tests for migrated extension scaffolding commands."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from core.application.extensions.scaffold import ExtensionScaffoldService
from core.application.management.query_models import (
    ExtensionDetailView,
    ExtensionHealthView,
    ExtensionListView,
    ExtensionPageProjectView,
)
from core.application.management.scope import ScopeContext
from core.entrypoints.cli.platform.modes import TerminalFacts
from core.entrypoints.cli.platform.service_registry import ServiceRegistry


@dataclass
class _Runtime:
    extension_scaffold_service: ExtensionScaffoldService
    extension_query_service: object
    extension_command_service: object
    extension_binding_service: object
    user_query_service: object = object()
    user_command_service: object = object()
    project_query_service: object = object()
    project_command_service: object = object()


class _ExtensionQuery:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def list_extensions(self, *, filters: dict[str, Any], scope: ScopeContext):
        self.calls.append(("list_extensions", (filters, scope)))
        state = filters["state"][0] if filters["state"] else "all"
        return ExtensionListView(
            extensions=[{"extension_id": "acme.crm-sync", "state": state}],
            total=1,
        )

    def get_extension(self, extension_id: str, *, scope: ScopeContext):
        self.calls.append(("get_extension", (extension_id, scope)))
        return ExtensionDetailView(
            extension_id=extension_id,
            display_name="CRM Sync",
            compiled_manifest={
                "extension_id": extension_id,
                "ui_manifest": {"version": "1", "pages": []},
            },
            validation_summary={"status": "ok", "error_count": 0},
            projects=[ExtensionPageProjectView(id="project-1", name="Sales")],
            selected_project_id="project-1",
        )

    def get_health(self, extension_id: str, *, scope: ScopeContext):
        self.calls.append(("get_health", (extension_id, scope)))
        return ExtensionHealthView(
            extension_id=extension_id,
            health={
                "status": "healthy",
                "reason": "Canonical runtime probe passed.",
                "checked_at": "2026-08-01T12:00:00Z",
            },
        )


class _ExtensionBinding:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def install_extension_archive_path(self, *, archive_path: Path, scope: ScopeContext):
        self.calls.append(("install_extension_archive_path", (archive_path, scope)))
        return {
            "extension": {"extension_id": "acme.crm-sync"},
            "installed_path": str(archive_path),
        }

    def preview_extension_uninstall(self, extension_id: str, *, scope: ScopeContext):
        self.calls.append(("preview_extension_uninstall", (extension_id, scope)))
        return {
            "extension_id": extension_id,
            "install_id": "install-acme",
            "release_id": "release-acme",
            "revision": "rev-1",
            "remove": [{"kind": "runtime_package", "id": extension_id, "count": 1}],
            "affected": [],
            "retained": [],
        }

    def uninstall_extension(self, extension_id: str, *, plan_revision: str, scope: ScopeContext):
        self.calls.append(("uninstall_extension", (extension_id, plan_revision, scope)))
        return {"extension_id": extension_id, "state": "uninstalled", "removed_data": []}

    def set_enabled_state(
        self,
        extension_id: str,
        *,
        version: str | None,
        enabled: bool,
        config,
        scope: ScopeContext,
        approved_browser_authority_impact_hash: str = "",
    ):
        self.calls.append(
            (
                "set_enabled_state",
                (
                    extension_id,
                    version,
                    enabled,
                    config,
                    scope,
                    approved_browser_authority_impact_hash,
                ),
            )
        )
        return {
            "extension": {
                "extension_id": extension_id,
                "state": "active" if enabled else "disabled",
            }
        }


class _ExtensionCommand:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def enable_extension(
        self,
        extension_id: str,
        *,
        scope: ScopeContext,
        approved_browser_authority_impact_hash: str = "",
    ):
        self.calls.append(
            (
                "enable_extension",
                (extension_id, scope, approved_browser_authority_impact_hash),
            )
        )
        return {"extension_id": extension_id, "state": "active"}


def _app() -> object:
    from core.entrypoints.cli.platform.click_adapter import build_click_app

    return build_click_app(
        ServiceRegistry(
            _Runtime(
                extension_scaffold_service=ExtensionScaffoldService(),
                extension_query_service=_ExtensionQuery(),
                extension_command_service=_ExtensionCommand(),
                extension_binding_service=_ExtensionBinding(),
            )
        ),
        SimpleNamespace(),
    )


def _app_and_runtime() -> tuple[object, _Runtime]:
    from core.entrypoints.cli.platform.click_adapter import build_click_app

    runtime = _Runtime(
        extension_scaffold_service=ExtensionScaffoldService(),
        extension_query_service=_ExtensionQuery(),
        extension_command_service=_ExtensionCommand(),
        extension_binding_service=_ExtensionBinding(),
    )
    return build_click_app(ServiceRegistry(runtime), SimpleNamespace()), runtime


def _assert_cli_admin_scope(value: object) -> ScopeContext:
    assert isinstance(value, ScopeContext)
    assert value.actor_id == "flow-steward-cli"
    assert value.principal_type == "admin"
    assert value.project_id is None
    assert value.allowed_project_ids == frozenset()
    return value


def test_extensions_group_help_exposes_exact_phase2_leaves() -> None:
    result = CliRunner().invoke(_app(), ["extensions", "--help"])

    assert result.exit_code == 0, result.output
    for command in (
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
    ):
        assert command in result.output
    assert " tooling " not in result.output


def test_extensions_create_help_marks_required_format_examples_and_no_force() -> None:
    result = CliRunner().invoke(
        _app(),
        ["extensions", "create", "--help"],
        color=True,
        terminal_width=80,
    )

    assert result.exit_code == 0
    plain_help = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "Extension ID" in plain_help
    assert "Required" in plain_help
    assert "Canonical lowercase dot-separated package ID" in plain_help
    assert (
        "Examples:\n"
        "  flow-steward extensions create acme.crm-sync\n"
        "  flow-steward extensions create com.example.notifications\n"
    ) in plain_help
    assert "--force" not in plain_help


def test_extension_lifecycle_leaves_delegate_to_shared_services(tmp_path: Path) -> None:
    app, runtime = _app_and_runtime()
    archive = tmp_path / "acme.zip"
    archive.write_bytes(b"zip-bytes")
    runner = CliRunner()

    assert runner.invoke(app, ["extensions", "list", "--state", "disabled"]).exit_code == 0
    method, args = runtime.extension_query_service.calls[-1]
    assert method == "list_extensions"
    assert args[0] == {"state": ["disabled"]}
    _assert_cli_admin_scope(args[1])

    assert runner.invoke(app, ["extensions", "show", "acme.crm-sync"]).exit_code == 0
    method, args = runtime.extension_query_service.calls[-1]
    assert method == "get_extension"
    assert args[0] == "acme.crm-sync"
    _assert_cli_admin_scope(args[1])

    with patch.object(
        Path, "read_bytes", side_effect=AssertionError("CLI must not read archive bytes")
    ):
        assert (
            runner.invoke(
                app, ["extensions", "install", "--archive", str(archive), "--confirm"]
            ).exit_code
            == 0
        )
    method, args = runtime.extension_binding_service.calls[-1]
    assert method == "install_extension_archive_path"
    assert args[0] == archive
    _assert_cli_admin_scope(args[1])

    assert (
        runner.invoke(
            app,
            [
                "extensions",
                "uninstall",
                "acme.crm-sync",
                "--confirm",
                "acme.crm-sync",
            ],
        ).exit_code
        == 0
    )
    method, args = runtime.extension_binding_service.calls[-1]
    assert method == "uninstall_extension"
    assert args[:2] == ("acme.crm-sync", "rev-1")
    _assert_cli_admin_scope(args[2])

    assert (
        runner.invoke(
            app,
            [
                "extensions",
                "enable",
                "acme.crm-sync",
                "--approve-browser-authority",
                "a" * 64,
                "--confirm",
            ],
        ).exit_code
        == 0
    )
    method, args = runtime.extension_binding_service.calls[-1]
    assert method == "set_enabled_state"
    assert args[:4] == ("acme.crm-sync", None, True, {})
    _assert_cli_admin_scope(args[4])
    assert args[5] == "a" * 64
    assert (
        runner.invoke(app, ["extensions", "disable", "acme.crm-sync", "--confirm"]).exit_code == 0
    )
    method, args = runtime.extension_binding_service.calls[-1]
    assert method == "set_enabled_state"
    assert args[:4] == ("acme.crm-sync", None, False, {})
    _assert_cli_admin_scope(args[4])


def test_extension_install_resolves_relative_archive_from_host_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, runtime = _app_and_runtime()
    monkeypatch.setenv("FS_DOCKER_HOST_PROJECT_ROOT", str(tmp_path))

    result = CliRunner().invoke(
        app,
        ["extensions", "install", "--archive", "dist/acme.zip", "--confirm"],
    )

    assert result.exit_code == 0, result.output
    method, args = runtime.extension_binding_service.calls[-1]
    assert method == "install_extension_archive_path"
    assert args[0] == tmp_path / "dist/acme.zip"


@pytest.mark.parametrize("tool_exit_code", [0, 1])
def test_extensions_test_json_keeps_dependency_output_inside_one_document(
    tool_exit_code: int,
) -> None:
    from core.application.tooling.extensions.test_extension import ExtensionTestResult

    with patch(
        "core.entrypoints.cli.platform.service_bindings.extensions.test_extension",
        return_value=ExtensionTestResult(
            status="passed" if tool_exit_code == 0 else "failed",
            extension_id="acme.crm-sync",
            exit_code=tool_exit_code,
            output="Looking in links: /extension/dev-wheels\npytest passed\n",
        ),
    ):
        result = CliRunner().invoke(
            _app(),
            [
                "extensions",
                "test",
                "acme.crm-sync",
                "--skip-validate",
                "--skip-smoke",
                "--skip-pytest",
                "--json",
            ],
        )

    payload = json.loads(result.output)
    assert payload["schema"] == "flow-steward.cli.v1"
    assert "Looking in links:" in json.dumps(payload)
    assert result.exit_code == (0 if tool_exit_code == 0 else 3)


@pytest.mark.parametrize(
    ("mode", "expected_fragments"),
    [
        (
            "--json",
            (
                '"compiled_manifest"',
                '"validation_summary"',
                '"projects"',
                '"selected_project_id": "project-1"',
            ),
        ),
        (
            "--plain",
            (
                '"compiled_manifest"',
                '"validation_summary"',
                '"projects"',
                '"selected_project_id": "project-1"',
            ),
        ),
    ],
)
def test_extensions_show_preserves_full_detail_contract(
    mode: str, expected_fragments: tuple[str, ...]
) -> None:
    result = CliRunner().invoke(_app(), ["extensions", "show", "acme.crm-sync", mode])

    assert result.exit_code == 0, result.output
    for fragment in expected_fragments:
        assert fragment in result.output


def test_extensions_show_rich_output_preserves_full_detail_contract() -> None:
    result = CliRunner().invoke(
        _app(),
        ["extensions", "show", "acme.crm-sync"],
        color=True,
    )

    assert result.exit_code == 0, result.output
    for fragment in (
        "compiled_manifest",
        "validation_summary",
        "projects",
        "selected_project_id",
        "project-1",
    ):
        assert fragment in result.output


def test_extensions_health_reports_the_canonical_runtime_probe() -> None:
    app, runtime = _app_and_runtime()

    result = CliRunner().invoke(app, ["extensions", "health", "acme.crm-sync", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["result"] == {
        "extension_id": "acme.crm-sync",
        "health": {
            "status": "healthy",
            "reason": "Canonical runtime probe passed.",
            "checked_at": "2026-08-01T12:00:00Z",
        },
    }
    method, args = runtime.extension_query_service.calls[-1]
    assert method == "get_health"
    assert args[0] == "acme.crm-sync"
    _assert_cli_admin_scope(args[1])


def test_extensions_uninstall_builds_and_reviews_plan_before_mutation() -> None:
    app, runtime = _app_and_runtime()

    result = CliRunner().invoke(
        app,
        [
            "extensions",
            "uninstall",
            "acme.crm-sync",
            "--plain",
            "--confirm",
            "acme.crm-sync",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "rev-1" in result.output
    assert "runtime_package" in result.output
    methods = [method for method, _args in runtime.extension_binding_service.calls]
    assert methods[-2:] == ["preview_extension_uninstall", "uninstall_extension"]
    _method, args = runtime.extension_binding_service.calls[-1]
    assert args[:2] == ("acme.crm-sync", "rev-1")


def test_extensions_uninstall_has_no_public_plan_revision_option() -> None:
    result = CliRunner().invoke(_app(), ["extensions", "uninstall", "--help"])

    assert result.exit_code == 0, result.output
    assert "--plan-revision" not in result.output
    assert "uninstall-plan" not in result.output


@pytest.mark.parametrize("extension_id", ["acme.crm-sync", "com.example.notifications"])
def test_extensions_create_examples_validate(extension_id: str, tmp_path: Path) -> None:
    from core.application.extensions.scaffold import ExtensionScaffoldCreateInput

    command = ExtensionScaffoldCreateInput(
        extension_id=extension_id,
        output_root=tmp_path / "extensions",
    )

    assert command.extension_id == extension_id


@pytest.mark.parametrize(
    "extension_id",
    ["1", "550e8400-e29b-41d4-a716-446655440000", "ACME.crm", "acme_bad", "acme"],
)
def test_extensions_create_rejects_noncanonical_ids_before_filesystem_change(
    extension_id: str, tmp_path: Path
) -> None:
    output_root = tmp_path / "extensions"

    result = CliRunner().invoke(
        _app(),
        ["extensions", "create", extension_id, "--output-root", str(output_root), "--json"],
    )

    assert result.exit_code == 3
    assert not output_root.exists()


def test_extensions_create_incomplete_interactive_form_preserves_click_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.entrypoints.cli.platform import click_adapter

    captured: dict[str, Any] = {}
    output_root = tmp_path / "extensions"

    def fake_form(**kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(
            status="confirmed",
            values={
                "extension_id": "acme.crm-sync",
                "output_root": output_root,
                "display_name": "CRM Sync",
                "description": "Synchronize CRM records",
                "vendor_name": "Acme",
                "category": "sales",
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

    result = CliRunner().invoke(
        _app(),
        [
            "--tui",
            "extensions",
            "create",
            "--output-root",
            str(output_root),
            "--display-name",
            "CRM Sync",
            "--vendor-name",
            "Acme",
        ],
    )

    assert result.exit_code == 0, result.output
    initial = captured["initial_values"]
    assert isinstance(initial, dict)
    assert initial["output_root"] == str(output_root)
    assert initial["display_name"] == "CRM Sync"
    assert initial["vendor_name"] == "Acme"
    assert (output_root / "acme_crm_sync").is_dir()


def test_extensions_create_cancel_creates_no_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.entrypoints.cli.platform import click_adapter

    output_root = tmp_path / "extensions"
    monkeypatch.setattr(
        click_adapter,
        "_run_textual_form",
        lambda **_kwargs: SimpleNamespace(status="cancelled", values={}),
        raising=False,
    )
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
        _app(), ["--tui", "extensions", "create", "--output-root", str(output_root)]
    )

    assert result.exit_code == 10
    assert not output_root.exists()


def test_extensions_create_review_rejection_shows_scaffold_plan_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.entrypoints.cli.platform import click_adapter

    output_root = tmp_path / "extensions"
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
    result = CliRunner().invoke(
        _app(),
        [
            "extensions",
            "create",
            "acme.review-plan",
            "--output-root",
            str(output_root),
            "--display-name",
            "Review Plan",
            "--vendor-name",
            "Acme",
            "--category",
            "ops",
        ],
        input="n\n",
    )

    assert result.exit_code == 10
    assert "Canonical extension ID: acme.review-plan" in result.output
    assert f"Target directory: {output_root / 'acme_review_plan'}" in result.output
    assert "Side effect: creation of a new extension scaffold." in result.output
    assert "existing targets are not overwritten" in result.output
    assert "Vendor name: Acme" in result.output
    assert "Category: ops" in result.output
    assert not output_root.exists()


def test_extensions_create_noninteractive_json_success_and_plain_summary(
    tmp_path: Path,
) -> None:
    import json

    output_root = tmp_path / "extensions"
    json_result = CliRunner().invoke(
        _app(),
        [
            "extensions",
            "create",
            "acme.crm-sync",
            "--output-root",
            str(output_root),
            "--display-name",
            "CRM Sync",
            "--vendor-name",
            "Acme",
            "--category",
            "sales",
            "--confirm",
            "--json",
        ],
    )

    assert json_result.exit_code == 0, json_result.output
    payload = json.loads(json_result.output)
    assert payload["result"]["extension_id"] == "acme.crm-sync"
    assert payload["result"]["path"].endswith("acme_crm_sync")
    assert payload["result"]["files_created"] > 0
    assert "Flow Steward extension scaffold TODO" not in json_result.output

    plain_root = tmp_path / "plain-extensions"
    plain_result = CliRunner().invoke(
        _app(),
        [
            "extensions",
            "create",
            "com.example.notifications",
            "--output-root",
            str(plain_root),
            "--confirm",
        ],
    )
    assert plain_result.exit_code == 0, plain_result.output
    assert "com.example.notifications" in plain_result.output
    assert str(plain_root / "com_example_notifications") in plain_result.output
    assert "Flow Steward extension scaffold TODO" not in plain_result.output


def test_extensions_create_existing_target_returns_conflict_without_altering_files(
    tmp_path: Path,
) -> None:
    import json

    output_root = tmp_path / "extensions"
    runner = CliRunner()
    first = runner.invoke(
        _app(),
        [
            "extensions",
            "create",
            "acme.existing",
            "--output-root",
            str(output_root),
            "--confirm",
            "--json",
        ],
    )
    assert first.exit_code == 0, first.output
    marker = output_root / "acme_existing" / "keep.txt"
    marker.write_text("preserve me", encoding="utf-8")

    second = runner.invoke(
        _app(),
        [
            "extensions",
            "create",
            "acme.existing",
            "--output-root",
            str(output_root),
            "--confirm",
            "--json",
        ],
    )

    assert second.exit_code == 6
    assert json.loads(second.output)["error"]["code"] == "conflict"
    assert marker.read_text(encoding="utf-8") == "preserve me"
    assert "preserve me" not in second.output


def test_legacy_tooling_extensions_create_no_longer_resolves() -> None:
    repo_root = Path(__file__).parents[3]
    proc = subprocess.run(
        [sys.executable, "-m", "core.entrypoints.cli", "tooling", "extensions", "create"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 2
    assert "No such command 'tooling'" in proc.stderr
