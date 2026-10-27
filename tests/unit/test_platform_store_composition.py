from __future__ import annotations

from core.application.extensions.oauth_lifecycle import record_extension_oauth_uninstall_audit
from core.infrastructure.db.platform_stores import (
    PlatformControlScheduleStore,
    PlatformIdentityStore,
    PlatformIntegrationStore,
    PlatformJobStore,
    PlatformProjectStore,
    PlatformWorkerStore,
)


def test_identity_store_exposes_working_action_audit_persistence(monkeypatch) -> None:
    captured = {}
    connection = object()
    store = object.__new__(PlatformIdentityStore)
    store.conn = connection

    def capture_log_action(**kwargs):
        captured.update(kwargs)
        return {"id": 17}

    monkeypatch.setattr(
        "core.infrastructure.db.action_log_store.log_action",
        capture_log_action,
    )

    result = store.log_action(
        project_id=None,
        job_id=None,
        task_id="auth",
        task_summary="Auth login",
        agent="auth",
        action_type="auth_login",
        status="ok",
        payload={"username": "alice@example.com"},
        result={"client_ip": "127.0.0.1"},
        meta={"audit_category": "auth"},
    )

    assert result == {"id": 17}
    assert captured["conn"] is connection
    assert captured["action_type"] == "auth_login"
    assert captured["meta"] == {"audit_category": "auth"}


def test_identity_store_writes_the_extension_oauth_uninstall_audit(monkeypatch) -> None:
    """The extension lifecycle runs on the identity store; an OAuth uninstall audits through it.

    Without the writer, uninstalling File Transfer (dropbox_oauth) or Google
    Workspace (workspace_oauth) answered 500 "Durable lifecycle audit storage
    is unavailable" after the plan had been confirmed.
    """
    captured = {}
    connection = object()
    store = object.__new__(PlatformIdentityStore)
    store.conn = connection

    def capture_log_command_execution(**kwargs):
        captured.update(kwargs)
        return {"id": 23}

    monkeypatch.setattr(
        "core.infrastructure.db.command_audit_store.log_command_execution",
        capture_log_command_execution,
    )

    record_extension_oauth_uninstall_audit(
        store,
        extension_id="fixture.oauth-provider",
        account_id="account-1",
        install_id="install-1",
        plan_revision="revision-1",
        actor_type="admin",
        actor_id="admin-1",
        outcome="not_available",
    )

    assert captured["conn"] is connection
    assert captured["command_type"] == "extension_oauth_uninstall_lifecycle"
    assert captured["target"] == {
        "extension_id": "fixture.oauth-provider",
        "install_id": "install-1",
    }
    assert captured["result"] == {"remote_revocation": "not_available"}


def test_identity_store_exposes_project_secret_storage_for_extension_callbacks() -> None:
    assert callable(getattr(PlatformIdentityStore, "save_project_secret", None))
    assert callable(getattr(PlatformIdentityStore, "get_project_secret", None))


def test_project_store_exposes_project_owned_schedule_and_connection_operations() -> None:
    """The project service composition must cover its live route dependencies."""
    assert callable(getattr(PlatformProjectStore, "create_scheduled_job", None))
    assert callable(getattr(PlatformProjectStore, "list_project_connections", None))
    assert callable(getattr(PlatformProjectStore, "record_public_progress_transition", None))
    assert callable(getattr(PlatformProjectStore, "finish_job", None))
    assert callable(getattr(PlatformProjectStore, "acquire_scheduled_job_execution_lock", None))
    assert callable(getattr(PlatformProjectStore, "get_project_settings_snapshot", None))
    assert callable(getattr(PlatformProjectStore, "create_project_settings_revision", None))
    assert callable(getattr(PlatformProjectStore, "list_project_settings_revisions", None))
    assert callable(getattr(PlatformProjectStore, "get_project_settings_revision", None))
    assert callable(getattr(PlatformProjectStore, "restore_project_settings_revision", None))
    assert callable(
        getattr(PlatformControlScheduleStore, "record_public_progress_transition", None)
    )
    assert callable(getattr(PlatformControlScheduleStore, "finish_job", None))
    assert callable(getattr(PlatformControlScheduleStore, "list_project_connections", None))
    assert callable(getattr(PlatformWorkerStore, "log_action", None))
    assert callable(getattr(PlatformWorkerStore, "log_job_error", None))
    assert callable(getattr(PlatformJobStore, "get_project", None))


def test_integration_store_covers_worker_notification_runtime_surface() -> None:
    required_operations = (
        "list_notification_routes",
        "get_project_connection",
        "get_provider_secret",
        "get_extension",
        "get_extension_by_install_id",
        "log_extension_invocation",
        "log_notification_delivery",
        "get_project",
        "cursor",
    )

    assert all(
        callable(getattr(PlatformIntegrationStore, name, None)) for name in required_operations
    )
    assert not callable(getattr(PlatformWorkerStore, "get_extension", None))
    assert not callable(getattr(PlatformWorkerStore, "get_extension_by_install_id", None))
    assert not callable(getattr(PlatformWorkerStore, "log_extension_invocation", None))
