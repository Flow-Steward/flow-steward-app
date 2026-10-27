from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from core.application.extensions.install import ExtensionInstallRecord
from core.infrastructure.config.platform_identity import DEFAULT_ACCOUNT_ID

_CREDENTIAL_KEY = "credential_key"
_NOTIFIER_EXTENSION_ID = "acme.notification-provider"


def _declare_notifier_grants(extension: dict) -> None:
    """The notifier's own manifest declares the grant over its own connections."""
    extension.setdefault("compiled_manifest", {})["runtime_grants"] = {
        "mutations": {"project_connection_upsert": {"scope": "owning_extension"}}
    }


def _bind_connectionless_notification_install(
    store,
    *,
    extension_key: str = "fixture.notification-provider",
    account_id: str = DEFAULT_ACCOUNT_ID,
):
    """Bind one selected release without making catalog/latest executable."""
    install_id = f"install-{extension_key}"
    release_id = f"release-{extension_key}"
    runtime_row = {
        **dict(store.get_extension.return_value),
        "extension_key": extension_key,
        "extension_id": extension_key,
        "extension_install_id": install_id,
        "install_id": install_id,
        "release_id": release_id,
        "account_id": account_id,
    }
    store.conn = object()
    store.get_project.return_value = {"account_id": account_id}
    store.get_extension_by_install_id.return_value = runtime_row
    install_store = MagicMock()
    install_store.list_for_account.return_value = [
        ExtensionInstallRecord(
            extension_id=extension_key,
            version=str(runtime_row.get("version") or "1.0.0"),
            account_id=account_id,
            install_id=install_id,
            release_id=release_id,
        )
    ]
    return patch(
        "core.application.extensions.install.DBExtensionInstallStore",
        return_value=install_store,
    )


def test_notification_connection_hydrates_canonical_project_provider_secrets():
    from core.application.notifications.delivery import _notification_connection_target

    class Store:
        def get_project_connection(self, **_kwargs):
            return {
                "connection_id": "conn-1",
                "account_id": "acct-1",
                "project_id": "project-1",
                "extension_id": "acme.notifier",
                "extension_install_id": "install-1",
                "connection_type": "bot",
                "enabled": True,
                "secret_refs": {
                    "provider_secrets": {
                        "provider_key": "acme.notifier",
                        "keys": {"credential_value": _CREDENTIAL_KEY},
                    }
                },
            }

        def get_provider_secret(self, **kwargs):
            assert kwargs == {
                "provider_key": "acme.notifier",
                "secret_key": _CREDENTIAL_KEY,
                "project_id": "project-1",
                "include_global": False,
            }
            return "secret-value"

    runtime, logged = _notification_connection_target(
        Store(),
        connection_id="conn-1",
        account_id="acct-1",
        project_id="project-1",
        extension_id="acme.notifier",
        extension_install_id="install-1",
    )

    assert runtime["secrets"] == {"credential_value": "secret-value"}
    assert logged["secrets"] == {"credential_value": "<redacted>"}


def test_notification_connection_rejects_a_different_extension_install() -> None:
    from core.application.notifications.delivery import _notification_connection_target

    store = MagicMock()
    store.get_project_connection.return_value = {
        "connection_id": "conn-1",
        "account_id": "acct-1",
        "project_id": "project-1",
        "extension_id": "acme.notifier",
        "extension_install_id": "install-foreign",
        "enabled": True,
    }

    runtime, logged = _notification_connection_target(
        store,
        connection_id="conn-1",
        account_id="acct-1",
        project_id="project-1",
        extension_id="acme.notifier",
        extension_install_id="install-active",
    )

    assert runtime == {}
    assert logged == {}
    assert store.get_project_connection.call_args.kwargs["extension_install_id"] == (
        "install-active"
    )


def test_notification_install_resolution_overlays_manifest_runtime_grants() -> None:
    from core.application.extensions.runtime_host_grants import trusted_mutation_grant
    from core.application.notifications.delivery import _resolve_notification_extension

    raw_extension = {
        "extension_key": _NOTIFIER_EXTENSION_ID,
        "extension_id": _NOTIFIER_EXTENSION_ID,
        "install_id": "install-notifier",
        "account_id": "account-1",
        "enabled": True,
        "kind": "notification_provider",
        "compiled_manifest": {"extension_id": _NOTIFIER_EXTENSION_ID},
    }
    _declare_notifier_grants(raw_extension)

    class Store:
        @staticmethod
        def get_extension(_extension_key):
            return dict(raw_extension)

        @staticmethod
        def get_extension_by_install_id(_install_id, **_kwargs):
            return dict(raw_extension)

        @staticmethod
        def get_extension_by_install_id_for_runtime_gate(_install_id, **_kwargs):
            return dict(raw_extension)

    assert "install_metadata" not in raw_extension

    resolved = _resolve_notification_extension(
        Store(),
        {"provider_config": {"extension_key": _NOTIFIER_EXTENSION_ID}},
        extension_install_id="install-notifier",
        account_id="account-1",
    )

    assert resolved["install_id"] == "install-notifier"
    assert resolved["account_id"] == "account-1"
    assert trusted_mutation_grant(resolved, "project_connection_upsert") == {
        "scope": "owning_extension"
    }


def test_dispatch_project_notification_event_delivers_fixture_notifier_via_extension():
    from core.application.notifications.dispatch import dispatch_project_notification_event

    mock_bl = MagicMock()
    mock_bl.list_notification_routes.return_value = [
        {
            "route_id": 9,
            "route_key": "failed",
            "provider_key": "fixture_notifier",
            "provider_config": {"extension_key": "fixture.notification-provider"},
            "implementation_kind": "builtin",
            "event_type": "job.failed",
            "severity": "error",
            "route_config": {},
        }
    ]
    mock_bl.get_extension.return_value = {
        "extension_key": "fixture.notification-provider",
        "enabled": True,
        "kind": "notification_provider",
        "bundle_root": "/tmp",
        "entrypoint": {"command": ["python3", "main.py"]},
    }
    mock_bl.get_project_run_config.return_value = {"project_id": 7, "project_providers": []}
    mock_bl.log_notification_delivery.return_value = {
        "delivery_id": 1,
        "project_id": 7,
        "provider_key": "fixture_notifier",
        "status": "delivered",
    }

    with (
        _bind_connectionless_notification_install(mock_bl, account_id="acc_alpha"),
        patch(
            "core.application.notifications.delivery.run_extension_invocation",
            return_value={
                "status": "succeeded",
                "response": {"ok": True, "delivered": True},
            },
        ),
    ):
        deliveries = dispatch_project_notification_event(
            mock_bl,
            project_id=7,
            event_type="job.failed",
            severity="error",
            job_id=55,
            payload={"summary": "failed"},
            account_id="acc_alpha",
            correlation_id="job:55:job.failed",
        )

    assert len(deliveries) == 1
    assert deliveries[0]["status"] == "delivered"
    request_payload = mock_bl.log_extension_invocation.call_args.kwargs["request"]["event"]
    request_context = mock_bl.log_extension_invocation.call_args.kwargs["request_context"]
    assert request_payload["schema_version"] == "event_envelope_v1"
    assert request_payload["account_id"] == "acc_alpha"
    assert request_payload["correlation_id"] == "job:55:job.failed"
    assert request_context["trace_id"] == "job:55"
    assert request_context["event_id"] == request_payload["event_id"]
    assert request_context["correlation_id"] == "job:55:job.failed"
    mock_bl.log_notification_delivery.assert_called_once()


def test_worker_notification_retry_supplies_one_durable_event_identity() -> None:
    from core.application.jobs.execution_contracts import TaskNotification
    from core.infrastructure.runtime.worker.worker_execution_adapters import (
        WorkerTerminalEffectsAdapter,
    )

    def notification() -> TaskNotification:
        return TaskNotification(
            project_id="project-1",
            event_type="job.failed",
            severity="error",
            workflow_id="workflow-1",
            job_id="job-1",
            payload={"summary": "failed"},
        )

    worker_store = object()
    notification_store = object()
    adapter = WorkerTerminalEffectsAdapter(
        bl=worker_store,
        notification_store_provider=lambda: notification_store,
        worker_id=lambda: "worker-1",
    )
    with patch(
        "core.application.notifications.dispatch.dispatch_project_notification_event"
    ) as dispatch:
        adapter.notify(notification(), repository=MagicMock())
        adapter.notify(notification(), repository=MagicMock())

    first = dispatch.call_args_list[0].kwargs
    second = dispatch.call_args_list[1].kwargs
    assert dispatch.call_args_list[0].args[0] is notification_store
    assert dispatch.call_args_list[1].args[0] is notification_store
    assert first["event_id"]
    assert first["event_id"] == second["event_id"]
    assert first["correlation_id"] == second["correlation_id"] == first["event_id"]


