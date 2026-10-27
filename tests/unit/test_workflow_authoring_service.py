"""Facade errors and post-commit cache consistency against real draft behavior."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from core.application.management.errors import ServiceConflictError, ServiceValidationError
from core.application.workflows.authoring.service import WorkflowAuthoringService
from tests.unit.test_workflow_authoring_catalog import SCOPE
from tests.unit.test_workflow_authoring_drafts import DraftDB, bundle


def test_facade_invalid_yaml_maps_safe_structured_422():
    service = WorkflowAuthoringService(DraftDB(), creation_authorizer=lambda scope: None)
    with pytest.raises(ServiceValidationError) as raised:
        service.workflow_draft_save(
            scope=SCOPE,
            project_id="project",
            bundle_yaml="workflows: [\nprivate_marker: sensitive_source_excerpt",
        )
    assert raised.value.status_code == 422
    assert raised.value.payload["code"] == "bundle_yaml_invalid"
    assert raised.value.payload["issues"][0]["path"] == "bundle_yaml"
    assert raised.value.payload["issues"][0]["repair_guidance"]
    assert "sensitive_source_excerpt" not in str(raised.value.payload)


def test_facade_validation_failure_retains_field_repair_issues():
    service = WorkflowAuthoringService(DraftDB(), creation_authorizer=lambda scope: None)
    text = bundle(settings="invalid_scalar")
    with pytest.raises(ServiceValidationError) as raised:
        service.workflow_draft_save(scope=SCOPE, project_id="project", bundle_yaml=text)
    assert raised.value.status_code == 422
    issue = raised.value.payload["issues"][0]
    assert issue["code"]
    assert issue["path"] == "workflows[0].settings"
    assert issue["blocking"] is True
    assert issue["repair_guidance"]


def test_cache_refresh_observes_committed_draft_and_is_not_used_for_validate_or_errors():
    class CommitAwareDB(DraftDB):
        in_transaction = False

        @contextmanager
        def workflow_import_transaction(self):
            self.in_transaction = True
            try:
                with super().workflow_import_transaction():
                    yield
            finally:
                self.in_transaction = False

    db = CommitAwareDB()
    refreshes = []

    def refresh():
        assert db.in_transaction is False
        refreshes.append((list(db.workflows), list(db.bindings), list(db.history)))

    service = WorkflowAuthoringService(
        db, creation_authorizer=lambda scope: None, after_save=refresh
    )
    service.workflow_draft_validate(scope=SCOPE, project_id="project", bundle_yaml=bundle())
    assert refreshes == []
    saved = service.workflow_draft_save(scope=SCOPE, project_id="project", bundle_yaml=bundle())
    assert saved["enabled"] is False
    assert refreshes == [
        (
            ["new_workflow"],
            [("project", "new_workflow")],
            [{"workflow_id": "new_workflow", "action": "create", "author": "operator"}],
        )
    ]
    retry = bundle()
    with pytest.raises(ServiceConflictError):
        service.workflow_draft_save(scope=SCOPE, project_id="project", bundle_yaml=retry)
    assert len(refreshes) == 1


def test_cache_refresh_failure_preserves_created_identity_and_safe_nonblocking_warning(caplog):
    db = DraftDB()

    def refresh():
        raise RuntimeError("sensitive_cache_diagnostic")

    saved = WorkflowAuthoringService(
        db, creation_authorizer=lambda scope: None, after_save=refresh
    ).workflow_draft_save(scope=SCOPE, project_id="project", bundle_yaml=bundle())
    assert saved["workflow_id"] == "new_workflow"
    assert saved["enabled"] is False
    assert saved["editor_url"] == "/registry/workflows/new_workflow/builder"
    warning = next(
        issue for issue in saved["issues"] if issue["code"] == "authoring.cache_refresh_failed"
    )
    assert warning["blocking"] is False
    assert warning["severity"] == "warning"
    assert db.bindings == [("project", "new_workflow")]
    assert "sensitive_cache_diagnostic" not in str(saved) + caplog.text


def test_failed_transaction_does_not_refresh_cache():
    db = DraftDB()
    db.fail_binding = True
    refreshes = []
    service = WorkflowAuthoringService(
        db, creation_authorizer=lambda scope: None, after_save=lambda: refreshes.append(True)
    )
    text = bundle()
    with pytest.raises(RuntimeError, match="binding failed"):
        service.workflow_draft_save(scope=SCOPE, project_id="project", bundle_yaml=text)
    assert refreshes == []
    assert db.workflows == {}
    assert db.history == []
