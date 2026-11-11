"""`database upgrade` names what blocks it, and where its log is (2026-11-11 demo upgrade).

With the 0.9.12 app, worker, control-plane and mcp still connected, the upgrade printed
"An unexpected internal error occurred. Code: internal_error" and `Log: /app/logs/cli/...`.
The cause -- "preflight is blocked ... runtime_sessions" -- was only in that log, and the
log path was a container path that does not exist on the host.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.application.lifecycle.errors import LifecycleError
from core.application.management.command_specs.contracts import ExitCode
from core.application.management.command_specs.database import DATABASE_COMMAND_SPECS
from core.application.tooling.db import upgrade_db
from core.application.tooling.db.models import DatabaseUpgradeRequest
from core.application.tooling.db.service import DatabaseToolingService
from core.entrypoints.cli.platform.execution import execute_command
from core.entrypoints.cli.platform.operation_log import OperationLog, reported_log_path
from core.entrypoints.cli.platform.renderers import PlainRenderer

_BLOCKING = [
    {
        "code": "runtime_sessions",
        "message": "Flow Steward processes are still connected; stop every app, worker and "
        "scheduler process first.",
        "details": {"sessions": [{"pid": 41, "application_name": "flow-steward:app:0.9.14"}]},
    },
    {
        "code": "unlabeled_sessions",
        "message": "Sessions of the application's database user carry no Flow Steward name; "
        "they may be processes of an earlier release.",
        "details": {"sessions": [{"pid": 42, "application_name": ""}]},
    },
]
_UPGRADE_SPEC = next(
    spec for spec in DATABASE_COMMAND_SPECS if spec.command_id == "database.upgrade"
)


@pytest.fixture
def blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(**_kwargs: object) -> dict[str, object]:
        raise upgrade_db.DatabaseUpgradeBlockedError(_BLOCKING)

    monkeypatch.setattr(upgrade_db, "upgrade_database", refuse)


def test_the_upgrade_refuses_a_blocked_preflight_with_its_own_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        upgrade_db.upgrade_preflight,
        "preflight",
        lambda _conn, **_kwargs: {"blocking": _BLOCKING, "warnings": []},
    )

    with pytest.raises(upgrade_db.DatabaseUpgradeBlockedError) as raised:
        upgrade_db._refuse_blocked_preflight(object(), allow_unlabeled_sessions=False)

    assert [issue["code"] for issue in raised.value.blocking] == [
        "runtime_sessions",
        "unlabeled_sessions",
    ]
    assert "preflight is blocked (2 issue(s))" in str(raised.value)


def test_the_service_turns_a_blocked_preflight_into_a_precondition_failure(
    blocked: None,
) -> None:
    with pytest.raises(LifecycleError) as raised:
        DatabaseToolingService().upgrade(DatabaseUpgradeRequest(maintenance_confirmed=True))

    error = raised.value
    assert error.code == "database_upgrade_blocked"
    assert error.exit_code is ExitCode.PRECONDITION
    assert "runtime_sessions, unlabeled_sessions" in error.cause
    findings = error.details["findings"]
    assert [row["check_id"] for row in findings] == ["runtime_sessions", "unlabeled_sessions"]
    assert findings[0]["detail"] == "Connected: pid 41 (flow-steward:app:0.9.14)."
    assert findings[1]["detail"] == "Connected: pid 42."
    assert "maintenance enable" in error.remediation
    assert "--allow-unlabeled-sessions" in error.remediation
    assert "stops fs-app, fs-worker, control-plane and fs-mcp" in findings[0]["remediation"]


def test_an_issue_without_its_own_advice_points_at_the_full_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(**_kwargs: object) -> dict[str, object]:
        raise upgrade_db.DatabaseUpgradeBlockedError(
            [
                {
                    "code": "migration_drift",
                    "message": "Applied migrations differ from this release's files.",
                    "details": {"migrations": ["20261103090000_secret_catalog.sql"]},
                }
            ]
        )

    monkeypatch.setattr(upgrade_db, "upgrade_database", refuse)

    with pytest.raises(LifecycleError) as raised:
        DatabaseToolingService().upgrade(DatabaseUpgradeRequest(maintenance_confirmed=True))

    finding = raised.value.details["findings"][0]
    assert finding["detail"] == "migrations: 20261103090000_secret_catalog.sql"
    assert "database preflight" in raised.value.remediation


class _DatabaseRegistry:
    def invoke(self, key: str, value: object, **_sinks: object) -> object:
        assert key == "database.upgrade"
        return DatabaseToolingService().upgrade(value)


def test_the_cli_shows_every_blocking_issue_instead_of_internal_error(
    blocked: None, capsys: pytest.CaptureFixture[str]
) -> None:
    result = execute_command(
        spec=_UPGRADE_SPEC,
        command_input=DatabaseUpgradeRequest(maintenance_confirmed=True),
        services=_DatabaseRegistry(),
    )

    assert result.exit_code is ExitCode.PRECONDITION
    assert result.error is not None
    assert result.error.code == "database_upgrade_blocked"

    PlainRenderer().render_result(_UPGRADE_SPEC, result)

    out = capsys.readouterr().out
    assert "internal_error" not in out
    assert "Code: database_upgrade_blocked" in out
    assert "runtime_sessions" in out
    assert "unlabeled_sessions" in out
    assert "Connected: pid 41 (flow-steward:app:0.9.14)." in out


def test_a_log_written_in_the_container_is_reported_at_its_host_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container_root = tmp_path / "app/logs/cli"
    monkeypatch.delenv("FS_OPERATION_LOG_ROOT", raising=False)
    monkeypatch.setenv("FS_CLI_OPERATION_LOG_ROOT", str(container_root))
    monkeypatch.setenv("FS_CLI_OPERATION_LOG_HOST_ROOT", "/srv/flow-steward/logs/cli")

    log = OperationLog("database.upgrade")
    log.close()

    assert log.path.parent == container_root.resolve()
    assert reported_log_path(log.path) == f"/srv/flow-steward/logs/cli/{log.path.name}"


def test_without_a_host_root_or_outside_the_log_root_the_path_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FS_OPERATION_LOG_ROOT", raising=False)
    monkeypatch.setenv("FS_CLI_OPERATION_LOG_ROOT", str(tmp_path / "logs"))
    elsewhere = tmp_path / "elsewhere/x.log"

    monkeypatch.delenv("FS_CLI_OPERATION_LOG_HOST_ROOT", raising=False)
    assert reported_log_path(tmp_path / "logs/x.log") == str(tmp_path / "logs/x.log")

    monkeypatch.setenv("FS_CLI_OPERATION_LOG_HOST_ROOT", "/srv/flow-steward/logs/cli")
    assert reported_log_path(elsewhere) == str(elsewhere)