def test_worker_notification_uses_real_dispatcher_with_integration_store_surface() -> None:
    from core.application.jobs.execution_contracts import TaskNotification
    from core.infrastructure.db.logger_extensions_accounts_common import (
        PlatformLoggerExtensionsAccountsCommonMixin,
    )
    from core.infrastructure.runtime.worker.worker_execution_adapters import (
        WorkerTerminalEffectsAdapter,
    )

    extension = {
        "extension_key": "fixture.notification-provider",
        "extension_install_id": "install-1",
        "install_id": "install-1",
        "enabled": True,
        "bundle_root": "/tmp",
        "entrypoint": {"command": ["python3", "main.py"]},
        "compiled_manifest": {
            "extension_id": "fixture.notification-provider",
            "kind": "notification_provider",
            "external_effects": [
                {
                    "operation_id": "deliver_notification",
                    "effect_kind": "message_delivery",
                    "channel": "notification",
                    "idempotency": "required",
                    "test_mode_behavior": "suppress",
                }
            ],
            "operation_manifest": [
                {"operation_id": "deliver_notification", "outputs": [{"name": "delivered"}]}
            ],
        },
    }

    class NotificationStore:
        def __init__(self) -> None:
            self.invocations: list[dict] = []
            self.deliveries: list[dict] = []

        def list_notification_routes(self, **_kwargs):
            return [
                {
                    "route_id": "11111111-1111-1111-1111-111111111111",
                    "route_key": "approval",
                    "provider_key": "fixture.notification-provider",
                    "integration_key": "fixture.notification-provider",
                    "implementation_kind": "extension",
                    "event_type": "job.awaiting_approval",
                    "severity": "warning",
                    "route_config": {"channel_id": "v_testbot20260314"},
                }
            ]

        def get_project(self, _project_id):
            return {"account_id": "account-1"}

        def get_project_connection(self, **_kwargs):
            return {
                "connection_id": "connection-1",
                "account_id": "account-1",
                "project_id": "project-1",
                "extension_id": "fixture.notification-provider",
                "extension_install_id": "install-1",
                "connection_config": {},
                "secret_refs": {},
                "enabled": True,
            }

        def get_extension(self, _extension_key):
            return {**extension, "kind": "notification_provider"}

        def get_extension_by_install_id(self, _install_id, **_kwargs):
            return PlatformLoggerExtensionsAccountsCommonMixin._normalize_extension_row(extension)

        def log_extension_invocation(self, **kwargs):
            self.invocations.append(kwargs)

        def log_notification_delivery(self, **kwargs):
            self.deliveries.append(kwargs)
            return dict(kwargs)

    worker_store_without_extension_runtime = object()
    notification_store = NotificationStore()
    notification = TaskNotification(
        project_id="project-1",
        event_type="job.awaiting_approval",
        severity="warning",
        workflow_id="workflow-1",
        job_id="job-1",
        payload={"summary": "Approval required", "approval_id": "gate-1"},
    )
    adapter = WorkerTerminalEffectsAdapter(
        bl=worker_store_without_extension_runtime,
        notification_store_provider=lambda: notification_store,
        worker_id=lambda: "worker-1",
    )

    with (
        patch(
            "core.application.extensions.external_effects.claim_external_effect_for_action",
            return_value={
                "declared": True,
                "effect": {
                    "effect_id": "effect-1",
                    "claim_token": "claim-1",
                    "status": "claimed",
                },
                "should_spawn": True,
            },
        ) as claim_effect,
        patch(
            "core.application.extensions.external_effects.mark_external_effect_running",
            return_value={"effect_id": "effect-1", "status": "running"},
        ) as mark_running,
        patch(
            "core.application.extensions.external_effects.confirm_external_effect_from_invocation",
            return_value={
                "effect_id": "effect-1",
                "status": "succeeded",
                "provider_message_id": "message-1",
                "__confirmed_replay_invocation": {
                    "status": "succeeded",
                    "response": {
                        "ok": True,
                        "delivered": True,
                        "provider_message_id": "message-1",
                    },
                },
            },
        ) as confirm_effect,
        patch(
            "core.application.notifications.delivery.run_extension_invocation",
            return_value={
                "status": "succeeded",
                "response": {
                    "ok": True,
                    "delivered": True,
                    "provider_message_id": "message-1",
                },
            },
        ),
    ):
        adapter.notify(notification, repository=MagicMock())

    assert claim_effect.call_args.args[0] is notification_store
    assert mark_running.call_args.args[0] is notification_store
    assert confirm_effect.call_args.args[0] is notification_store
    assert notification_store.invocations[0]["external_effect_id"] == "effect-1"
    assert notification_store.deliveries[0]["status"] == "delivered"
    assert notification_store.deliveries[0]["provider_message_id"] == "message-1"


def test_worker_notification_provider_builds_shared_integration_store_once(monkeypatch) -> None:
    from core.infrastructure.runtime.worker import worker_task_execution

    created: list[str] = []
    notification_store = object()

    class IntegrationStore:
        @classmethod
        def get_shared_instance(cls, dsn):
            created.append(dsn)
            return notification_store

    monkeypatch.setattr(
        worker_task_execution,
        "_platform_notification_store_factory",
        lambda: IntegrationStore,
    )
    provider = worker_task_execution._notification_store_provider(
        {"fs_observability_dsn": "postgresql://notification-store"}
    )

    assert provider() is notification_store
    assert provider() is notification_store
    assert created == ["postgresql://notification-store"]


@pytest.mark.parametrize(
    ("message", "service_name"),
    [
        ({"job_id": "job-1"}, "_task_service"),
        (
            {
                "message_type": "workflow_resume",
                "job_id": "job-1",
                "resume_revision": 1,
            },
            "_workflow_resume_service",
        ),
    ],
)
def test_execute_task_wires_notification_provider_to_both_terminal_paths(
    monkeypatch, message, service_name
) -> None:
    from core.infrastructure.runtime.worker import worker_task_execution

    worker_store = object()
    notification_store = object()
    captured: dict[str, object] = {}

    def notification_store_provider():
        return notification_store

    class Service:
        def execute(self, raw_message):
            captured["message"] = raw_message
            return {"action": "ack", "reason": "captured"}

    def build_service(**kwargs):
        captured.update(kwargs)
        return Service()

    monkeypatch.setattr(worker_task_execution, "_runtime_config", dict)
    monkeypatch.setattr(
        worker_task_execution, "_build_platform_logger", lambda _config: worker_store
    )
    monkeypatch.setattr(
        worker_task_execution,
        "_notification_store_provider",
        lambda _config: notification_store_provider,
    )
    monkeypatch.setattr(worker_task_execution, service_name, build_service)

    result = worker_task_execution.execute_task(message)

    assert result == {"action": "ack", "reason": "captured"}
    assert captured["bl"] is worker_store
    assert captured["notification_store_provider"] is notification_store_provider
    assert captured["notification_store_provider"]() is notification_store


def test_workflow_resume_terminal_uses_notification_store_provider() -> None:
    from core.application.jobs.execution_contracts import WorkflowResumeTerminalContext
    from core.infrastructure.runtime.worker.worker_resume_adapters import (
        WorkerWorkflowResumeTerminalAdapter,
    )

    worker_store = object()
    notification_store = object()
    adapter = WorkerWorkflowResumeTerminalAdapter(
        bl=worker_store,
        notification_store_provider=lambda: notification_store,
        worker_id=lambda: "worker-1",
    )
    context = WorkflowResumeTerminalContext(
        job_id="job-1",
        job_row={"project_id": "project-1", "task_id": "task-1"},
        final_status="completed",
        result={"status": "completed", "approval": {"approval_id": "gate-1"}},
        resume_revision=2,
        finalizer_token="finalizer-1",
        workflow_id="workflow-1",
        notification_event_type="job.completed",
    )

    with patch(
        "core.application.notifications.dispatch.dispatch_project_notification_event"
    ) as dispatch:
        adapter.notify(context)

    assert dispatch.call_args.args[0] is notification_store
    assert dispatch.call_args.kwargs["event_id"] == ("approval-resume:job-1:2:job.completed:gate-1")


def test_notification_retry_fingerprint_excludes_envelope_construction_time() -> None:
    from core.application.notifications import delivery as notification_delivery

    common = {
        "event_id": "evt-stable",
        "event_type": "job.failed",
        "correlation_id": "evt-stable",
        "account_id": "account-1",
        "project_id": "project-1",
        "job_id": "job-1",
        "payload": {"summary": "failed"},
    }
    with patch(
        "core.application.extensions.external_effects.claim_external_effect_for_action",
        return_value={"declared": True, "should_spawn": True},
    ) as claim:
        for occurred_at in ("2026-09-06T10:00:00+00:00", "2026-09-06T10:00:01+00:00"):
            notification_delivery._claim_notification_external_effect(
                bl=object(),
                extension={"extension_id": "fixture.notification"},
                extension_id="fixture.notification",
                extension_install_id="install-1",
                connection_id="connection-1",
                account_id="account-1",
                project_id="project-1",
                event={**common, "occurred_at": occurred_at},
                route_payload={"route_id": "route-1", "route_key": "failed"},
                provider_payload={"provider_key": "fixture.notification"},
            )

    first = claim.call_args_list[0].kwargs
    second = claim.call_args_list[1].kwargs
    assert first["idempotency_key_override"] == second["idempotency_key_override"]
    assert first["request_payload"] == second["request_payload"]


def test_project_notification_without_durable_event_identity_fails_before_route_claim() -> None:
    from core.application.notifications.dispatch import dispatch_project_notification_event

    bl = MagicMock()
    with pytest.raises(ValueError, match="event_id or correlation_id"):
        dispatch_project_notification_event(
            bl,
            project_id="project-1",
            event_type="project.changed",
            payload={"summary": "changed"},
        )
    bl.list_notification_routes.assert_not_called()


def test_dispatch_project_notification_event_passes_full_route_to_extension_runtime():
    from core.application.notifications.dispatch import dispatch_project_notification_event

    mock_bl = MagicMock()
    mock_bl.list_notification_routes.return_value = [
        {
            "route_id": 9,
            "route_key": "failed",
            "provider_key": "fixture_notifier",
            "provider_config": {"extension_key": "fixture.notification-provider"},
            "implementation_kind": "builtin",
            "event_type": "job.failed",
            "severity": "error",
            "created_at": datetime(2026, 5, 10, 9, 0, tzinfo=UTC),
            "route_config": {"channel_id": "live-group"},
        }
    ]
    mock_bl.get_extension.return_value = {
        "extension_key": "fixture.notification-provider",
        "enabled": True,
        "kind": "notification_provider",
        "bundle_root": "/tmp",
        "entrypoint": {"command": ["python3", "main.py"]},
    }
    mock_bl.get_project_run_config.return_value = {"project_id": 7, "project_providers": []}
    mock_bl.log_notification_delivery.return_value = {
        "delivery_id": 1,
        "project_id": 7,
        "provider_key": "fixture_notifier",
        "status": "delivered",
    }

    with (
        _bind_connectionless_notification_install(mock_bl),
        patch(
            "core.application.notifications.delivery.run_extension_invocation",
            return_value={
                "status": "succeeded",
                "response": {"ok": True, "delivered": True},
            },
        ) as invoke,
    ):
        dispatch_project_notification_event(
            mock_bl,
            project_id=7,
            event_type="job.failed",
            severity="error",
            job_id=55,
            payload={"summary": "failed"},
        )

    request_payload = invoke.call_args.kwargs["request_payload"]
    assert request_payload["route"]["route_id"] == "9"
    assert request_payload["route"]["route_key"] == "failed"
    assert request_payload["route"]["route_config"]["channel_id"] == "live-group"
    assert "created_at" not in request_payload["route"]


