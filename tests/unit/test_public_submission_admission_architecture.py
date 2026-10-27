"""Architecture checks for the single public job-admission boundary."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_PATH = "/run/flow-steward-runtime"


def test_canonical_boundary_ignores_spoofed_submission_classification() -> None:
    from core.application.jobs.submission_admission import (
        PublicSubmissionAdmissionBoundary,
        PublicSubmissionPausedError,
    )

    boundary = PublicSubmissionAdmissionBoundary(lifecycle_pause_active=lambda: True)

    for task_source, channel in (
        ("manual", "schedule"),
        ("mcp_workflow", "resume"),
        ("schedule", "internal"),
        ("job_restart", "parent_call"),
    ):
        try:
            with boundary.admit(
                task_source=task_source,
                meta={"invocation": {"channel": channel}},
            ):
                raise AssertionError("a paused new submission must not enter")
        except PublicSubmissionPausedError:
            pass
        else:
            raise AssertionError("caller metadata bypassed lifecycle admission")


def test_enqueue_runtime_uses_the_application_public_admission_boundary() -> None:
    source = (ROOT / "core/application/jobs/task_enqueue_runtime.py").read_text()

    assert "admit_public_submission(" in source
    assert "with admit_public_submission(" in source


def test_scheduled_runtime_uses_the_same_application_admission_boundary() -> None:
    source = (ROOT / "core/application/jobs/task_scheduling.py").read_text()

    assert "with admit_public_submission(" in source


def test_transports_only_use_body_staging_admission_not_marker_boundary() -> None:
    for relative_path in (
        "core/entrypoints/api/jobs_control.py",
        "core/entrypoints/api/workflow_entrypoints_routes_execute.py",
    ):
        source = (ROOT / relative_path).read_text()
        assert "lifecycle_submission_pause_active" not in source


def test_app_reads_marker_while_lifecycle_cli_owns_runtime_state() -> None:
    read_only = (
        "${FS_INSTALLER_STATE_DIR:-../../.flow-steward}/runtime:/run/flow-steward-runtime:ro"
    )
    read_write = (
        "${FS_INSTALLER_STATE_DIR:-../../.flow-steward}/runtime:/run/flow-steward-runtime:rw"
    )
    for relative_path in (
        "infra/docker/docker-compose.yaml",
        "infra/docker/docker-compose.prod.yaml",
    ):
        source = (ROOT / relative_path).read_text()
        for service, expected in (
            ("fs-app:", read_only),
            ("fs-cli:", read_write),
        ):
            service_match = re.search(rf"^  {re.escape(service)}\n", source, flags=re.MULTILINE)
            assert service_match is not None, f"{relative_path}: {service} missing"
            service_start = service_match.start()
            next_service = re.search(r"\n  [a-z][a-z0-9-]*:\n", source[service_start + 1 :])
            end = service_start + 1 + next_service.start() if next_service else None
            service_block = source[service_start:end]
            assert expected in service_block, f"{relative_path}: {service}"


def test_runtime_uses_the_canonical_marker_path_only() -> None:
    source = (ROOT / "core/application/jobs/submission_admission.py").read_text()

    assert '_CANONICAL_RUNTIME_DIR = Path("/run/flow-steward-runtime")' in source
    assert 'Path.cwd() / ".flow-steward/runtime"' not in source


def test_submission_pauses_for_lifecycle_or_maintenance_marker(tmp_path: Path, monkeypatch) -> None:
    from core.application.jobs import submission_admission

    monkeypatch.setattr(submission_admission, "_CANONICAL_RUNTIME_DIR", tmp_path)

    assert submission_admission.public_submission_pause_active() is False
    (tmp_path / "job-admission-paused").touch()
    assert submission_admission.public_submission_pause_active() is True
    (tmp_path / "job-admission-paused").unlink()
    (tmp_path / "maintenance-state.json").touch()
    assert submission_admission.public_submission_pause_active() is True
