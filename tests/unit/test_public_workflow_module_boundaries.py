from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _line_count(relative_path: str) -> int:
    return len((ROOT / relative_path).read_text(encoding="utf-8").splitlines())


def test_public_workflow_backend_modules_are_split_by_use_case() -> None:
    assert _line_count("core/application/jobs/task_enqueue_runtime.py") < 1000
    assert _line_count("core/application/workflows/workflow_entrypoint_execution_service.py") < 700
    for relative_path in (
        "core/application/jobs/task_dispatch_publish.py",
        "core/application/jobs/task_dispatch_recovery.py",
        "core/application/jobs/task_scheduling.py",
        "core/application/workflows/workflow_entrypoint_idempotency_service.py",
        "core/application/workflows/workflow_entrypoint_submission.py",
        "core/application/workflows/workflow_entrypoint_artifacts.py",
    ):
        assert (ROOT / relative_path).is_file(), relative_path


def test_job_detail_page_delegates_coordination_to_page_local_hooks() -> None:
    assert _line_count("assets/frontend/src/pages/jobs/detail/index.tsx") < 500
    for relative_path in (
        "assets/frontend/src/pages/jobs/detail/use-job-detail-queries.ts",
        "assets/frontend/src/pages/jobs/detail/use-job-detail-realtime.ts",
        "assets/frontend/src/pages/jobs/detail/use-job-detail-actions.ts",
    ):
        assert (ROOT / relative_path).is_file(), relative_path