def test_dispatch_project_notification_event_uses_route_connection_target_and_secrets():
    from core.application.notifications.dispatch import dispatch_project_notification_event

    mock_bl = MagicMock()
    mock_bl.get_project_run_config.return_value = {"project_id": 7, "project_providers": []}
    mock_bl.get_project_connection.return_value = {
        "connection_id": "conn_route",
        "account_id": "acc_alpha",
        "project_id": "7",
        "extension_id": "example.host-fixture",
        "extension_install_id": "install-host-fixture",
        "display_name": "Route Delivery",
        "connection_type": "api_token",
        "credential_revision": 3,
        "connection_config": {
            "provider": "example_notification_api",
            "client_id": "notify-client",
            "endpoint": "https://notify.example.com/events",
        },
        "secret_refs": {"refs": {"api_token": "project_connection:conn_route:api_token"}},
        "enabled": True,
    }
    mock_bl.list_notification_routes.return_value = [
        {
            "route_id": 9,
            "route_key": "delivery_failures",
            "provider_key": "example.host-fixture",
            "implementation_kind": "extension",
            "event_type": "job.failed",
            "severity": "error",
            "route_config": {"connection_id": "conn_route", "destination": "operations"},
        }
    ]
    mock_bl.get_extension.return_value = {
        "extension_key": "example.host-fixture",
        "enabled": True,
        "kind": "notification_provider",
        "bundle_root": "/tmp",
        "entrypoint": {"command": ["python3", "main.py"]},
    }
    mock_bl.get_extension_by_install_id.return_value = {
        **mock_bl.get_extension.return_value,
        "install_id": "install-host-fixture",
    }
    mock_bl.log_notification_delivery.return_value = {
        "delivery_id": 1,
        "project_id": 7,
        "provider_key": "example.host-fixture",
        "status": "delivered",
    }

    with (
        patch(
            "core.application.management.project_connection_secrets.resolve_connection_secrets",
            return_value={"api_token": "secret-token"},
        ) as resolve_secrets,
        patch(
            "core.application.notifications.delivery.run_extension_invocation",
            return_value={"status": "succeeded", "response": {"ok": True, "delivered": True}},
        ) as invoke,
    ):
        dispatch_project_notification_event(
            mock_bl,
            project_id=7,
            event_type="job.failed",
            severity="error",
            job_id=55,
            payload={"summary": "failed"},
            account_id="acc_alpha",
        )

    request_payload = invoke.call_args.kwargs["request_payload"]
    runtime_context = invoke.call_args.kwargs["runtime_context"]
    connection = request_payload["action_payload"]["target"]["connection"]
    assert connection["connection_id"] == "conn_route"
    assert connection["config"]["endpoint"] == "https://notify.example.com/events"
    assert connection["secrets"] == {"api_token": "secret-token"}
    assert "api_token" not in connection["config"]
    assert request_payload["provider"]["provider_config"].get("api_token") is None
    assert runtime_context["account_id"] == "acc_alpha"
    assert runtime_context["extension_install_id"] == "install-host-fixture"
    resolve_secrets.assert_called_once()
    assert resolve_secrets.call_args.kwargs["secret_refs"] == {
        "refs": {"api_token": "project_connection:conn_route:api_token"}
    }


def test_dispatch_project_notification_event_forwards_runtime_context_to_extension_delivery():
    from core.application.notifications.dispatch import dispatch_project_notification_event

    mock_bl = MagicMock()
    mock_bl.list_notification_routes.return_value = [
        {
            "route_id": 9,
            "route_key": "approval_route",
            "provider_key": "fixture_notifier",
            "provider_config": {"extension_key": "fixture.notification-provider"},
            "implementation_kind": "builtin",
            "event_type": "approval.requested",
            "severity": "info",
            "route_config": {"channel_id": "live-group"},
        }
    ]
    mock_bl.get_extension.return_value = {
        "extension_key": "fixture.notification-provider",
        "enabled": True,
        "kind": "notification_provider",
        "bundle_root": "/tmp",
        "entrypoint": {"command": ["python3", "main.py"]},
    }
    mock_bl.get_project_run_config.return_value = {"project_id": 7, "project_providers": []}
    mock_bl.log_notification_delivery.return_value = {
        "delivery_id": 1,
        "project_id": 7,
        "provider_key": "fixture_notifier",
        "status": "delivered",
    }

    with (
        _bind_connectionless_notification_install(mock_bl),
        patch(
            "core.application.notifications.delivery.run_extension_invocation",
            return_value={
                "status": "succeeded",
                "response": {"ok": True, "delivered": True},
            },
        ) as invoke,
    ):
        dispatch_project_notification_event(
            mock_bl,
            project_id=7,
            event_type="approval.requested",
            severity="info",
            job_id=77,
            payload={"summary": "Approval required"},
        )

    runtime_context = invoke.call_args.kwargs["runtime_context"]
    assert isinstance(runtime_context, dict)
    assert runtime_context["logger"] is mock_bl


def test_dispatch_project_notification_event_persists_direct_extension_channel_without_uuid_route_id():
    from core.application.notifications.dispatch import dispatch_project_notification_event

    mock_bl = MagicMock()
    mock_bl.list_notification_routes.return_value = []
    mock_bl.get_extension.return_value = {
        "extension_key": "fixture.notification-provider",
        "enabled": True,
        "kind": "notification_provider",
        "bundle_root": "/tmp",
        "entrypoint": {"command": ["python3", "main.py"]},
    }
    mock_bl.get_project_run_config.return_value = {"project_id": 7, "project_providers": []}
    mock_bl.log_notification_delivery.return_value = {
        "delivery_id": 2,
        "route_id": None,
        "project_id": 7,
        "provider_key": "fixture_notifier",
        "status": "delivered",
        "provider_message_id": "45",
    }

    with (
        _bind_connectionless_notification_install(mock_bl),
        patch(
            "core.application.notifications.delivery.run_extension_invocation",
            return_value={
                "status": "succeeded",
                "response": {"ok": True, "delivered": True, "provider_message_id": "45"},
            },
        ),
    ):
        deliveries = dispatch_project_notification_event(
            mock_bl,
            project_id=7,
            event_type="workflow.automation.notify",
            severity="info",
            workflow_id="monthly_review",
            correlation_id="workflow:monthly_review:direct-route:test",
            direct_routes=[
                {
                    "route_key": "extension_channel:fixture_notifier:live-group",
                    "display_name": "fixture_notifier:live-group",
                    "provider_key": "fixture_notifier",
                    "provider_config": {"extension_key": "fixture.notification-provider"},
                    "implementation_kind": "extension",
                    "enabled": True,
                    "event_type": "workflow.automation.notify",
                    "severity": "info",
                    "route_config": {"channel_id": "live-group"},
                }
            ],
            payload={"summary": "direct route"},
        )

    assert len(deliveries) == 1
    assert deliveries[0]["status"] == "delivered"
    assert deliveries[0]["provider_message_id"] == "45"
    assert deliveries[0]["route_id"] is None
    assert mock_bl.log_notification_delivery.call_args.kwargs["route_id"] is None


def test_dispatch_project_notification_event_uses_only_direct_routes_when_provided():
    from core.application.notifications.dispatch import dispatch_project_notification_event

    mock_bl = MagicMock()
    mock_bl.list_notification_routes.return_value = [
        {
            "route_id": "unrelated-route",
            "route_key": "unrelated",
            "provider_key": "unrelated_provider",
            "implementation_kind": "extension",
            "enabled": True,
            "event_type": "approval.resolved",
            "severity": "info",
            "route_config": {"channel_id": "other-channel"},
        }
    ]
    mock_bl.get_extension.return_value = {
        "extension_key": "fixture.notification-provider",
        "enabled": True,
        "kind": "notification_provider",
        "bundle_root": "/tmp",
        "entrypoint": {"command": ["python3", "main.py"]},
    }
    mock_bl.get_project_run_config.return_value = {"project_id": 7, "project_providers": []}
    mock_bl.log_notification_delivery.side_effect = lambda **kwargs: kwargs

    with (
        _bind_connectionless_notification_install(
            mock_bl,
            extension_key="acme.chat",
        ),
        patch(
            "core.application.notifications.delivery.run_extension_invocation",
            return_value={
                "status": "succeeded",
                "response": {"ok": True, "delivered": True, "provider_message_id": "45"},
            },
        ) as invoke,
    ):
        deliveries = dispatch_project_notification_event(
            mock_bl,
            project_id=7,
            event_type="approval.resolved",
            job_id="job-7",
            direct_routes=[
                {
                    "route_key": "extension_channel:acme.chat:approvals",
                    "display_name": "acme.chat:approvals",
                    "provider_key": "acme.chat",
                    "provider_config": {"extension_key": "acme.chat"},
                    "implementation_kind": "extension",
                    "enabled": True,
                    "event_type": "approval.resolved",
                    "severity": "info",
                    "route_config": {
                        "channel_id": "approvals",
                        "required_capability": "human_decision",
                    },
                }
            ],
            ignore_filters=True,
            payload={"action": "approve", "applied": True},
        )

    assert len(deliveries) == 1
    assert invoke.call_count == 1
    assert mock_bl.list_notification_routes.call_count == 0
    assert mock_bl.log_notification_delivery.call_args.kwargs["route_id"] is None


