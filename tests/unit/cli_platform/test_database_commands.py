"""Contract tests for schema-driven database tooling commands."""

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

    def status(self, command: object) -> dict[str, Any]:
        payload = command.model_dump(mode="json")
        self.calls.append(("status", payload))
        return {
            "operation": "database.status",
            "status": "ok",
            "changed": False,
            "message": "database is current",
            "details": {"needs_upgrade": False},
            "artifacts": [],
        }

    def upgrade(self, command: object) -> dict[str, Any]:
        payload = command.model_dump(mode="json")
        self.calls.append(("upgrade", payload))
        return {
            "operation": "database.upgrade",
            "status": "completed",
            "changed": True,
            "message": "upgrade applied",
            "details": payload,
            "artifacts": [],
        }

    def bootstrap_seed(self, command: object) -> dict[str, Any]:
        payload = command.model_dump(mode="json")
        self.calls.append(("bootstrap_seed", payload))
        return {
            "operation": "database.bootstrap-seed",
            "status": "completed",
            "changed": True,
            "message": "seed applied",
            "details": payload,
            "artifacts": [],
        }

    def generate_migration(self, command: object) -> dict[str, Any]:
        payload = command.model_dump(mode="json")
        self.calls.append(("generate_migration", payload))
        return {
            "operation": "database.generate-migration",
            "status": "created",
            "changed": True,
            "message": "migration created",
            "details": payload,
            "artifacts": [
                str(Path(payload["root"]) / "db/migrations/20260717000000_add_audit.sql")
            ],
        }

    def query_diagnostics(self, command: object) -> dict[str, Any]:
        payload = command.model_dump(mode="json")
        self.calls.append(("query_diagnostics", payload))
        return {
            "operation": "database.query-diagnostics",
            "status": "completed",
            "changed": False,
            "message": "query diagnostics collected",
            "details": {"settings": [], **payload},
            "artifacts": [],
        }

    def tune_env(self, command: object) -> dict[str, Any]:
        payload = command.model_dump(mode="json")
        self.calls.append(("tune_env", payload))
        return {
            "operation": "database.tune-env",
            "status": "completed",
            "changed": False,
            "message": "postgres env profile generated",
            "details": {"profile": {"POSTGRES_JIT": "off"}, **payload},
            "artifacts": [],
        }

    def restore_parity_capture(self, command: object) -> dict[str, Any]:
        payload = command.model_dump(mode="json")
        self.calls.append(("restore_parity_capture", payload))
        return {
            "operation": "database.restore-parity-capture",
            "status": "captured",
            "changed": True,
            "message": "parity metadata captured",
            "details": payload,
            "artifacts": [payload["output"]],
        }

    def restore_parity_verify(self, command: object) -> dict[str, Any]:
        payload = command.model_dump(mode="json")
        self.calls.append(("restore_parity_verify", payload))
        return {
            "operation": "database.restore-parity-verify",
            "status": "passed",
            "changed": bool(payload["output"]),
            "message": "parity metadata verified",
            "details": payload,
            "artifacts": [payload["output"]] if payload["output"] else [],
        }


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


def _app(runtime: _Runtime | None = None) -> tuple[Any, _Runtime]:
    from core.entrypoints.cli.platform.click_adapter import build_click_app

    runtime = runtime or _Runtime()
    return build_click_app(ServiceRegistry(runtime), SimpleNamespace()), runtime


def test_database_read_commands_delegate_to_typed_application_tooling_service() -> None:
    app, runtime = _app()
    runner = CliRunner()

    cases = [
        (
            ["database", "status", "--plain"],
            ("status", {"dsn": ""}),
        ),
        (
            [
                "database",
                "query-diagnostics",
                "--limit",
                "7",
                "--sort-by",
                "total",
                "--min-rows",
                "42",
                "--max-query-len",
                "80",
                "--plain",
            ],
            (
                "query_diagnostics",
                {
                    "dsn": "",
                    "limit": 7,
                    "sort_by": "total",
                    "min_rows": 42,
                    "max_query_len": 80,
                },
            ),
        ),
    ]

    for args, expected in cases:
        result = runner.invoke(app, args)
        assert result.exit_code == 0, (args, result.output)
        assert runtime.database_tooling_service.calls[-1] == expected


