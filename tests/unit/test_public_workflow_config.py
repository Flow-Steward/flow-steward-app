from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.application.management.command_services_project_tasks_mixin import (
    _idempotency_result_expires_at,
)

ROOT = Path(__file__).resolve().parents[2]


def test_public_workflow_runtime_settings_are_bounded_and_manifest_owned(monkeypatch) -> None:
    monkeypatch.setenv("FS_PUBLIC_SSE_MAX_CONNECTIONS_PER_PRINCIPAL", "0")
    monkeypatch.setenv("FS_PUBLIC_SSE_MAX_CONNECTIONS_PER_ACCOUNT", "999999")
    monkeypatch.setenv("FS_RETENTION_JOBS_DAYS", "invalid")

    from core.infrastructure.config.public_workflow import public_workflow_runtime_settings

    settings = public_workflow_runtime_settings()

    assert settings.sse_connections_per_principal == 1
    assert settings.sse_connections_per_account == 10000
    assert settings.idempotency_result_retention_days == 30

    manifest = json.loads(
        (ROOT / "core/infrastructure/config/flow_steward_env_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    names = {entry["name"] for entry in manifest["variables"]}
    assert {
        "FS_PUBLIC_SSE_MAX_CONNECTIONS_PER_PRINCIPAL",
        "FS_PUBLIC_SSE_MAX_CONNECTIONS_PER_ACCOUNT",
        "FS_RETENTION_JOBS_DAYS",
    } <= names


def test_application_retention_policy_uses_injected_days() -> None:
    expires_at = _idempotency_result_expires_at(retention_days=45)

    assert datetime.now(UTC) + timedelta(days=44, seconds=59) <= expires_at
    assert expires_at <= datetime.now(UTC) + timedelta(days=45, seconds=1)


def test_runtime_consumers_do_not_read_public_workflow_environment_directly() -> None:
    events_source = (ROOT / "core/entrypoints/api/events_stream.py").read_text(encoding="utf-8")
    tasks_source = (
        ROOT / "core/application/management/command_services_project_tasks_mixin.py"
    ).read_text(encoding="utf-8")

    assert "os.getenv" not in events_source
    assert "os.getenv" not in tasks_source