def test_dispatch_project_notification_event_derives_account_id_from_project_when_missing():
    from core.application.notifications.dispatch import dispatch_project_notification_event

    mock_bl = MagicMock()
    mock_bl.get_project.return_value = {"id": 7, "account_id": "acc_alpha"}
    mock_bl.list_notification_routes.return_value = [
        {
            "route_id": 9,
            "route_key": "failed",
            "account_id": "acc_alpha",
            "provider_key": "fixture_notifier",
            "provider_config": {"extension_key": "fixture.notification-provider"},
            "implementation_kind": "builtin",
            "event_type": "job.failed",
            "severity": "error",
            "route_config": {},
        }
    ]
    mock_bl.get_extension.return_value = {
        "extension_key": "fixture.notification-provider",
        "enabled": True,
        "kind": "notification_provider",
        "bundle_root": "/tmp",
        "entrypoint": {"command": ["python3", "main.py"]},
    }
    mock_bl.get_project_run_config.return_value = {"project_id": 7, "project_providers": []}
    mock_bl.log_notification_delivery.return_value = {
        "delivery_id": 1,
        "project_id": 7,
        "provider_key": "fixture_notifier",
        "status": "delivered",
    }

    with (
        _bind_connectionless_notification_install(mock_bl, account_id="acc_alpha"),
        patch(
            "core.application.notifications.delivery.run_extension_invocation",
            return_value={
                "status": "succeeded",
                "response": {"ok": True, "delivered": True},
            },
        ),
    ):
        deliveries = dispatch_project_notification_event(
            mock_bl,
            project_id=7,
            event_type="job.failed",
            severity="error",
            job_id=55,
            payload={"summary": "failed"},
        )

    assert len(deliveries) == 1
    request_payload = mock_bl.log_extension_invocation.call_args.kwargs["request"]["event"]
    assert request_payload["account_id"] == "acc_alpha"


def test_dispatch_project_notification_event_handles_extension_failure():
    from core.application.notifications.dispatch import dispatch_project_notification_event

    mock_bl = MagicMock()
    mock_bl.get_project_run_config.return_value = {"project_id": 7, "project_providers": []}
    mock_bl.list_notification_routes.return_value = [
        {
            "route_id": 11,
            "route_key": "failed_ext",
            "provider_key": "slack_ext",
            "implementation_kind": "extension",
            "provider_config": {"extension_key": "example_notification_provider"},
            "integration_key": "example_notification_provider",
            "event_type": "job.failed",
            "severity": "error",
            "route_config": {},
        }
    ]
    mock_bl.get_extension.return_value = {
        "extension_key": "example_notification_provider",
        "enabled": True,
        "kind": "notification_provider",
        "bundle_root": "/tmp",
        "entrypoint": {"command": ["python3", "main.py"]},
    }
    mock_bl.log_notification_delivery.return_value = {
        "delivery_id": 2,
        "project_id": 7,
        "provider_key": "slack_ext",
        "status": "failed",
    }

    with (
        _bind_connectionless_notification_install(
            mock_bl,
            extension_key="example_notification_provider",
        ),
        patch(
            "core.application.notifications.delivery.run_extension_invocation",
            return_value={"status": "failed", "detail": "boom"},
        ) as mock_invoke,
    ):
        deliveries = dispatch_project_notification_event(
            mock_bl,
            project_id=7,
            event_type="job.failed",
            severity="error",
            job_id=56,
            payload={"summary": "failed"},
        )

    assert len(deliveries) == 1
    assert deliveries[0]["status"] == "failed"
    request_payload = mock_invoke.call_args.kwargs["request_payload"]
    assert request_payload["provider"]["project_id"] == "7"
    assert isinstance(request_payload["provider"]["project_config"], dict)
    assert request_payload["provider"]["project_config"]["project_id"] == "7"
    mock_bl.log_extension_invocation.assert_called_once()


def test_dispatch_project_notification_event_links_external_effect_logs():
    from core.application.notifications.dispatch import dispatch_project_notification_event

    mock_bl = MagicMock()
    mock_bl.get_project_run_config.return_value = {"project_id": 7, "project_providers": []}
    mock_bl.get_project_connection.return_value = {
        "connection_id": "conn_delivery",
        "account_id": "acc_alpha",
        "project_id": "7",
        "extension_id": "example.host-fixture",
        "extension_install_id": "install-delivery",
        "connection_config": {},
        "secret_refs": [],
        "enabled": True,
    }
    mock_bl.record_event_dispatch.return_value = {"dispatch_id": "dispatch_1"}
    mock_bl.list_notification_routes.return_value = [
        {
            "route_id": "11111111-1111-1111-1111-111111111111",
            "route_key": "delivery_failures",
            "provider_key": "example.host-fixture",
            "implementation_kind": "extension",
            "event_type": "job.failed",
            "severity": "error",
            "route_config": {"connection_id": "conn_delivery"},
        }
    ]
    mock_bl.get_extension.return_value = {
        "extension_key": "example.host-fixture",
        "enabled": True,
        "kind": "notification_provider",
        "bundle_root": "/tmp",
        "entrypoint": {"command": ["python3", "main.py"]},
        "compiled_manifest": {
            "external_effects": [
                {
                    "operation_id": "deliver_notification",
                    "effect_kind": "message_delivery",
                    "channel": "notification",
                    "idempotency": "required",
                    "test_mode_behavior": "suppress",
                }
            ],
            "operation_manifest": [
                {"operation_id": "deliver_notification", "outputs": [{"name": "delivered"}]}
            ],
        },
    }
    mock_bl.get_extension_by_install_id.return_value = {
        **mock_bl.get_extension.return_value,
        "install_id": "install-delivery",
    }
    mock_bl.log_notification_delivery.return_value = {
        "delivery_id": 2,
        "project_id": 7,
        "provider_key": "example.host-fixture",
        "status": "delivered",
    }

    with (
        patch(
            "core.application.extensions.external_effects.cleanup_stale_external_effects",
            return_value={},
        ),
        patch(
            "core.application.extensions.external_effects.claim_external_effect",
            return_value={
                "effect": {"effect_id": "eff_delivery_1", "status": "claimed"},
                "should_spawn": True,
                "suppressed": False,
                "deduped": False,
            },
        ) as claim,
        patch(
            "core.application.extensions.external_effects.mark_external_effect_running",
            return_value={"effect_id": "eff_delivery_1", "status": "running"},
        ),
        patch(
            "core.application.extensions.external_effects.confirm_external_effect_from_invocation",
            return_value={
                "effect_id": "eff_delivery_1",
                "status": "succeeded",
                "__confirmed_replay_invocation": {
                    "status": "succeeded",
                    "response": {"ok": True, "delivered": True},
                },
            },
        ),
        patch(
            "core.application.notifications.delivery.run_extension_invocation",
            return_value={"status": "succeeded", "response": {"ok": True, "delivered": True}},
        ),
    ):
        dispatch_project_notification_event(
            mock_bl,
            project_id=7,
            event_type="job.failed",
            severity="error",
            job_id=56,
            payload={"summary": "failed"},
            account_id="acc_alpha",
        )

    claim.assert_called_once()
    assert claim.call_args.kwargs["connection_id"] == "conn_delivery"
    assert (
        mock_bl.log_extension_invocation.call_args.kwargs["external_effect_id"] == "eff_delivery_1"
    )
    assert (
        mock_bl.log_notification_delivery.call_args.kwargs["external_effect_id"] == "eff_delivery_1"
    )
    assert (
        mock_bl.record_event_delivery_attempt.call_args.kwargs["external_effect_id"]
        == "eff_delivery_1"
    )


def test_duplicate_notification_external_effect_does_not_invoke_provider_twice():
    from core.application.notifications.delivery import _deliver_via_extension

    mock_bl = MagicMock()
    mock_bl.get_project_run_config.return_value = {"project_id": "7"}
    extension = {
        "extension_key": "acme.notification-provider",
        "extension_install_id": "install-notifier",
        "entrypoint": {"command": ["python3", "main.py"]},
        "compiled_manifest": {
            "external_effects": [
                {
                    "operation_id": "deliver_notification",
                    "effect_kind": "message_delivery",
                    "channel": "notification",
                    "idempotency": "required",
                    "test_mode_behavior": "suppress",
                }
            ],
            "operation_manifest": [
                {"operation_id": "deliver_notification", "outputs": [{"name": "delivered"}]}
            ],
        },
    }
    event = {
        "event_id": "event-notifier-once",
        "account_id": "acc-alpha",
        "project_id": "7",
        "event_type": "job.failed",
    }
    route = {
        "route_id": "route-notifier",
        "provider_key": "acme.notification-provider",
        "route_config": {},
    }

    with (
        patch(
            "core.application.notifications.delivery._resolve_notification_extension",
            return_value=extension,
        ),
        patch(
            "core.application.extensions.external_effects.cleanup_stale_external_effects",
            return_value={},
        ),
        patch(
            "core.application.extensions.external_effects.claim_external_effect",
            side_effect=[
                {
                    "effect": {
                        "effect_id": "effect-notifier-once",
                        "claim_token": "claim-token",
                        "status": "claimed",
                    },
                    "should_spawn": True,
                },
                {
                    "effect": {
                        "effect_id": "effect-notifier-once",
                        "status": "succeeded",
                        "provider_message_id": "notifier-message-1",
                    },
                    "should_spawn": False,
                    "deduped": True,
                },
            ],
        ),
        patch(
            "core.application.extensions.external_effects.mark_external_effect_running",
            return_value={"effect_id": "effect-notifier-once", "status": "running"},
        ),
        patch(
            "core.application.extensions.external_effects.confirm_external_effect_from_invocation",
            return_value={
                "effect_id": "effect-notifier-once",
                "status": "succeeded",
                "provider_message_id": "notifier-message-1",
                "__confirmed_replay_invocation": {
                    "status": "succeeded",
                    "response": {
                        "ok": True,
                        "result": {
                            "delivered": True,
                            "provider_message_id": "notifier-message-1",
                        },
                    },
                },
            },
        ),
        patch(
            "core.application.extensions.external_effect_replay.hydrate_external_effect_claim",
            side_effect=lambda _db, claim: {
                **claim,
                "replay_invocation": {
                    "status": "succeeded",
                    "response": {
                        "ok": True,
                        "result": {
                            "delivered": True,
                            "provider_message_id": "notifier-message-1",
                        },
                    },
                },
            },
        ),
        patch(
            "core.application.notifications.delivery.run_extension_invocation",
            return_value={
                "status": "succeeded",
                "response": {
                    "ok": True,
                    "result": {
                        "delivered": True,
                        "provider_message_id": "notifier-message-1",
                    },
                },
            },
        ) as invoke,
    ):
        first = _deliver_via_extension(
            bl=mock_bl,
            provider={"provider_key": "acme.notification-provider"},
            event=event,
            route=route,
            project_id="7",
        )
        replay = _deliver_via_extension(
            bl=mock_bl,
            provider={"provider_key": "acme.notification-provider"},
            event=event,
            route=route,
            project_id="7",
        )

    assert first["status"] == "delivered"
    assert replay["status"] == "delivered"
    assert first["provider_message_id"] == "notifier-message-1"
    assert replay["provider_message_id"] == "notifier-message-1"
    assert first["response"] == replay["response"]
    invoke.assert_called_once()