def test_database_mutation_commands_delegate_only_after_review_confirmation(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from core.entrypoints.cli.platform import click_adapter
    from core.entrypoints.cli.platform.modes import TerminalFacts

    app, runtime = _app()
    capture = tmp_path / "restore-parity.json"
    verify = tmp_path / "verify.json"
    runner = CliRunner()
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

    cases = [
        (
            ["database", "upgrade", "--author", "operator", "--maintenance-confirmed", "--plain"],
            ("upgrade", {"dsn": "", "author": "operator", "maintenance_confirmed": True}),
        ),
        (
            ["database", "bootstrap-seed", "--author", "operator", "--plain"],
            ("bootstrap_seed", {"dsn": "", "author": "operator"}),
        ),
        (
            ["database", "generate-migration", "add audit", "--root", str(tmp_path), "--plain"],
            ("generate_migration", {"name": "add audit", "root": str(tmp_path)}),
        ),
        (
            [
                "database",
                "tune-env",
                "--memory-gb",
                "8",
                "--max-connections",
                "50",
                "--storage",
                "hdd",
                "--plain",
            ],
            ("tune_env", {"memory_gb": 8.0, "max_connections": 50, "storage": "hdd"}),
        ),
        (
            [
                "database",
                "restore-parity-capture",
                "--output",
                str(capture),
                "--label",
                "base",
                "--plain",
            ],
            (
                "restore_parity_capture",
                {"dsn": "", "label": "base", "output": str(capture)},
            ),
        ),
        (
            [
                "database",
                "restore-parity-verify",
                "--baseline",
                str(capture),
                "--output",
                str(verify),
                "--label",
                "restored",
                "--plain",
            ],
            (
                "restore_parity_verify",
                {
                    "dsn": "",
                    "label": "restored",
                    "baseline": str(capture),
                    "output": str(verify),
                },
            ),
        ),
    ]

    for args, expected in cases:
        cancelled = runner.invoke(app, args, input="n\n")
        assert cancelled.exit_code == 10, (args, cancelled.output)
        assert runtime.database_tooling_service.calls == []

        confirmed = runner.invoke(app, args, input="y\n")
        assert confirmed.exit_code == 0, (args, confirmed.output)
        assert runtime.database_tooling_service.calls[-1] == expected
        runtime.database_tooling_service.calls.clear()


def test_database_dsn_is_secret_source_not_plain_argument(tmp_path: Path, monkeypatch: Any) -> None:
    from core.entrypoints.cli.platform import click_adapter

    app, runtime = _app()
    secret_root = tmp_path / ".flow-steward" / "secret-input"
    secret_root.mkdir(parents=True)
    dsn_file = secret_root / "db-dsn"
    dsn_file.write_text("postgres://user:password@example/db\n", encoding="utf-8")
    dsn_file.chmod(0o600)
    monkeypatch.setattr(click_adapter, "DEFAULT_SECRET_FILE_ROOT", secret_root)

    result = CliRunner().invoke(
        app,
        ["database", "status", "--dsn-file", str(dsn_file), "--json"],
    )

    assert result.exit_code == 0, result.output
    assert runtime.database_tooling_service.calls[-1] == (
        "status",
        {"dsn": "postgres://user:password@example/db"},
    )
    assert "postgres://user:password@example/db" not in result.output
    assert "--dsn " not in CliRunner().invoke(app, ["database", "status", "--help"]).output
    assert "--dsn-file" in CliRunner().invoke(app, ["database", "status", "--help"]).output


def test_database_binding_has_no_legacy_parser_or_transport_boundary() -> None:
    path = Path(__file__).parents[3] / "core/entrypoints/cli/platform/service_bindings/database.py"
    source = path.read_text(encoding="utf-8")

    forbidden = (
        "argparse",
        ".main(",
        "parse_args",
        "sys.argv",
        "runpy",
        "REMAINDER",
        "core.entrypoints.cli.tooling",
        "core.entrypoints.api",
        "requests",
        "httpx",
        "urllib",
        "/api/",
    )
    for token in forbidden:
        assert token not in source
