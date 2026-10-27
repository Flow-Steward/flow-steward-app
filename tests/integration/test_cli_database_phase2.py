"""Phase 2 database CLI migration regression tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from click.testing import CliRunner

from core.entrypoints.cli.platform.service_registry import ServiceRegistry


@dataclass
class _DatabaseTooling:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def _record(self, name: str, command: object, *, changed: bool = False) -> dict[str, Any]:
        payload = command.model_dump(mode="json")
        self.calls.append((name, payload))
        return {
            "operation": f"database.{name.replace('_', '-')}",
            "status": "completed",
            "changed": changed,
            "message": name,
            "details": payload,
            "artifacts": [],
        }

    def status(self, command: object) -> dict[str, Any]:
        return self._record("status", command)

    def upgrade(self, command: object) -> dict[str, Any]:
        return self._record("upgrade", command, changed=True)

    def bootstrap_seed(self, command: object) -> dict[str, Any]:
        return self._record("bootstrap_seed", command, changed=True)

    def generate_migration(self, command: object) -> dict[str, Any]:
        return self._record("generate_migration", command, changed=True)

    def query_diagnostics(self, command: object) -> dict[str, Any]:
        return self._record("query_diagnostics", command)

    def tune_env(self, command: object) -> dict[str, Any]:
        return self._record("tune_env", command)

    def restore_parity_capture(self, command: object) -> dict[str, Any]:
        return self._record("restore_parity_capture", command, changed=True)

    def restore_parity_verify(self, command: object) -> dict[str, Any]:
        return self._record("restore_parity_verify", command, changed=True)


@dataclass
class _Runtime:
    database_tooling_service: _DatabaseTooling = field(default_factory=_DatabaseTooling)
    project_query_service: object = object()
    project_command_service: object = object()
    reporting_service: object = object()
    approval_query_service: object = object()
    approval_command_service: object = object()
    job_query_service: object = object()
    job_command_service: object = object()
    ops_health_snapshot_service: object = object()
    workflow_operator_service: object = object()
    user_query_service: object = object()
    user_command_service: object = object()
    extension_query_service: object = object()
    extension_binding_service: object = object()
    extension_scaffold_service: object = object()


def _cli_app(runtime: _Runtime | None = None) -> tuple[Any, _Runtime]:
    from core.entrypoints.cli.platform.click_adapter import build_click_app

    runtime = runtime or _Runtime()
    return build_click_app(ServiceRegistry(runtime), SimpleNamespace()), runtime


def test_database_phase2_tree_contains_only_approved_leaves() -> None:
    from core.application.management.command_specs.registry import PUBLIC_COMMAND_SPECS

    tree: dict[str, list[str]] = {}
    for spec in PUBLIC_COMMAND_SPECS:
        if len(spec.path) == 2:
            tree.setdefault(spec.path[0], []).append(spec.path[1])

    assert tree["database"] == [
        "status",
        "upgrade",
        "bootstrap-seed",
        "generate-migration",
        "query-diagnostics",
        "tune-env",
        "restore-parity-capture",
        "restore-parity-verify",
    ]


def test_legacy_database_aliases_are_not_public_routes() -> None:
    app, runtime = _cli_app()

    for args in (
        ["db", "status"],
        ["tooling", "db", "upgrade"],
        ["tooling", "db", "generate-migration", "--", "--name", "x"],
        ["database", "restore-parity-check"],
        ["database", "init-db"],
    ):
        result = CliRunner().invoke(app, args)
        assert result.exit_code == 2, (args, result.output)
    assert runtime.database_tooling_service.calls == []


def test_database_help_and_validation_do_not_touch_database_or_artifact_dirs(
    tmp_path: Path,
) -> None:
    app, runtime = _cli_app()
    before = _artifact_snapshots()
    runner = CliRunner()

    for args in (
        ["database", "--help"],
        ["database", "status", "--help"],
        ["database", "generate-migration", "--help"],
        ["database", "restore-parity-capture", "--help"],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, (args, result.output)

    invalid = runner.invoke(
        app,
        ["database", "generate-migration", "--root", str(tmp_path), "--plain"],
        input="y\n",
    )
    assert invalid.exit_code == 3, invalid.output
    assert runtime.database_tooling_service.calls == []
    assert _artifact_snapshots() == before


def test_database_generate_migration_uses_declared_tmp_root_and_never_default_artifacts(
    tmp_path: Path,
) -> None:
    app, runtime = _cli_app()
    before = _artifact_snapshots()

    result = CliRunner().invoke(
        app,
        [
            "database",
            "generate-migration",
            "add audit",
            "--root",
            str(tmp_path),
            "--confirm",
            "--plain",
        ],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert runtime.database_tooling_service.calls == [
        ("generate_migration", {"name": "add audit", "root": str(tmp_path)})
    ]
    assert _artifact_snapshots() == before


def _artifact_snapshots() -> dict[str, set[str]]:
    root = Path(__file__).parents[2]
    return {
        str(path.relative_to(root)): {
            str(item.relative_to(path)) for item in path.rglob("*") if item.is_file()
        }
        for path in (root / "db/schema", root / "db/seeds", root / "db/migrations")
    }