def test_notification_external_effect_uses_stable_keyed_event_route_intent(monkeypatch):
    from core.application.notifications.delivery import _claim_notification_external_effect

    monkeypatch.setenv("ENCRYPTION_KEY", "notification-intent-test-root")
    common = {
        "bl": object(),
        "extension": {"extension_key": "acme.notification-provider"},
        "extension_id": "acme.notification-provider",
        "extension_install_id": "install-notifier",
        "connection_id": "connection-notifier",
        "account_id": "acc-alpha",
        "project_id": "project-alpha",
        "event": {"event_id": "event-1", "job_id": "job-1"},
        "route_payload": {"route_id": "route-1", "route_key": "failures"},
    }
    with patch(
        "core.application.extensions.external_effects.claim_external_effect_for_action",
        return_value={"declared": True, "should_spawn": True, "effect": {}},
    ) as claim:
        _claim_notification_external_effect(
            **common,
            provider_payload={"chat_id": "chat-a", "token": "secret-a"},
        )
        _claim_notification_external_effect(
            **common,
            provider_payload={"chat_id": "chat-b", "token": "secret-b"},
        )
        _claim_notification_external_effect(
            **{
                **common,
                "event": {"event_id": "event-2", "job_id": "job-1"},
            },
            provider_payload={"chat_id": "chat-b", "token": "secret-b"},
        )

    overrides = [call.kwargs.get("idempotency_key_override") for call in claim.call_args_list]
    assert overrides[0] == overrides[1]
    assert overrides[2] != overrides[0]
    assert str(overrides[0]).startswith("hmac-sha256:")
    assert "event-1" not in str(overrides[0])
    assert (
        claim.call_args_list[0].kwargs["request_payload"]
        != claim.call_args_list[1].kwargs["request_payload"]
    )


def test_notification_runner_exception_confirms_timeout_and_duplicate_never_respawns():
    from core.application.notifications.delivery import _deliver_via_extension

    mock_bl = MagicMock()
    mock_bl.get_project_run_config.return_value = {"project_id": "7"}
    extension = {
        "extension_key": "acme.notification-provider",
        "extension_install_id": "install-notifier",
        "entrypoint": {"command": ["python3", "main.py"]},
        "compiled_manifest": {
            "external_effects": [
                {
                    "operation_id": "deliver_notification",
                    "effect_kind": "message_delivery",
                    "channel": "notification",
                    "idempotency": "required",
                }
            ],
            "operation_manifest": [
                {"operation_id": "deliver_notification", "outputs": [{"name": "delivered"}]}
            ],
        },
    }
    event = {"event_id": "event-timeout", "account_id": "acc-alpha", "project_id": "7"}
    route = {"route_id": "route-notifier", "provider_key": "acme.notification-provider"}

    with (
        patch(
            "core.application.notifications.delivery._resolve_notification_extension",
            return_value=extension,
        ),
        patch(
            "core.application.extensions.external_effects.cleanup_stale_external_effects",
            return_value={},
        ),
        patch(
            "core.application.extensions.external_effects.claim_external_effect",
            side_effect=[
                {
                    "effect": {
                        "effect_id": "effect-timeout",
                        "claim_token": "claim-1",
                        "status": "claimed",
                    },
                    "should_spawn": True,
                },
                {
                    "effect": {"effect_id": "effect-timeout", "status": "timeout_unknown"},
                    "should_spawn": False,
                },
            ],
        ),
        patch(
            "core.application.extensions.external_effects.mark_external_effect_running",
            return_value={"effect_id": "effect-timeout", "status": "running"},
        ),
        patch(
            "core.application.extensions.external_effects.confirm_external_effect_from_invocation",
            return_value={"effect_id": "effect-timeout", "status": "timeout_unknown"},
        ) as confirm,
        patch(
            "core.application.extensions.external_effect_replay.hydrate_external_effect_claim",
            side_effect=lambda _db, claim: {
                **claim,
                "error": "external_effect_timeout_unknown",
            },
        ),
        patch(
            "core.application.notifications.delivery.run_extension_invocation",
            side_effect=RuntimeError("provider transport detail"),
        ) as provider,
    ):
        first = _deliver_via_extension(
            bl=mock_bl,
            provider={"provider_key": route["provider_key"]},
            event=event,
            route=route,
            project_id="7",
        )
        duplicate = _deliver_via_extension(
            bl=mock_bl,
            provider={"provider_key": route["provider_key"]},
            event=event,
            route=route,
            project_id="7",
        )

    assert first["status"] == "failed"
    assert duplicate["status"] == "failed"
    provider.assert_called_once()
    confirm.assert_called_once()
    assert confirm.call_args.kwargs["claim_token"] == "claim-1"
    assert (
        confirm.call_args.kwargs["invocation"]["detail"]
        == "external_effect_provider_invocation_exception"
    )
    assert "provider transport detail" not in repr(first)


def _two_phase_notification_extension():
    return {
        "extension_key": "example.two-phase",
        "extension_install_id": "install-two-phase",
        "enabled": True,
        "kind": "notification_provider",
        "entrypoint": {"command": ["python3", "main.py"]},
        "compiled_manifest": {
            "action_definitions": [
                {
                    "action_id": "deliver_notification",
                    "handler": {
                        "mode": "extension",
                        "config": {
                            "host_mutations": ["project_connection_upsert"],
                            "pre_dispatch_host_mutations": ["project_connection_upsert"],
                        },
                    },
                }
            ],
            "external_effects": [
                {
                    "operation_id": "deliver_notification",
                    "effect_kind": "message_delivery",
                    "channel": "notification",
                    "idempotency": "required",
                    "test_mode_behavior": "suppress",
                }
            ],
            "operation_manifest": [
                {"operation_id": "deliver_notification", "outputs": [{"name": "delivered"}]}
            ],
        },
    }


@pytest.mark.parametrize(
    ("replay_response", "expected_status"),
    [
        ({"ok": True, "delivered": True, "provider_message_id": "message-1"}, "delivered"),
        (
            {
                "ok": True,
                "result": {"delivered": False, "provider_message_id": "message-1"},
            },
            "failed",
        ),
    ],
)
def test_two_phase_duplicate_claim_skips_prepare_and_execute(
    replay_response: dict,
    expected_status: str,
):
    from core.application.notifications.delivery import _deliver_via_extension

    mock_bl = MagicMock()
    mock_bl.get_project_run_config.return_value = {"project_id": "7"}
    with (
        patch(
            "core.application.notifications.delivery._resolve_notification_extension",
            return_value=_two_phase_notification_extension(),
        ),
        patch(
            "core.application.extensions.provider_runtime.resolve_extension_runtime_config",
            return_value={},
        ),
        patch(
            "core.application.extensions.external_effects.claim_external_effect",
            return_value={
                "effect": {
                    "effect_id": "effect-1",
                    "status": "succeeded",
                    "provider_message_id": "message-1",
                },
                "should_spawn": False,
                "deduped": True,
            },
        ),
        patch(
            "core.application.extensions.external_effect_replay.hydrate_external_effect_claim",
            side_effect=lambda _db, claim: {
                **claim,
                "replay_invocation": {
                    "status": "succeeded",
                    "response": replay_response,
                },
            },
        ),
        patch("core.application.notifications.delivery.run_extension_invocation") as invoke,
    ):
        result = _deliver_via_extension(
            bl=mock_bl,
            provider={"provider_key": "example.two-phase"},
            event={"event_id": "event-1", "account_id": "acc-1"},
            route={"route_id": "route-1", "route_config": {}},
            project_id="7",
        )

    assert result["status"] == expected_status
    assert result["provider_message_id"] == "message-1"
    invoke.assert_not_called()


@pytest.mark.parametrize(
    "response",
    [
        {"ok": True, "delivered": False},
        {"ok": True, "result": {"delivered": False}},
    ],
)
def test_notification_delivery_false_without_definitive_metadata_is_uncertain(
    response: dict,
) -> None:
    from core.application.notifications.contracts import _normalize_notification_delivery_invocation

    normalized = _normalize_notification_delivery_invocation(
        {"status": "succeeded", "response": response}
    )

    assert normalized["status"] == "failed"
    assert normalized["detail"] == "external_effect_timeout_unknown"
    assert normalized["response"]["external_effect_status"] == "timeout_unknown"
    assert "definitely_no_external_effect" not in normalized["response"]


def test_notification_delivery_false_preserves_definitive_no_effect_failure() -> None:
    from core.application.notifications.contracts import _normalize_notification_delivery_invocation

    normalized = _normalize_notification_delivery_invocation(
        {
            "status": "succeeded",
            "response": {
                "ok": True,
                "delivered": False,
                "external_effect_status": "failed",
                "definitely_no_external_effect": True,
            },
        }
    )

    assert normalized["status"] == "failed"
    assert normalized["detail"] == "notification_not_delivered"
    assert normalized["response"]["external_effect_status"] == "failed"
    assert normalized["response"]["definitely_no_external_effect"] is True


def test_notification_delivery_false_preserves_explicit_timeout_unknown() -> None:
    from core.application.notifications.contracts import _normalize_notification_delivery_invocation

    normalized = _normalize_notification_delivery_invocation(
        {
            "status": "succeeded",
            "response": {
                "ok": True,
                "result": {
                    "delivered": False,
                    "external_effect_status": "timeout_unknown",
                },
            },
        }
    )

    assert normalized["status"] == "failed"
    assert normalized["detail"] == "external_effect_timeout_unknown"
    assert normalized["response"]["result"]["external_effect_status"] == "timeout_unknown"
    assert normalized["response"]["external_effect_status"] == "timeout_unknown"


