from __future__ import annotations

from pathlib import Path

import pytest

from core.application.tooling.testing import cutover_preflight
from core.application.tooling.testing.models import (
    TestingCutoverPreflightRequest as CutoverPreflightRequest,
)
from core.application.tooling.testing.service import TestingToolingService as LiveTestingService
from core.infrastructure.config.connection_strings import resolve_postgres_dsn, resolve_rabbitmq_url
from core.infrastructure.tooling.process_runner import OSProcessRunner


def test_cutover_preflight_passes_explicit_database_and_queue_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    monkeypatch.setattr(cutover_preflight, "check_health", lambda *_args: (True, "healthy"))
    monkeypatch.setattr(
        cutover_preflight,
        "check_graph_compiles",
        lambda database_dsn: (
            captured.setdefault("graph_database_dsn", database_dsn) and (True, "compiled")
        ),
    )
    monkeypatch.setattr(
        cutover_preflight,
        "check_message_table",
        lambda database_dsn: (
            captured.setdefault("database_dsn", database_dsn) and (True, "table present")
        ),
    )
    monkeypatch.setattr(
        cutover_preflight,
        "check_queue_depth",
        lambda project_id, rabbitmq_url: (
            captured.update(project_id=project_id, rabbitmq_url=rabbitmq_url)
            or (True, "queue depth=0")
        ),
    )

    result = cutover_preflight.run_preflight(
        project_id="project-1",
        base_url="https://flow.test",
        verify_ssl=True,
        database_dsn="postgresql://db-secret",
        rabbitmq_url="amqp://queue-secret",
    )

    assert result == 0
    assert captured == {
        "database_dsn": "postgresql://db-secret",
        "graph_database_dsn": "postgresql://db-secret",
        "project_id": "project-1",
        "rabbitmq_url": "amqp://queue-secret",
    }


def test_typed_cutover_preflight_crosses_scrubbed_child_boundary_without_secret_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FS_DB_DSN", "postgresql://db-secret")
    monkeypatch.setenv("RABBITMQ_URL", "amqp://queue-secret")

    def fake_preflight(**kwargs: object) -> int:
        assert kwargs["database_dsn"] == "postgresql://db-secret"
        assert kwargs["rabbitmq_url"] == "amqp://queue-secret"
        print("configuration received")
        return 0

    monkeypatch.setattr(cutover_preflight, "run_preflight", fake_preflight)
    service = LiveTestingService(
        runner=OSProcessRunner(working_directory=Path(__file__).parents[2]),
        database_dsn_provider=resolve_postgres_dsn,
        rabbitmq_url_provider=resolve_rabbitmq_url,
    )

    result = service.cutover_preflight(
        CutoverPreflightRequest(project_id="project-1", timeout_seconds=10)
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "configuration received"
    assert "db-secret" not in result.stdout + result.stderr
    assert "queue-secret" not in result.stdout + result.stderr


def test_cutover_preflight_fails_required_configuration_before_checks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key in ("FS_OBSERVABILITY_DSN", "FS_DB_DSN", "DATABASE_URL", "RABBITMQ_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        cutover_preflight,
        "check_health",
        lambda *_args: pytest.fail("health check must not run without required configuration"),
    )

    result = cutover_preflight.run_preflight(
        project_id="project-1",
        base_url="https://flow.test",
        verify_ssl=True,
        database_dsn="",
        rabbitmq_url="",
    )

    assert result == 2
    output = capsys.readouterr().out
    assert "FS_OBSERVABILITY_DSN/FS_DB_DSN" in output
    assert "RABBITMQ_URL" in output
