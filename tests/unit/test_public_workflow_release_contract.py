from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_public_docs_define_one_exact_official_release_journey() -> None:
    readme = (ROOT / "docs/operations/checkout-reference.md").read_text(encoding="utf-8")
    guide = (ROOT / "docs/GUIDE.md").read_text(encoding="utf-8")

    for document in (readme, guide):
        assert "Official release upgrade" in document or "Official production upgrade" in document
        release = document.split("Official release upgrade", maxsplit=1)[-1]
        if release == document:
            release = document.split("Official production upgrade", maxsplit=1)[1]
        release = release.split("Backup and recovery boundary", maxsplit=1)[0]
        normalized = " ".join(release.split())
        assert "stable github releases" in normalized.casefold()
        assert "tagged exactly `vX.Y.Z`" in normalized
        assert "./flow-steward release check" in release
        assert "./flow-steward release upgrade --confirm" in release
        assert "digest-pinned image" in normalized
        assert "build product images locally" in normalized
        assert "git pull" not in release


def test_job_lifecycle_sql_repository_is_owned_by_application_jobs() -> None:
    repository_path = ROOT / "core/application/jobs/job_lifecycle_repository.py"
    infrastructure_store = ROOT / "core/infrastructure/db/job_lifecycle_store.py"
    submission_source = (ROOT / "core/application/jobs/job_submission_repository.py").read_text(
        encoding="utf-8"
    )

    assert repository_path.exists()
    assert "class JobLifecycleRepository" in repository_path.read_text(encoding="utf-8")
    assert "INSERT INTO jobs" in repository_path.read_text(encoding="utf-8")
    assert not infrastructure_store.exists()
    assert "JobLifecycleRepository" in submission_source
    assert "core.infrastructure.db.job_lifecycle_store" not in submission_source
    assert "platform_logger_common import _truncate_text" not in submission_source


def test_dispatch_outbox_sql_repository_is_owned_by_application_jobs() -> None:
    application_source = (ROOT / "core/application/jobs/dispatch_outbox_repository.py").read_text(
        encoding="utf-8"
    )
    infrastructure_store = ROOT / "core/infrastructure/db/job_dispatch_outbox_store.py"
    submission_source = (ROOT / "core/application/jobs/job_submission_repository.py").read_text(
        encoding="utf-8"
    )

    assert "INSERT INTO job_dispatch_outbox" in application_source
    assert not infrastructure_store.exists()
    assert "transaction_conn" in submission_source
    assert "DispatchOutboxRepository(self.transaction_conn)" in submission_source
    assert 'getattr(self.bl, "create_job_dispatch_outbox"' not in submission_source


def test_dead_transport_fingerprint_is_not_part_of_public_composition_surface() -> None:
    from core.entrypoints.api import workflow_entrypoints

    assert not hasattr(workflow_entrypoints, "_entrypoint_request_fingerprint")


def test_workflow_entrypoint_routes_receive_audit_capable_project_store() -> None:
    source = (ROOT / "core/entrypoints/api/route_registration.py").read_text(encoding="utf-8")

    assert (
        "_register_workflow_entrypoint_routes(\n"
        "        current_app,\n"
        '        get_logger_only=lambda: _composed_service(current_app, "project_store"),'
    ) in source


def test_registry_composition_has_no_wildcard_or_filewide_mypy_suppression() -> None:
    source = (ROOT / "core/entrypoints/api/workflow_entrypoints_registry.py").read_text(
        encoding="utf-8"
    )

    assert "import *" not in source
    assert "mypy: ignore-errors" not in source


def test_webhook_transport_delegates_submission_and_audit_to_application_use_case() -> None:
    route_source = (ROOT / "core/entrypoints/api/workflow_entrypoints_routes_execute.py").read_text(
        encoding="utf-8"
    )
    service_path = ROOT / "core/application/workflows/workflow_entrypoint_request_service.py"
    execution_path = ROOT / "core/application/workflows/workflow_entrypoint_execution_service.py"
    audit_path = ROOT / "core/application/workflows/workflow_entrypoint_audit_service.py"

    assert service_path.exists()
    service_source = service_path.read_text(encoding="utf-8")
    assert "class WorkflowEntrypointRequestService" in service_source
    assert "class WorkflowEntrypointRequestCommand" in service_source
    assert "class WorkflowEntrypointRequestResult" in service_source
    assert "def execute" in service_source
    assert "def submission" in service_source
    assert "WorkflowEntrypointRequestService" in route_source
    assert ".execute" in route_source
    assert "class _WorkflowEntrypointHttpController" in route_source
    assert "class _WorkflowEntrypointHttpAudit" not in route_source
    assert "WorkflowEntrypointAuditService" in route_source
    assert "class WorkflowEntrypointAuditPolicy" in audit_path.read_text(encoding="utf-8")
    assert "record_rejection_outcome" in route_source
    assert "record_preparation_rejection" in route_source
    controller_source = route_source.split("class _WorkflowEntrypointHttpController", maxsplit=1)[1]
    assert "if not transport.already_recorded" not in controller_source
    assert "failure_suffix" not in controller_source
    assert "status_code" not in execution_path.read_text(encoding="utf-8")
    assert "_execute_workflow_entrypoint_request" not in route_source
    for transport_concern in (
        "status_code",
        "response_mode",
        "audit_meta",
        "run_sync",
        "load_transport",
    ):
        assert transport_concern not in service_source
    route_tree = ast.parse(route_source)
    handlers = [
        node
        for node in ast.walk(route_tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "trigger_workflow_entrypoint"
    ]
    assert len(handlers) == 1
    assert handlers[0].end_lineno is not None
    assert handlers[0].end_lineno - handlers[0].lineno + 1 <= 40


def test_entrypoint_auth_transport_returns_typed_outcome_without_writing_audit() -> None:
    auth_transport = (
        ROOT / "core/entrypoints/api/workflow_entrypoints_execution_auth.py"
    ).read_text(encoding="utf-8")
    audit_service = (
        ROOT / "core/application/workflows/workflow_entrypoint_audit_service.py"
    ).read_text(encoding="utf-8")

    assert "WorkflowEntrypointAuthOutcome" in auth_transport
    assert "record_workflow_entrypoint_execution" not in auth_transport
    assert "_log_entrypoint_execution" not in auth_transport
    assert "already_recorded" not in auth_transport
    assert "record_rejection_outcome" in audit_service
    assert "status_code" not in (
        ROOT / "core/application/workflows/workflow_entrypoint_auth.py"
    ).read_text(encoding="utf-8")


def test_graph_resume_runtime_does_not_own_durable_resume_state() -> None:
    runtime_source = (ROOT / "core/infrastructure/runtime/graph_resume_runtime.py").read_text(
        encoding="utf-8"
    )

    assert "ApprovalResumeIntentService" not in runtime_source
    assert "approval_resume_intents" not in runtime_source
    assert "resume_lease_id" not in runtime_source
    assert "SELECT meta FROM jobs" not in runtime_source
    assert "UPDATE jobs SET meta" not in runtime_source


def test_public_workflow_modules_are_enforced_by_xenon() -> None:
    source = (ROOT / "core/entrypoints/cli/scripts/lint.sh").read_text(encoding="utf-8")

    for path in (
        "core/application/jobs/task_enqueue_runtime.py",
        "core/application/workflows/workflow_entrypoint_execution_service.py",
        "core/entrypoints/api/jobs_control.py",
        "core/entrypoints/api/workflow_entrypoints_routes_execute.py",
    ):
        assert f'"{path}"' in source