def test_notification_delivery_false_timeout_is_deduped_without_provider_retry():
    from core.application.notifications.delivery import _deliver_via_extension

    mock_bl = MagicMock()
    mock_bl.get_project_run_config.return_value = {"project_id": "7"}
    extension = {
        "extension_key": "acme.notification-provider",
        "extension_install_id": "install-notifier",
        "entrypoint": {"command": ["python3", "main.py"]},
        "compiled_manifest": {
            "external_effects": [
                {
                    "operation_id": "deliver_notification",
                    "effect_kind": "message_delivery",
                    "channel": "notification",
                    "idempotency": "required",
                }
            ],
            "operation_manifest": [
                {"operation_id": "deliver_notification", "outputs": [{"name": "delivered"}]}
            ],
        },
    }
    event = {"event_id": "event-not-delivered", "account_id": "acc-alpha", "project_id": "7"}
    route = {"route_id": "route-notifier", "provider_key": "acme.notification-provider"}

    with (
        patch(
            "core.application.notifications.delivery._resolve_notification_extension",
            return_value=extension,
        ),
        patch(
            "core.application.extensions.external_effects.cleanup_stale_external_effects",
            return_value={},
        ),
        patch(
            "core.application.extensions.external_effects.claim_external_effect",
            side_effect=[
                {
                    "effect": {
                        "effect_id": "effect-timeout",
                        "claim_token": "claim-1",
                        "status": "claimed",
                    },
                    "should_spawn": True,
                },
                {
                    "effect": {"effect_id": "effect-timeout", "status": "timeout_unknown"},
                    "should_spawn": False,
                },
            ],
        ),
        patch(
            "core.application.extensions.external_effects.mark_external_effect_running",
            return_value={"effect_id": "effect-timeout", "status": "running"},
        ),
        patch(
            "core.application.extensions.external_effects.confirm_external_effect_from_invocation",
            return_value={"effect_id": "effect-timeout", "status": "timeout_unknown"},
        ) as confirm,
        patch(
            "core.application.extensions.external_effect_replay.hydrate_external_effect_claim",
            side_effect=lambda _db, claim: {
                **claim,
                "error": "external_effect_timeout_unknown",
            },
        ),
        patch(
            "core.application.notifications.delivery.run_extension_invocation",
            return_value={
                "status": "succeeded",
                "response": {"ok": True, "delivered": False},
            },
        ) as provider,
    ):
        first = _deliver_via_extension(
            bl=mock_bl,
            provider={"provider_key": route["provider_key"]},
            event=event,
            route=route,
            project_id="7",
        )
        duplicate = _deliver_via_extension(
            bl=mock_bl,
            provider={"provider_key": route["provider_key"]},
            event=event,
            route=route,
            project_id="7",
        )

    assert first["status"] == duplicate["status"] == "failed"
    assert first["error_text"] == duplicate["error_text"] == "external_effect_timeout_unknown"
    provider.assert_called_once()
    confirm.assert_called_once()
    assert (
        confirm.call_args.kwargs["invocation"]["response"]["external_effect_status"]
        == "timeout_unknown"
    )


def test_two_phase_notification_authorizes_prepare_mutation_before_execute():
    from core.application.notifications.delivery import _deliver_via_extension

    order: list[str] = []
    extension = _two_phase_notification_extension()
    _declare_notifier_grants(extension)
    extension["extension_key"] = _NOTIFIER_EXTENSION_ID
    extension["extension_id"] = _NOTIFIER_EXTENSION_ID
    extension["install_id"] = "install-two-phase"
    extension["account_id"] = "acc-1"
    extension["compiled_manifest"]["extension_id"] = _NOTIFIER_EXTENSION_ID
    extension["compiled_manifest"]["kind"] = "notification_provider"
    extension["compiled_manifest"]["connection_types"] = [{"connection_type_id": "notification"}]

    class NotificationStore:
        def __init__(self) -> None:
            self.connection = {
                "connection_id": "conn-1",
                "account_id": "acc-1",
                "project_id": "7",
                "extension_id": _NOTIFIER_EXTENSION_ID,
                "extension_install_id": "install-two-phase",
                "connection_kind": "extension",
                "connection_type": "notification",
                "display_name": "Notifications",
                "enabled": True,
                "connection_config": {},
                "secret_refs": {},
                "connection_routes": [],
                "ingress_config": {},
                "workflow_mappings": {},
            }
            self.writes: list[dict] = []

        @staticmethod
        def get_project_run_config(_project_id):
            return {"project_id": "7"}

        @staticmethod
        def cursor():
            return None

        def get_project_connection(self, **_kwargs):
            return dict(self.connection)

        @staticmethod
        def get_extension(_extension_key):
            return dict(extension)

        @staticmethod
        def get_extension_by_install_id(_install_id, **_kwargs):
            return dict(extension)

        @staticmethod
        def get_extension_by_install_id_for_runtime_gate(_install_id, **_kwargs):
            return dict(extension)

        def upsert_project_connection(self, *, project_id, extension_id, **payload):
            order.append("persist")
            self.writes.append({"project_id": project_id, "extension_id": extension_id, **payload})
            self.connection = {
                **self.connection,
                **payload,
                "project_id": project_id,
                "extension_id": extension_id,
            }
            return dict(self.connection)

    store = NotificationStore()

    def invoke(_extension, *, request_payload, **_kwargs):
        phase = request_payload["delivery_phase"]
        order.append(phase)
        if phase == "prepare":
            return {
                "status": "succeeded",
                "response": {
                    "ok": True,
                    "delivered": False,
                    "prepared_delivery": {"opaque": {"value": "ready"}},
                    "host_mutations": [
                        {
                            "kind": "project_connection_upsert",
                            "payload": {
                                "connection_id": "conn-1",
                                "extension_install_id": "install-two-phase",
                                "connection_config": {"prepared": True},
                            },
                        }
                    ],
                },
            }
        assert request_payload["prepared_delivery"] == {"opaque": {"value": "ready"}}
        return {
            "status": "succeeded",
            "response": {"ok": True, "delivered": True},
        }

    def mark_running(*_args, **_kwargs):
        order.append("running")
        return {"effect_id": "effect-1", "status": "running"}

    def claim_effect(*_args, **_kwargs):
        order.append("claim")
        return {
            "effect": {
                "effect_id": "effect-1",
                "claim_token": "claim-1",
                "status": "claimed",
            },
            "should_spawn": True,
        }

    with (
        patch(
            "core.application.extensions.external_effects.claim_external_effect",
            side_effect=claim_effect,
        ),
        patch(
            "core.application.extensions.external_effects.mark_external_effect_running",
            side_effect=mark_running,
        ),
        patch(
            "core.application.extensions.external_effects.confirm_external_effect_from_invocation",
            return_value={
                "effect_id": "effect-1",
                "status": "succeeded",
                "__confirmed_replay_invocation": {
                    "status": "succeeded",
                    "response": {"ok": True, "delivered": True},
                },
            },
        ),
        patch(
            "core.application.notifications.delivery.run_extension_invocation",
            side_effect=invoke,
        ),
    ):
        result = _deliver_via_extension(
            bl=store,
            provider={"provider_key": _NOTIFIER_EXTENSION_ID},
            event={"event_id": "event-1", "account_id": "acc-1"},
            route={"route_id": "route-1", "route_config": {"connection_id": "conn-1"}},
            project_id="7",
        )

    assert result["status"] == "delivered", result
    assert order == ["claim", "prepare", "persist", "running", "execute"]
    assert store.writes[0]["connection_config"] == {"prepared": True}


def test_two_phase_mark_running_failure_prevents_execute_and_is_definitive():
    from core.application.notifications.delivery import _deliver_via_extension

    mock_bl = MagicMock()
    mock_bl.get_project_run_config.return_value = {"project_id": "7"}
    phases = []

    def invoke(_extension, *, request_payload, **_kwargs):
        phases.append(request_payload["delivery_phase"])
        return {
            "status": "succeeded",
            "response": {
                "ok": True,
                "delivered": False,
                "prepared_delivery": {},
            },
        }

    with (
        patch(
            "core.application.notifications.delivery._resolve_notification_extension",
            return_value=_two_phase_notification_extension(),
        ),
        patch(
            "core.application.extensions.provider_runtime.resolve_extension_runtime_config",
            return_value={},
        ),
        patch(
            "core.application.extensions.external_effects.claim_external_effect",
            return_value={
                "effect": {
                    "effect_id": "effect-1",
                    "claim_token": "claim-1",
                    "status": "claimed",
                },
                "should_spawn": True,
            },
        ),
        patch("core.application.extensions.response_envelope.process_project_state_updates"),
        patch(
            "core.application.extensions.external_effects.mark_external_effect_running",
            return_value=None,
        ),
        patch(
            "core.application.extensions.external_effects.confirm_external_effect_from_invocation",
            return_value={"effect_id": "effect-1", "status": "failed"},
        ) as confirm,
        patch(
            "core.application.notifications.delivery.run_extension_invocation",
            side_effect=invoke,
        ),
    ):
        result = _deliver_via_extension(
            bl=mock_bl,
            provider={"provider_key": "example.two-phase"},
            event={"event_id": "event-1", "account_id": "acc-1"},
            route={"route_id": "route-1", "route_config": {}},
            project_id="7",
        )

    assert result["status"] == "failed"
    assert phases == ["prepare"]
    failed_confirmation = confirm.call_args.kwargs["invocation"]
    assert failed_confirmation["response"]["definitely_no_external_effect"] is True
    assert confirm.call_args.kwargs["expected_current_status"] == "claimed"


def test_two_phase_execute_rejects_nested_reserved_mutation_before_processing():
    from core.application.notifications.delivery import _deliver_via_extension

    mock_bl = MagicMock()
    mock_bl.get_project_run_config.return_value = {"project_id": "7"}

    def invoke(_extension, *, request_payload, **_kwargs):
        if request_payload["delivery_phase"] == "prepare":
            return {
                "status": "succeeded",
                "response": {
                    "ok": True,
                    "delivered": False,
                    "prepared_delivery": {},
                },
            }
        return {
            "status": "succeeded",
            "response": {
                "ok": True,
                "delivered": True,
                "result": {
                    "host_mutations": [{"kind": "project_connection_upsert", "payload": {}}]
                },
            },
        }

    with (
        patch(
            "core.application.notifications.delivery._resolve_notification_extension",
            return_value=_two_phase_notification_extension(),
        ),
        patch(
            "core.application.extensions.provider_runtime.resolve_extension_runtime_config",
            return_value={},
        ),
        patch(
            "core.application.extensions.external_effects.claim_external_effect",
            return_value={
                "effect": {
                    "effect_id": "effect-1",
                    "claim_token": "claim-1",
                    "status": "claimed",
                },
                "should_spawn": True,
            },
        ),
        patch(
            "core.application.extensions.external_effects.mark_external_effect_running",
            return_value={"effect_id": "effect-1", "status": "running"},
        ),
        patch(
            "core.application.extensions.response_envelope.process_project_state_updates"
        ) as process_updates,
        patch(
            "core.application.extensions.external_effects.confirm_external_effect_from_invocation",
            return_value={"effect_id": "effect-1", "status": "timeout_unknown"},
        ),
        patch(
            "core.application.notifications.delivery.run_extension_invocation",
            side_effect=invoke,
        ),
    ):
        result = _deliver_via_extension(
            bl=mock_bl,
            provider={"provider_key": "example.two-phase"},
            event={"event_id": "event-1", "account_id": "acc-1"},
            route={"route_id": "route-1", "route_config": {}},
            project_id="7",
        )

    assert result["status"] == "failed"
    assert result["error_text"] == "execute_pre_dispatch_host_mutation_forbidden"
    process_updates.assert_called_once()


def test_two_phase_prepare_is_secret_free_but_execute_receives_secrets():
    from core.application.notifications.delivery import _deliver_via_extension

    mock_bl = MagicMock()
    mock_bl.get_project_run_config.return_value = {
        "project_id": "7",
        "project_providers": [{"api_token": "project-secret"}],
    }
    extension = _two_phase_notification_extension()
    connection = {
        "connection_id": "conn-1",
        "account_id": "acc-1",
        "project_id": "7",
        "extension_id": "example.two-phase",
        "extension_install_id": "install-two-phase",
        "config": {"channels": [{"channel_id": "ops"}]},
        "secret_refs": {"refs": {"api_token": "ref-1"}},
        "secrets": {"api_token": "super-secret"},
        "ingress_config": {"mode": "webhook"},
    }
    requests = []

    def invoke(_extension, *, request_payload, **_kwargs):
        requests.append(request_payload)
        if request_payload["delivery_phase"] == "prepare":
            return {
                "status": "succeeded",
                "response": {
                    "ok": True,
                    "delivered": False,
                    "prepared_delivery": {},
                },
            }
        return {
            "status": "succeeded",
            "response": {"ok": True, "delivered": True},
        }

    with (
        patch(
            "core.application.notifications.delivery._resolve_notification_extension",
            return_value=extension,
        ),
        patch(
            "core.application.notifications.delivery._load_notification_connection_row",
            return_value={
                "connection_id": "conn-1",
                "account_id": "acc-1",
                "project_id": "7",
                "extension_id": "example.two-phase",
                "extension_install_id": "install-two-phase",
                "enabled": True,
            },
        ),
        patch(
            "core.application.notifications.delivery._notification_connection_target",
            return_value=(connection, {**connection, "secrets": {"api_token": "<redacted>"}}),
        ),
        patch(
            "core.application.extensions.external_effects.claim_external_effect",
            return_value={
                "effect": {
                    "effect_id": "effect-1",
                    "claim_token": "claim-1",
                    "status": "claimed",
                },
                "should_spawn": True,
            },
        ) as claim,
        patch(
            "core.application.extensions.external_effects.mark_external_effect_running",
            return_value={"effect_id": "effect-1", "status": "running"},
        ),
        patch("core.application.extensions.response_envelope.process_project_state_updates"),
        patch(
            "core.application.extensions.external_effects.confirm_external_effect_from_invocation",
            return_value={
                "effect_id": "effect-1",
                "status": "succeeded",
                "__confirmed_replay_invocation": {
                    "status": "succeeded",
                    "response": {"ok": True, "delivered": True},
                },
            },
        ),
        patch(
            "core.application.notifications.delivery.run_extension_invocation",
            side_effect=invoke,
        ),
    ):
        result = _deliver_via_extension(
            bl=mock_bl,
            provider={"provider_key": "example.two-phase"},
            event={"event_id": "event-1", "account_id": "acc-1"},
            route={
                "route_id": "route-1",
                "route_config": {"connection_id": "conn-1"},
            },
            project_id="7",
        )

    assert result["status"] == "delivered"
    prepare, execute = requests
    assert "secrets" not in prepare["action_payload"]["target"]["connection"]
    assert "secret_refs" not in prepare["action_payload"]["target"]["connection"]
    assert "project_config" not in prepare["provider"]
    assert "secret_refs" not in prepare["provider"]
    assert "super-secret" not in repr(prepare)
    assert "super-secret" not in repr(claim.call_args.kwargs["request_payload"])
    assert "project_config" not in claim.call_args.kwargs["request_payload"]["provider"]
    assert execute["action_payload"]["target"]["connection"]["secrets"] == {
        "api_token": "super-secret"
    }


def test_two_phase_prepare_persistence_failure_prevents_send_and_allows_retry():
    from core.application.notifications.delivery import _deliver_via_extension

    mock_bl = MagicMock()
    mock_bl.get_project_run_config.return_value = {"project_id": "7"}
    extension = _two_phase_notification_extension()
    phases = []

    def invoke(_extension, *, request_payload, **_kwargs):
        phases.append(request_payload["delivery_phase"])
        if request_payload["delivery_phase"] == "prepare":
            return {
                "status": "succeeded",
                "response": {
                    "ok": True,
                    "delivered": False,
                    "prepared_delivery": {},
                    "host_mutations": [{"kind": "project_connection_upsert", "payload": {}}],
                },
            }
        return {
            "status": "succeeded",
            "response": {"ok": True, "delivered": True},
        }

    with (
        patch(
            "core.application.notifications.delivery._resolve_notification_extension",
            return_value=extension,
        ),
        patch(
            "core.application.extensions.provider_runtime.resolve_extension_runtime_config",
            return_value={},
        ),
        patch(
            "core.application.extensions.external_effects.claim_external_effect",
            side_effect=[
                {
                    "effect": {
                        "effect_id": "effect-1",
                        "claim_token": "claim-1",
                        "status": "claimed",
                    },
                    "should_spawn": True,
                },
                {
                    "effect": {
                        "effect_id": "effect-1",
                        "claim_token": "claim-2",
                        "status": "claimed",
                    },
                    "should_spawn": True,
                },
            ],
        ),
        patch(
            "core.application.extensions.response_envelope.process_project_state_updates",
            side_effect=[RuntimeError("write failed"), None, None],
        ),
        patch(
            "core.application.extensions.external_effects.mark_external_effect_running",
            return_value={"effect_id": "effect-1", "status": "running"},
        ) as mark_running,
        patch(
            "core.application.extensions.external_effects.confirm_external_effect_from_invocation",
            side_effect=[
                {"effect_id": "effect-1", "status": "failed"},
                {
                    "effect_id": "effect-1",
                    "status": "succeeded",
                    "__confirmed_replay_invocation": {
                        "status": "succeeded",
                        "response": {"ok": True, "delivered": True},
                    },
                },
            ],
        ) as confirm,
        patch(
            "core.application.notifications.delivery.run_extension_invocation",
            side_effect=invoke,
        ),
    ):
        first = _deliver_via_extension(
            bl=mock_bl,
            provider={"provider_key": "example.two-phase"},
            event={"event_id": "event-1", "account_id": "acc-1"},
            route={"route_id": "route-1", "route_config": {}},
            project_id="7",
        )
        second = _deliver_via_extension(
            bl=mock_bl,
            provider={"provider_key": "example.two-phase"},
            event={"event_id": "event-1", "account_id": "acc-1"},
            route={"route_id": "route-1", "route_config": {}},
            project_id="7",
        )

    assert first["status"] == "failed"
    assert second["status"] == "delivered"
    assert phases == ["prepare", "prepare", "execute"]
    mark_running.assert_called_once()
    failed_confirmation = confirm.call_args_list[0].kwargs["invocation"]
    assert failed_confirmation["response"]["definitely_no_external_effect"] is True
    assert confirm.call_args_list[0].kwargs["expected_current_status"] == "claimed"
    assert confirm.call_args_list[1].kwargs["expected_current_status"] == "running"


def test_dispatch_project_notification_event_test_mode_claims_suppressed_external_effect():
    from core.application.notifications.dispatch import dispatch_project_notification_event
    from core.infrastructure.runtime.test_mode import reset_runtime_test_mode, set_runtime_test_mode

    mock_bl = MagicMock()
    mock_bl.get_project_run_config.return_value = {"project_id": 7, "project_providers": []}
    mock_bl.get_project_connection.return_value = {
        "connection_id": "conn_delivery",
        "account_id": "acc_alpha",
        "project_id": "7",
        "extension_id": "example.host-fixture",
        "extension_install_id": "install-test-delivery",
        "connection_config": {},
        "secret_refs": [],
        "enabled": True,
    }
    mock_bl.list_notification_routes.return_value = [
        {
            "route_id": "11111111-1111-1111-1111-111111111111",
            "route_key": "delivery_failures",
            "provider_key": "example.host-fixture",
            "implementation_kind": "extension",
            "event_type": "job.failed",
            "severity": "error",
            "route_config": {"connection_id": "conn_delivery"},
        }
    ]
    mock_bl.get_extension.return_value = {
        "extension_key": "example.host-fixture",
        "enabled": True,
        "kind": "notification_provider",
        "bundle_root": "/tmp",
        "entrypoint": {"command": ["python3", "main.py"]},
        "compiled_manifest": {
            "external_effects": [
                {
                    "operation_id": "deliver_notification",
                    "effect_kind": "message_delivery",
                    "channel": "notification",
                    "idempotency": "required",
                    "test_mode_behavior": "suppress",
                }
            ],
            "operation_manifest": [
                {"operation_id": "deliver_notification", "outputs": [{"name": "delivered"}]}
            ],
        },
    }
    mock_bl.get_extension_by_install_id.return_value = {
        **mock_bl.get_extension.return_value,
        "install_id": "install-test-delivery",
    }

    token = set_runtime_test_mode(True)
    try:
        with (
            patch(
                "core.application.extensions.external_effects.cleanup_stale_external_effects",
                return_value={},
            ),
            patch(
                "core.application.extensions.external_effects.claim_external_effect",
                return_value={
                    "effect": {"effect_id": "eff_suppressed_1", "status": "suppressed"},
                    "should_spawn": False,
                    "suppressed": True,
                    "deduped": False,
                },
            ) as claim,
            patch("core.application.notifications.delivery.run_extension_invocation") as invoke,
        ):
            deliveries = dispatch_project_notification_event(
                mock_bl,
                project_id=7,
                event_type="job.failed",
                severity="error",
                job_id=56,
                payload={"summary": "failed"},
                account_id="acc_alpha",
            )
    finally:
        reset_runtime_test_mode(token)

    assert len(deliveries) == 1
    assert deliveries[0]["status"] == "skipped"
    assert deliveries[0]["external_effect_id"] == "eff_suppressed_1"
    claim.assert_called_once()
    assert claim.call_args.kwargs["test_mode"] is True
    invoke.assert_not_called()
    assert (
        mock_bl.log_notification_delivery.call_args.kwargs["external_effect_id"]
        == "eff_suppressed_1"
    )


def test_dispatch_project_notification_event_skips_in_runtime_test_mode():
    from core.application.notifications.dispatch import dispatch_project_notification_event
    from core.infrastructure.runtime.test_mode import reset_runtime_test_mode, set_runtime_test_mode

    mock_bl = MagicMock()
    mock_bl.list_notification_routes.return_value = [
        {
            "route_id": 9,
            "route_key": "failed",
            "provider_key": "fixture_notifier",
            "implementation_kind": "builtin",
            "event_type": "job.failed",
            "severity": "error",
            "route_config": {},
        }
    ]

    token = set_runtime_test_mode(True)
    try:
        deliveries = dispatch_project_notification_event(
            mock_bl,
            project_id=7,
            event_type="job.failed",
            severity="error",
            job_id=55,
            payload={"summary": "failed"},
        )
    finally:
        reset_runtime_test_mode(token)

    assert len(deliveries) == 1
    assert deliveries[0]["status"] == "skipped"
    assert deliveries[0]["error_text"] == "test_mode_suppressed"
    mock_bl.list_notification_routes.assert_called_once()


def test_dispatch_project_notification_event_skips_when_job_meta_marks_test_mode():
    from core.application.notifications.dispatch import dispatch_project_notification_event

    mock_bl = MagicMock()
    mock_bl.get_job.return_value = {"id": 55, "meta": {"test_mode": True}}
    mock_bl.list_notification_routes.return_value = [
        {
            "route_id": 9,
            "route_key": "failed",
            "provider_key": "fixture_notifier",
            "implementation_kind": "builtin",
            "event_type": "job.failed",
            "severity": "error",
            "route_config": {},
        }
    ]

    deliveries = dispatch_project_notification_event(
        mock_bl,
        project_id=7,
        event_type="job.failed",
        severity="error",
        job_id=55,
        payload={"summary": "failed"},
    )

    assert len(deliveries) == 1
    assert deliveries[0]["status"] == "skipped"
    assert deliveries[0]["error_text"] == "test_mode_suppressed"
    mock_bl.get_job.assert_called_once_with("55")
    mock_bl.list_notification_routes.assert_called_once()


def test_dispatch_project_notification_event_blocks_cross_account_route_delivery():
    from core.application.notifications.dispatch import dispatch_project_notification_event

    mock_bl = MagicMock()
    mock_bl.list_notification_routes.return_value = [
        {
            "route_id": 9,
            "route_key": "failed",
            "account_id": "acc_beta",
            "provider_key": "fixture_notifier",
            "implementation_kind": "extension",
            "event_type": "job.failed",
            "severity": "error",
            "route_config": {},
        }
    ]

    with patch("core.application.notifications.delivery.run_extension_invocation") as invoke:
        deliveries = dispatch_project_notification_event(
            mock_bl,
            project_id=7,
            event_type="job.failed",
            severity="error",
            job_id=55,
            payload={"summary": "failed"},
            account_id="acc_alpha",
            correlation_id="job:55:job.failed",
        )

    invoke.assert_not_called()
    assert deliveries == []


def test_dispatch_project_notification_event_allows_same_account_route_delivery():
    from core.application.notifications.dispatch import dispatch_project_notification_event

    mock_bl = MagicMock()
    mock_bl.list_notification_routes.return_value = [
        {
            "route_id": 9,
            "route_key": "failed",
            "account_id": "acc_alpha",
            "provider_key": "fixture_notifier",
            "provider_config": {"extension_key": "fixture.notification-provider"},
            "implementation_kind": "builtin",
            "event_type": "job.failed",
            "severity": "error",
            "route_config": {},
        }
    ]
    mock_bl.get_extension.return_value = {
        "extension_key": "fixture.notification-provider",
        "enabled": True,
        "kind": "notification_provider",
        "bundle_root": "/tmp",
        "entrypoint": {"command": ["python3", "main.py"]},
    }
    mock_bl.get_project_run_config.return_value = {"project_id": 7, "project_providers": []}

    with (
        _bind_connectionless_notification_install(mock_bl, account_id="acc_alpha"),
        patch(
            "core.application.notifications.delivery.run_extension_invocation",
            return_value={
                "status": "succeeded",
                "response": {"ok": True, "delivered": True},
            },
        ) as invoke,
    ):
        deliveries = dispatch_project_notification_event(
            mock_bl,
            project_id=7,
            event_type="job.failed",
            severity="error",
            job_id=55,
            payload={"summary": "failed"},
            account_id="acc_alpha",
            correlation_id="job:55:job.failed",
        )

    invoke.assert_called_once()
    assert len(deliveries) == 1
    assert deliveries[0]["status"] == "delivered"


def test_dispatch_project_notification_event_records_domain_event_before_dispatch_tracking():
    from core.application.notifications.dispatch import dispatch_project_notification_event

    mock_bl = MagicMock()
    mock_bl.list_notification_routes.return_value = [
        {
            "route_id": 9,
            "route_key": "failed",
            "account_id": "acc_alpha",
            "provider_key": "fixture_notifier",
            "provider_config": {"extension_key": "fixture.notification-provider"},
            "implementation_kind": "builtin",
            "event_type": "job.failed",
            "severity": "error",
            "route_config": {},
        }
    ]
    mock_bl.get_extension.return_value = {
        "extension_key": "fixture.notification-provider",
        "enabled": True,
        "kind": "notification_provider",
        "bundle_root": "/tmp",
        "entrypoint": {"command": ["python3", "main.py"]},
    }
    mock_bl.get_project_run_config.return_value = {"project_id": 7, "project_providers": []}

    with (
        _bind_connectionless_notification_install(mock_bl, account_id="acc_alpha"),
        patch(
            "core.application.notifications.delivery.run_extension_invocation",
            return_value={
                "status": "succeeded",
                "response": {"ok": True, "delivered": True},
            },
        ),
    ):
        dispatch_project_notification_event(
            mock_bl,
            project_id=7,
            event_type="job.failed",
            severity="error",
            job_id=55,
            payload={"summary": "failed"},
            account_id="acc_alpha",
            correlation_id="job:55:job.failed",
        )

    record_event = mock_bl.record_domain_event.call_args.kwargs
    record_dispatch = mock_bl.record_event_dispatch.call_args.kwargs
    method_names = [call[0] for call in mock_bl.method_calls]
    assert record_event["event_id"] == record_dispatch["event_id"]
    assert method_names.index("record_domain_event") < method_names.index("record_event_dispatch")


def test_dispatch_project_notification_event_disables_tracking_when_ledger_write_fails():
    from core.application.notifications.dispatch import dispatch_project_notification_event

    mock_bl = MagicMock()
    mock_bl.list_notification_routes.return_value = [
        {
            "route_id": 9,
            "route_key": "failed",
            "account_id": "acc_alpha",
            "provider_key": "fixture_notifier",
            "provider_config": {"extension_key": "fixture.notification-provider"},
            "implementation_kind": "builtin",
            "event_type": "job.failed",
            "severity": "error",
            "route_config": {},
        }
    ]
    mock_bl.record_domain_event.side_effect = RuntimeError("ledger unavailable")
    mock_bl.get_extension.return_value = {
        "extension_key": "fixture.notification-provider",
        "enabled": True,
        "kind": "notification_provider",
        "bundle_root": "/tmp",
        "entrypoint": {"command": ["python3", "main.py"]},
    }
    mock_bl.get_project_run_config.return_value = {"project_id": 7, "project_providers": []}

    with (
        _bind_connectionless_notification_install(mock_bl, account_id="acc_alpha"),
        patch(
            "core.application.notifications.delivery.run_extension_invocation",
            return_value={
                "status": "succeeded",
                "response": {"ok": True, "delivered": True},
            },
        ),
    ):
        deliveries = dispatch_project_notification_event(
            mock_bl,
            project_id=7,
            event_type="job.failed",
            severity="error",
            job_id=55,
            payload={"summary": "failed"},
            account_id="acc_alpha",
            correlation_id="job:55:job.failed",
        )

    assert len(deliveries) == 1
    mock_bl.record_domain_event.assert_called_once()
    mock_bl.record_event_dispatch.assert_not_called()
