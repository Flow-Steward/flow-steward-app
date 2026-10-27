from __future__ import annotations

import json
import socket
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from core.application.lifecycle.configuration_service import ConfigurationService
from core.application.lifecycle.contracts import (
    CancellationToken,
    ComposeTarget,
    DeploymentConfig,
    LifecycleOperationJournal,
    LifecycleOperationStatus,
)
from core.application.lifecycle.errors import LifecycleError
from core.application.lifecycle.preservation_service import PreservationComparison
from core.application.lifecycle.stack_service import (
    StackContainerRollbackState,
    StackGatewayError,
    StackLogsRequest,
    StackRebuildRequest,
    StackRestartRequest,
    StackRollbackSnapshot,
    StackService,
    StackStartRequest,
    StackStatusRequest,
    StackStopRequest,
)
from core.infrastructure.lifecycle.configuration_gateway import ConfigurationLifecycleGateway
from core.infrastructure.lifecycle.docker_gateway import DockerLifecycleGateway
from core.infrastructure.lifecycle.state_store import LifecycleStateStore


class FakeConfigurationStore:
    def __init__(self) -> None:
        self.config = DeploymentConfig()
        self.pending_path = Path("pending")
        self.write_calls: list[str] = []

    def read_active(self) -> DeploymentConfig:
        return self.config

    def project_name(self, _config: DeploymentConfig) -> str:
        return "installed-project"


class FakeDockerGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.config_services = (
            "fs-app",
            "fs-worker",
            "pg-fs",
            "seaweedfs",
            "mailpit",
            "memory-postgres-extensions",
            "fs-frontend",
        )
        self.runnable = {"fs-app", "fs-worker", "pg-fs"}
        self.health_failure = False
        self.restore_success = True
        self.crash_action: str | None = None
        self.crashed = False
        self.rollback_inventories: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        self.restore_requests: list[tuple[str, StackRollbackSnapshot]] = []
        self.released_pins: list[str] = []
        self.restore_mismatch_report: tuple[str, ...] = ()
        self.release_failures: tuple[str, ...] = ()
        self.image_ids = {
            service: f"sha256:{index:064x}"
            for index, service in enumerate(self.config_services, start=1)
        }
        self.images_preserved = True
        self.image_check_error = False
        self.drifted_service = "fs-app"
        self.drifted_image_id = f"sha256:{99:064x}"
        self.prior_image_available = True
        self.configured_image_ids: dict[str, str] | None = None
        self.configured_image_error = False

    def services(self, _target: ComposeTarget) -> tuple[str, ...]:
        self.calls.append(("services", ()))
        return self.config_services

    def status(self, target: ComposeTarget):
        from core.application.lifecycle.stack_service import (
            StackContainerState,
            StackRuntimeState,
            StackVolumeState,
        )

        services = tuple(
            StackContainerState(
                service=name,
                container_id=f"id-{name}",
                container_name=f"{target.project_name}-{name}-1",
                state="running" if name in self.runnable else "exited",
                health=(
                    "unhealthy"
                    if self.health_failure
                    and name == "fs-app"
                    and any(call[0] in {"start", "restart", "rebuild"} for call in self.calls)
                    else "healthy"
                    if name in self.runnable
                    else ""
                ),
                exit_code=0,
            )
            for name in target.required_services
        )
        return StackRuntimeState(
            project_name=target.project_name,
            services=services,
            volumes=(
                StackVolumeState(
                    name=f"{target.project_name}_postgres",
                    driver="local",
                    scope="local",
                ),
            ),
        )

    def capture_rollback(
        self,
        target: ComposeTarget,
        *,
        operation_id: str,
        selected_services: tuple[str, ...],
        managed_services: tuple[str, ...],
    ) -> StackRollbackSnapshot:
        self.calls.append(("capture_rollback", selected_services))
        self.rollback_inventories.append((selected_services, managed_services))
        return StackRollbackSnapshot(
            operation_id=operation_id,
            project_name=target.project_name,
            selected_services=selected_services,
            services=tuple(
                StackContainerRollbackState(
                    service=service,
                    topology="running" if service in self.runnable else "stopped",
                    container_id=f"id-{service}",
                    image_id=self.image_ids[service],
                )
                for service in managed_services
            ),
        )

    def runtime_images_match(
        self,
        target: ComposeTarget,
        snapshot: StackRollbackSnapshot,
    ) -> bool:
        self.calls.append(("runtime_images_match", ()))
        return not self.runtime_image_drift(target, snapshot)

    def runtime_image_drift(
        self,
        _target: ComposeTarget,
        snapshot: StackRollbackSnapshot,
    ):
        from core.application.lifecycle.stack_service import StackImageTransition

        self.calls.append(("runtime_image_drift", ()))
        if self.image_check_error:
            raise StackGatewayError(
                action="inspect",
                detail="The runtime image identity could not be inspected.",
            )
        if self.images_preserved:
            return ()
        expected = next(
            (item.image_id for item in snapshot.services if item.service == self.drifted_service),
            self.image_ids.get(self.drifted_service, f"sha256:{1:064x}"),
        )
        return (
            StackImageTransition(
                service=self.drifted_service,
                expected_image_id=expected,
                observed_image_id=self.drifted_image_id,
                expected_image_available=self.prior_image_available,
            ),
        )

    def configured_image_identities(self, _target: ComposeTarget) -> dict[str, str]:
        self.calls.append(("configured_image_identities", ()))
        if self.configured_image_error:
            raise StackGatewayError(
                action="configured_images",
                detail="The configured image pins could not be resolved.",
            )
        if self.configured_image_ids is not None:
            return dict(self.configured_image_ids)
        return {self.drifted_service: self.drifted_image_id}

    def start(
        self,
        _target: ComposeTarget,
        services: tuple[str, ...],
        **_kwargs: object,
    ) -> None:
        self.calls.append(("start", services))
        self.runnable.update(services)

    def stop(
        self,
        _target: ComposeTarget,
        services: tuple[str, ...],
        **_kwargs: object,
    ) -> None:
        self.calls.append(("stop", services))
        self.runnable.difference_update(services)

    def restart(
        self,
        _target: ComposeTarget,
        services: tuple[str, ...],
        **_kwargs: object,
    ) -> None:
        self.calls.append(("restart", services))
        self.runnable.update(services)

    def rebuild(
        self,
        _target: ComposeTarget,
        services: tuple[str, ...],
        **_kwargs: object,
    ) -> None:
        self.calls.append(("rebuild", services))
        self.runnable.update(services)
        if self.crash_action == "rebuild" and not self.crashed:
            self.crashed = True
            raise SystemExit("simulated process crash")

    def restore(
        self,
        _target: ComposeTarget,
        *,
        action: str,
        snapshot: StackRollbackSnapshot,
        **_kwargs: object,
    ) -> bool:
        previously_runnable = tuple(
            item.service for item in snapshot.services if item.topology == "running"
        )
        self.calls.append(("restore", previously_runnable))
        self.restore_requests.append((action, snapshot))
        if self.restore_success:
            self.runnable = set(previously_runnable)
            # An exact restore puts the recorded images back, so drift is gone.
            self.images_preserved = True
        return self.restore_success

    def logs(
        self,
        _target: ComposeTarget,
        *,
        service: str | None,
        tail: int,
        follow: bool,
        output_sink: Callable[[str], None],
    ) -> int:
        self.calls.append(("logs", ((service or "*"), str(tail), str(follow))))
        output_sink("token=must-not-appear")
        return 1

    def restore_mismatches(
        self,
        _target: ComposeTarget,
        _snapshot: StackRollbackSnapshot,
    ) -> tuple[str, ...]:
        self.calls.append(("restore_mismatches", ()))
        return self.restore_mismatch_report

    def release_rollback_pins(self, snapshot: StackRollbackSnapshot) -> tuple[str, ...]:
        self.calls.append(("release_rollback_pins", ()))
        self.released_pins.append(snapshot.operation_id)
        return self.release_failures

    def manual_recovery_command(
        self,
        action: str,
    ) -> str:
        return f"./flow-steward stack {action} --confirm"

    def storage_connectivity(self, _target: ComposeTarget) -> bool:
        self.calls.append(("storage_connectivity", ()))
        return True


class FakeReadinessService:
    def __init__(self) -> None:
        self.ready = True
        self.ready_after_calls: int | None = None
        self.crash_once = False
        self.calls = 0
        self.failed_ids: tuple[str, ...] = ()

    def check(self) -> SimpleNamespace:
        self.calls += 1
        if self.crash_once:
            self.crash_once = False
            raise SystemExit("simulated post-effect crash")
        ready = self.ready or (
            self.ready_after_calls is not None and self.calls >= self.ready_after_calls
        )
        return SimpleNamespace(
            status="ready" if ready else "blocked",
            core_failed=0 if ready else 1,
            findings=tuple(
                SimpleNamespace(check_id=check_id, area="core", status="failed")
                for check_id in self.failed_ids
            ),
        )


class FakePreservationService:
    def __init__(self) -> None:
        self.captures: list[str] = []
        self.fail_after = False
        self.comparison = PreservationComparison(success=True, compared_fields=("compose_project",))

    def capture(self, **kwargs: object):
        self.captures.append(str(kwargs["stage"]))
        if self.fail_after and kwargs["stage"] == "after":
            raise ValueError("after manifest unavailable")
        return SimpleNamespace(
            operation_id=kwargs["operation_id"],
            stage=kwargs["stage"],
            database_reachable=True,
            fields={"compose_project": kwargs["compose_project"]},
        )

    def compare(self, _before: object, _after: object, **_kwargs: object):
        return self.comparison


class FakeManifestStore:
    def __init__(self) -> None:
        self.saved: list[object] = []

    def save(self, manifest: object) -> Path:
        self.saved.append(manifest)
        return Path(f"/tmp/{manifest.stage}.json")  # type: ignore[attr-defined]

    def load(self, operation_id: str, stage: str) -> object | None:
        return next(
            (
                manifest
                for manifest in reversed(self.saved)
                if manifest.operation_id == operation_id  # type: ignore[attr-defined]
                and manifest.stage == stage  # type: ignore[attr-defined]
            ),
            None,
        )


def _target() -> ComposeTarget:
    return ComposeTarget(
        stack_environment="development",
        file_storage="bundled",
        env_file=".env",
        compose_files=(
            "infra/docker/docker-compose.yaml",
            ".flow-steward/installer.compose.override.yaml",
        ),
        compose_profiles=("storage-seaweedfs",),
        required_services=("fs-app", "fs-worker", "pg-fs", "seaweedfs"),
        runtime_services=("fs-app", "fs-worker", "pg-fs", "seaweedfs"),
        rebuild_services=("fs-app", "fs-worker"),
        project_name="installed-project",
    )


def _service(
    tmp_path: Path,
    *,
    docker: FakeDockerGateway | None = None,
    preservation: FakePreservationService | None = None,
    readiness: FakeReadinessService | None = None,
    readiness_reconciler: Callable[[float], None] | None = None,
) -> tuple[StackService, FakeDockerGateway, FakePreservationService, FakeConfigurationStore]:
    docker = docker or FakeDockerGateway()
    preservation = preservation or FakePreservationService()
    readiness = readiness or FakeReadinessService()
    store = FakeConfigurationStore()
    configuration = ConfigurationService(store=store)
    configuration.derive_target = lambda _config: _target()  # type: ignore[method-assign]
    service = StackService(
        configuration_service=configuration,
        docker_gateway=docker,
        lifecycle_state=LifecycleStateStore(tmp_path),
        preservation_service=preservation,
        manifest_store=FakeManifestStore(),
        release_provider=lambda: "1.2.3",
        readiness_service=readiness,
        readiness_reconciler=readiness_reconciler,
        storage_connectivity_provider=lambda _config, target: docker.storage_connectivity(target),
        health_timeout_seconds=37,
        readiness_sleep=lambda _seconds: None,
    )
    return service, docker, preservation, store


def test_stack_start_reconciles_maintenance_readiness_before_polling(tmp_path: Path) -> None:
    readiness = FakeReadinessService()
    events: list[str] = []
    budgets: list[float] = []
    original_check = readiness.check

    def check():
        events.append("readiness")
        return original_check()

    def reconcile(timeout: float) -> None:
        budgets.append(timeout)
        events.append("reconcile")

    readiness.check = check  # type: ignore[method-assign]
    service, _docker, _preservation, _store = _service(
        tmp_path,
        readiness=readiness,
        readiness_reconciler=reconcile,
    )

    service.start(StackStartRequest())

    assert events[:2] == ["reconcile", "readiness"]
    assert budgets == [37]


def test_stack_start_retains_primary_readiness_ids_after_successful_rollback(
    tmp_path: Path,
) -> None:
    readiness = FakeReadinessService()
    readiness.ready = False
    readiness.ready_after_calls = 38
    readiness.failed_ids = ("security_posture", "stuck_jobs")
    service, _docker, _preservation, _store = _service(tmp_path, readiness=readiness)

    request = StackStartRequest()

    with pytest.raises(LifecycleError) as raised:
        service.start(request)

    assert raised.value.code == "stack_start_failed"
    assert raised.value.details["failed_readiness_ids"] == ["security_posture", "stuck_jobs"]
    assert "security_posture, stuck_jobs" in raised.value.cause
    assert raised.value.log_path is not None
    assert "security_posture, stuck_jobs" in Path(raised.value.log_path).read_text(encoding="utf-8")


def test_status_returns_detailed_container_health_and_volume_state_without_journal(
    tmp_path: Path,
) -> None:
    service, _docker, _preservation, _store = _service(tmp_path)

    result = service.status(StackStatusRequest())

    assert result.action == "status"
    assert result.project_name == "installed-project"
    assert result.services[0].container_id == "id-fs-app"
    assert result.services[0].health == "healthy"
    assert result.volumes[0].name == "installed-project_postgres"
    assert not (tmp_path / ".flow-steward/operations").exists()


def test_status_returns_precondition_exit_when_a_required_runtime_service_is_stopped(
    tmp_path: Path,
) -> None:
    service, docker, _preservation, _store = _service(tmp_path)
    docker.runnable.remove("fs-worker")

    result = service.status(StackStatusRequest())

    assert result.status == "degraded"
    assert result.process_exit_code == 4


def test_log_choices_are_the_full_selected_compose_config_catalog(
    tmp_path: Path,
) -> None:
    service, docker, _preservation, _store = _service(tmp_path)

    assert service.log_service_choices() == (
        "fs-app",
        "fs-worker",
        "pg-fs",
        "seaweedfs",
        "mailpit",
        "memory-postgres-extensions",
        "fs-frontend",
    )
    assert docker.calls == [("services", ())]


def test_logs_validate_dynamic_service_and_stream_only_through_technical_sink(
    tmp_path: Path,
) -> None:
    service, docker, _preservation, _store = _service(tmp_path)
    technical: list[str] = []

    result = service.logs(
        StackLogsRequest(service="fs-app", tail=17, follow=True),
        technical_sink=technical.append,
    )

    assert result.service == "fs-app"
    assert result.tail == 17
    assert result.follow is True
    assert result.lines_streamed == 1
    assert result.lines == ("token=[REDACTED]",)
    assert result.stdout_full == ""
    assert docker.calls[-1] == ("logs", ("fs-app", "17", "True"))
    assert technical == ["token=[REDACTED]"]

    _raises_input_408_1 = StackLogsRequest(service="unknown-service")
    with pytest.raises(LifecycleError) as raised:
        service.logs(_raises_input_408_1)
    assert raised.value.code == "stack_service_unavailable"
    assert raised.value.exit_code == 3


def test_logs_counts_only_lines_received_from_the_gateway_sink(tmp_path: Path) -> None:
    service, docker, _preservation, _store = _service(tmp_path)
    docker.logs = lambda *_args, **_kwargs: 5

    result = service.logs(StackLogsRequest(service="fs-app", tail=5, follow=False))

    assert result.lines == ()
    assert result.lines_streamed == 0


@pytest.mark.parametrize(
    ("method_name", "request_type", "expected_action", "expected_services"),
    (
        ("start", StackStartRequest, "start", ("fs-app", "fs-worker", "pg-fs", "seaweedfs")),
        ("stop", StackStopRequest, "stop", ("fs-app", "fs-worker", "pg-fs", "seaweedfs")),
        (
            "restart",
            StackRestartRequest,
            "restart",
            ("fs-app", "fs-worker", "pg-fs", "seaweedfs"),
        ),
        ("rebuild", StackRebuildRequest, "rebuild", ("fs-app", "fs-worker")),
    ),
)
def test_mutations_use_shared_journal_preservation_and_application_selected_targets(
    tmp_path: Path,
    method_name: str,
    request_type: type[object],
    expected_action: str,
    expected_services: tuple[str, ...],
) -> None:
    service, docker, preservation, store = _service(tmp_path)
    docker.config_services = (*docker.config_services, "fs-cli")

    result = getattr(service, method_name)(request_type())

    assert result.action == expected_action
    assert result.target_services == expected_services
    assert result.operation_id
    assert Path(result.journal_path).is_file()
    assert preservation.captures == ["before", "after"]
    assert store.write_calls == []
    assert (expected_action, expected_services) in docker.calls
    assert "fs-cli" not in result.target_services
    assert all("fs-cli" not in services for _action, services in docker.calls)
    assert all(
        "fs-cli" not in selected and "fs-cli" not in managed
        for selected, managed in docker.rollback_inventories
    )

    journal = json.loads(Path(result.journal_path).read_text(encoding="utf-8"))
    assert journal["status"] == "completed"
    assert journal["parameters"]["project_name"] == "installed-project"
    assert journal["parameters"]["environment"] == "development"
    assert journal["parameters"]["release"] == "1.2.3"
    rollback = json.loads(
        (
            tmp_path / ".flow-steward/operations" / f"{result.operation_id}.previous-config.json"
        ).read_text(encoding="utf-8")
    )
    assert rollback["operation_id"] == result.operation_id
    assert rollback["project_name"] == "installed-project"
    assert {item["topology"] for item in rollback["services"]} <= {"running", "stopped"}
    assert all(item["image_id"].startswith("sha256:") for item in rollback["services"])


def test_stack_mutation_never_reconciles_the_retained_cli_runtime(tmp_path: Path) -> None:
    service, docker, _preservation, _store = _service(tmp_path)
    target = _target().model_copy(
        update={
            "required_services": (*_target().required_services, "fs-cli"),
            "runtime_services": (*_target().runtime_services, "fs-cli"),
        }
    )
    service.configuration_service.derive_target = lambda _config: target  # type: ignore[method-assign]
    docker.config_services = (*docker.config_services, "fs-cli")

    result = service.start(StackStartRequest())

    assert "fs-cli" not in result.target_services
    assert all("fs-cli" not in services for _action, services in docker.calls)
    assert all(
        "fs-cli" not in selected and "fs-cli" not in managed
        for selected, managed in docker.rollback_inventories
    )


def test_stack_start_rejects_changed_runtime_image_identity(tmp_path: Path) -> None:
    docker = FakeDockerGateway()
    docker.images_preserved = False
    service, _docker, _preservation, _store = _service(tmp_path, docker=docker)

    _raises_input_506_1 = StackStartRequest()
    with pytest.raises(LifecycleError) as raised:
        service.start(_raises_input_506_1)

    assert raised.value.code == "stack_start_failed"
    assert ("runtime_image_drift", ()) in docker.calls
    assert any(call[0] == "restore" for call in docker.calls)
    assert "was sha256:" in raised.value.cause
    assert "--adopt-image-pins" in raised.value.cause


def test_stack_start_recovers_when_runtime_image_identity_cannot_be_read(
    tmp_path: Path,
) -> None:
    docker = FakeDockerGateway()
    docker.image_check_error = True
    service, _docker, _preservation, _store = _service(tmp_path, docker=docker)

    _raises_input_521_1 = StackStartRequest()
    with pytest.raises(LifecycleError) as raised:
        service.start(_raises_input_521_1)

    assert raised.value.code == "stack_start_failed"
    assert ("runtime_image_drift", ()) in docker.calls
    assert any(call[0] == "restore" for call in docker.calls)


def test_development_test_runner_is_in_selected_and_managed_rebuild_snapshot(
    tmp_path: Path,
) -> None:
    service, docker, _preservation, store = _service(tmp_path)
    store.config = DeploymentConfig(development_tools=("test_runner",))
    service.configuration_service = ConfigurationService(store=store)
    target = service.configuration_service.derive_target(store.config)
    docker.config_services = target.required_services
    docker.runnable = set(target.runtime_services)
    docker.image_ids = {
        name: f"sha256:{index:064x}" for index, name in enumerate(target.required_services, start=1)
    }

    result = service.rebuild(StackRebuildRequest())

    assert "fs-test-runner" in result.target_services
    selected, managed = docker.rollback_inventories[0]
    assert "fs-test-runner" in selected
    assert set(selected).issubset(managed)
    rollback_path = (
        tmp_path / ".flow-steward/operations" / f"{result.operation_id}.previous-config.json"
    )
    rollback = json.loads(rollback_path.read_text(encoding="utf-8"))
    assert "fs-test-runner" in {item["service"] for item in rollback["services"]}


def test_running_postcondition_requires_core_readiness_and_real_storage_probe(
    tmp_path: Path,
) -> None:
    readiness = FakeReadinessService()
    readiness.ready = False
    docker = FakeDockerGateway()
    service, _docker, _preservation, _store = _service(
        tmp_path,
        docker=docker,
        readiness=readiness,
    )

    _raises_input_567_1 = StackRestartRequest()
    with pytest.raises(LifecycleError) as raised:
        service.restart(_raises_input_567_1)

    assert raised.value.code == "stack_restart_recovery_required"
    assert raised.value.exit_code == 9
    assert readiness.calls >= 1
    assert ("storage_connectivity", ()) in docker.calls
    assert any(call[0] == "restore" for call in docker.calls)


def test_restart_rejects_incomplete_preservation_before_runtime_mutation(
    tmp_path: Path,
) -> None:
    preservation = FakePreservationService()
    preservation.comparison = PreservationComparison(
        success=False,
        compared_fields=("indexed_branch_sha_markers",),
    )
    service, docker, _preservation, _store = _service(
        tmp_path,
        preservation=preservation,
    )

    _raises_input_590_1 = StackRestartRequest()
    with pytest.raises(LifecycleError) as raised:
        service.restart(_raises_input_590_1)

    assert raised.value.code == "stack_preservation_capture_failed"
    assert not any(call[0] == "restart" for call in docker.calls)
    assert not any(call[0] == "restore" for call in docker.calls)


def test_failed_health_restores_prior_runnable_runtime_and_returns_stable_failure(
    tmp_path: Path,
) -> None:
    docker = FakeDockerGateway()
    docker.runnable = {"fs-app", "pg-fs"}
    docker.health_failure = True
    service, _docker, _preservation, _store = _service(tmp_path, docker=docker)

    _raises_input_606_1 = StackRestartRequest()
    with pytest.raises(LifecycleError) as raised:
        service.restart(_raises_input_606_1)

    assert raised.value.code == "stack_restart_failed"
    assert raised.value.exit_code == 8
    assert ("restore", ("fs-app", "pg-fs")) in docker.calls


def test_recovery_waits_for_delayed_application_readiness(tmp_path: Path) -> None:
    docker = FakeDockerGateway()
    docker.health_failure = True
    readiness = FakeReadinessService()
    readiness.ready = False
    readiness.ready_after_calls = 3
    service, _docker, _preservation, _store = _service(
        tmp_path,
        docker=docker,
        readiness=readiness,
    )

    _raises_input_626_1 = StackRestartRequest()
    with pytest.raises(LifecycleError) as raised:
        service.restart(_raises_input_626_1)

    assert raised.value.code == "stack_restart_failed"
    assert readiness.calls == 3
    assert any(call[0] == "restore" for call in docker.calls)


def test_failed_health_without_verified_restore_requires_exact_manual_action(
    tmp_path: Path,
) -> None:
    docker = FakeDockerGateway()
    docker.health_failure = True
    docker.restore_success = False
    service, _docker, _preservation, _store = _service(tmp_path, docker=docker)

    _raises_input_642_1 = StackRebuildRequest()
    with pytest.raises(LifecycleError) as raised:
        service.rebuild(_raises_input_642_1)

    assert raised.value.code == "stack_rebuild_recovery_required"
    assert raised.value.exit_code == 9
    assert raised.value.recovery_command == "./flow-steward stack rebuild --confirm"
    assert raised.value.recovery_command in raised.value.remediation


def test_exact_host_recovery_command_restores_saved_operation_without_new_mutation(
    tmp_path: Path,
) -> None:
    docker = FakeDockerGateway()
    docker.health_failure = True
    docker.restore_success = False
    service, _docker, _preservation, _store = _service(tmp_path, docker=docker)

    _raises_input_659_1 = StackRebuildRequest()
    with pytest.raises(LifecycleError) as raised:
        service.rebuild(_raises_input_659_1)

    operation_id = raised.value.operation_id
    assert operation_id
    docker.health_failure = False
    docker.restore_success = True
    rebuild_calls = [call for call in docker.calls if call[0] == "rebuild"]

    result = service.rebuild(StackRebuildRequest())

    assert result.operation_id == operation_id
    assert result.status == "recovered"
    assert result.runtime_restored is True
    assert [call for call in docker.calls if call[0] == "rebuild"] == rebuild_calls
    journal = service.lifecycle_state.load_journal(operation_id)
    assert journal is not None
    assert journal.status is LifecycleOperationStatus.FAILED
    assert journal.retryable is False
    assert journal.last_error_code == "stack_recovery_completed"


def test_failed_postvalidation_requires_recovery_when_restored_preservation_is_unproven(
    tmp_path: Path,
) -> None:
    preservation = FakePreservationService()
    preservation.fail_after = True
    service, docker, _preservation, _store = _service(
        tmp_path,
        preservation=preservation,
    )

    _raises_input_691_1 = StackRestartRequest()
    with pytest.raises(LifecycleError) as raised:
        service.restart(_raises_input_691_1)

    assert raised.value.code == "stack_restart_recovery_required"
    assert raised.value.exit_code == 9
    assert ("restore", ("fs-app", "fs-worker", "pg-fs")) in docker.calls


def test_crash_retry_resumes_same_journal_without_recapturing_partial_before_state(
    tmp_path: Path,
) -> None:
    docker = FakeDockerGateway()
    docker.crash_action = "rebuild"
    service, _docker, preservation, _store = _service(tmp_path, docker=docker)

    _raises_input_706_1 = StackRebuildRequest()
    with pytest.raises(SystemExit, match="simulated process crash"):
        service.rebuild(_raises_input_706_1)

    state = service.lifecycle_state
    unfinished = state.unfinished_journals()
    assert len(unfinished) == 1
    assert unfinished[0].status is LifecycleOperationStatus.RUNNING
    operation_id = unfinished[0].operation_id

    result = service.rebuild(StackRebuildRequest())

    assert result.operation_id == operation_id
    assert preservation.captures == ["before", "after"]
    assert [call[0] for call in docker.calls].count("capture_rollback") == 1


@pytest.mark.parametrize(
    ("action", "request_type"),
    (
        ("rebuild", StackRebuildRequest),
        ("restart", StackRestartRequest),
    ),
)
def test_post_effect_crash_resume_never_repeats_completed_apply_action(
    tmp_path: Path,
    action: str,
    request_type: type[StackRebuildRequest] | type[StackRestartRequest],
) -> None:
    docker = FakeDockerGateway()
    readiness = FakeReadinessService()
    readiness.crash_once = True
    service, _docker, _preservation, _store = _service(
        tmp_path,
        docker=docker,
        readiness=readiness,
    )
    invoke = getattr(service, action)

    _raises_input_744_1 = request_type()
    with pytest.raises(SystemExit, match="simulated post-effect crash"):
        invoke(_raises_input_744_1)

    unfinished = service.lifecycle_state.unfinished_journals()
    assert len(unfinished) == 1
    operation_id = unfinished[0].operation_id
    assert "apply_action" in unfinished[0].completed_steps
    assert [call[0] for call in docker.calls].count(action) == 1

    result = invoke(request_type())

    assert result.operation_id == operation_id
    assert [call[0] for call in docker.calls].count(action) == 1


def test_cancelled_operation_retries_with_same_operation_id(
    tmp_path: Path,
) -> None:
    service, _docker, _preservation, _store = _service(tmp_path)
    cancellation = CancellationToken()
    cancellation.request()

    _raises_input_766_1 = StackStartRequest()
    with pytest.raises(LifecycleError) as raised:
        service.start(_raises_input_766_1, cancellation=cancellation)

    assert raised.value.code == "lifecycle_cancelled"
    operation_id = raised.value.operation_id
    result = service.start(StackStartRequest())
    assert result.operation_id == operation_id


def test_ambiguous_unfinished_journals_require_exact_nonpublic_operator_action(
    tmp_path: Path,
) -> None:
    service, _docker, _preservation, _store = _service(tmp_path)
    cancellation = CancellationToken()
    cancellation.request()
    _raises_input_781_1 = StackStartRequest()
    with pytest.raises(LifecycleError) as interrupted:
        service.start(_raises_input_781_1, cancellation=cancellation)
    first_id = interrupted.value.operation_id
    assert first_id
    first = service.lifecycle_state.load_journal(first_id)
    assert first is not None
    second_id = "second-unfinished-operation"
    service.lifecycle_state.save_journal(first.model_copy(update={"operation_id": second_id}))
    paths = sorted(
        (
            f".flow-steward/operations/{first_id}.json",
            f".flow-steward/operations/{second_id}.json",
        )
    )
    expected_action = (
        "operator: reconcile lifecycle journals "
        f"{', '.join(paths)}; retain exactly one unfinished journal"
    )

    _raises_input_800_1 = StackStartRequest()
    with pytest.raises(LifecycleError) as raised:
        service.start(_raises_input_800_1)

    assert raised.value.code == "stack_operation_recovery_required"
    assert raised.value.recovery_command == expected_action
    assert "--operation-id" not in raised.value.remediation
    assert "./flow-steward stack start --confirm" in raised.value.remediation


def test_different_action_cannot_capture_new_state_over_stale_running_operation(
    tmp_path: Path,
) -> None:
    docker = FakeDockerGateway()
    docker.crash_action = "rebuild"
    service, _docker, preservation, _store = _service(tmp_path, docker=docker)

    _raises_input_816_1 = StackRebuildRequest()
    with pytest.raises(SystemExit):
        service.rebuild(_raises_input_816_1)

    _raises_input_819_1 = StackStartRequest()
    with pytest.raises(LifecycleError) as raised:
        service.start(_raises_input_819_1)

    assert raised.value.exit_code == 9
    assert raised.value.code == "stack_operation_recovery_required"
    assert preservation.captures == ["before"]


def test_live_lifecycle_lock_blocks_before_any_new_snapshot_is_captured(tmp_path: Path) -> None:
    service, docker, preservation, _store = _service(tmp_path)

    _raises_input_830_1 = StackStopRequest()
    with (
        service.lifecycle_state.operation_lock("live-operation", mutating=True),
        pytest.raises(LifecycleError) as raised,
    ):
        service.stop(_raises_input_830_1)

    assert raised.value.code == "lifecycle_operation_active"
    assert raised.value.exit_code == 6
    assert not any(call[0] == "capture_rollback" for call in docker.calls)
    assert preservation.captures == []


def test_corrupt_unfinished_journal_blocks_new_snapshot_as_recovery_required(
    tmp_path: Path,
) -> None:
    service, docker, preservation, _store = _service(tmp_path)
    operations = tmp_path / ".flow-steward/operations"
    operations.mkdir(parents=True)
    (operations / "crashed-operation.json").write_text("{not-json", encoding="utf-8")

    _raises_input_850_1 = StackStartRequest()
    with pytest.raises(LifecycleError) as raised:
        service.start(_raises_input_850_1)

    assert raised.value.exit_code == 9
    assert raised.value.code == "stack_operation_recovery_required"
    assert not any(call[0] == "capture_rollback" for call in docker.calls)
    assert preservation.captures == []


def test_stack_mutation_request_rejects_internal_operation_id() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        StackStartRequest(operation_id="crashed-operation")


def test_nonretryable_failed_journal_is_not_automatically_replayed(tmp_path: Path) -> None:
    service, docker, preservation, _store = _service(tmp_path)
    completed = service.start(StackStartRequest())
    journal = service.lifecycle_state.load_journal(completed.operation_id)
    assert journal is not None
    service.lifecycle_state.save_journal(
        journal.model_copy(update={"status": LifecycleOperationStatus.FAILED, "retryable": False})
    )
    prior_captures = len(preservation.captures)
    prior_rollback_calls = sum(call[0] == "capture_rollback" for call in docker.calls)

    result = service.start(StackStartRequest())

    assert result.operation_id != completed.operation_id
    assert len(preservation.captures) == prior_captures + 2
    assert sum(call[0] == "capture_rollback" for call in docker.calls) == prior_rollback_calls + 1


class _Process:
    def __init__(self, responses: list[tuple[int, str]]) -> None:
        self.responses = list(responses)
        self.commands: list[tuple[str, ...]] = []
        self.invocations: list[dict[str, object]] = []

    def run(self, command: list[str], **kwargs: object):
        self.commands.append(tuple(command))
        self.invocations.append(dict(kwargs))
        returncode, stdout = self.responses.pop(0)
        sink = kwargs.get("output_sink")
        if callable(sink) and stdout:
            for line in stdout.splitlines():
                sink(line)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


def test_compact_management_preflight_executes_inside_product_runtime(tmp_path: Path) -> None:
    process = _Process([(0, "candidate database and worker identity preflight passed\n")])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)
    target = _target().model_copy(update={"deployment_profile": "compact"})

    assert gateway.compact_candidate_preflight(target) == (
        "candidate database and worker identity preflight passed\n"
    )
    assert process.commands[0][-6:] == (
        "exec",
        "-T",
        "compact-runtime",
        "python",
        "-m",
        "core.entrypoints.cli.ops.candidate_runtime_preflight",
    )
    assert process.invocations[0]["timeout_seconds"] == 210.0


# W4: every operation that brings services up ends by listing the project's containers,
# names and states, to finish a recreate a kill left half-way.
_NAMES_LISTING = ("--format", "{{.ID}}\t{{.Names}}\t{{.State}}")


def test_docker_configuration_validation_renders_exact_target_without_mutation(
    tmp_path: Path,
) -> None:
    process = _Process([(0, "")])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    environment = {"CANDIDATE_ONLY": "value"}
    override = "services: {}\n"
    gateway.validate_target(_target(), environment=environment, override_content=override)

    command = process.commands[0]
    assert command[-2:] == ("config", "--quiet")
    assert "--env-file" in command
    assert command[command.index("--env-file") + 1] == "/dev/null"
    assert command.count("-f") == 2
    assert "-" in command
    assert "--profile" in command
    assert process.invocations[0]["environment"] == environment
    assert process.invocations[0]["input_text"] == override
    assert not {"up", "stop", "down", "rm"}.intersection(command)


def test_stack_start_never_pulls_or_builds_missing_runtime_images(tmp_path: Path) -> None:
    process = _Process([(0, ""), (0, "")])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    gateway.start(
        _target(),
        ("fs-app", "fs-worker"),
        timeout_seconds=37,
        output_sink=lambda _line: None,
        heartbeat=lambda _detail: None,
    )

    command = process.commands[0]
    assert command[-9:] == (
        "up",
        "--wait",
        "--wait-timeout",
        "37",
        "--no-build",
        "--pull",
        "never",
        "fs-app",
        "fs-worker",
    )


def test_docker_target_image_preparation_uses_candidate_environment_without_mutation(
    tmp_path: Path,
) -> None:
    process = _Process(
        [
            (0, "missing-image:release\n"),
            (1, ""),
            (0, ""),
            (0, ""),
        ]
    )
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)
    target = _target().model_copy(
        update={
            "required_services": (*_target().required_services, "fs-cli"),
        }
    )
    environment = {"CANDIDATE_ONLY": "value"}
    override = "services: {}\n"

    gateway.prepare_target_images(
        target,
        environment=environment,
        override_content=override,
        timeout_seconds=1234,
    )

    inventory, inspection, pull, build = process.commands
    services = tuple(service for service in target.required_services if service != "fs-cli")
    assert pull[-(len(services) + 4) :] == (
        "pull",
        "--ignore-buildable",
        "--policy",
        "missing",
        *services,
    )
    assert build[-(len(services) + 1) :] == ("build", *services)
    assert "fs-cli" not in pull[pull.index("pull") + 1 :]
    assert "fs-cli" not in build[build.index("build") + 1 :]
    assert inventory[-2:] == ("config", "--images")
    assert inspection[-1] == "missing-image:release"
    assert all(
        invocation["environment"] == environment
        and invocation["input_text"] == override
        and invocation["timeout_seconds"] == 1234
        for invocation in process.invocations[-2:]
    )
    assert all(
        command[command.index("--env-file") + 1] == "/dev/null"
        for command in (inventory, pull, build)
    )
    assert not {"up", "stop", "down", "rm"}.intersection((*inventory, *inspection, *pull, *build))


def test_docker_target_image_preparation_supplies_missing_target_env_temporarily(
    tmp_path: Path,
) -> None:
    target = _target().model_copy(
        update={
            "env_file": ".env.candidate",
            "compose_files": ("infra/docker/docker-compose.yaml",),
        }
    )
    observed: list[tuple[Path, str, int]] = []
    candidate_env = tmp_path / ".env.candidate"

    class EnvCheckingProcess(_Process):
        def run(self, command: list[str], **kwargs: object):
            if tuple(command[:2]) == ("docker", "compose"):
                observed.append(
                    (
                        candidate_env,
                        candidate_env.read_text(encoding="utf-8"),
                        candidate_env.stat().st_mode & 0o777,
                    )
                )
            return super().run(command, **kwargs)

    process = EnvCheckingProcess(
        [
            (0, "missing-image:release\n"),
            (1, ""),
            (0, ""),
            (0, ""),
        ]
    )
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    gateway.prepare_target_images(
        target,
        environment={"CANDIDATE_ONLY": "value"},
        timeout_seconds=1234,
    )

    assert observed
    assert all(path == candidate_env for path, _text, _mode in observed)
    assert all(text == "CANDIDATE_ONLY=value\n" for _path, text, _mode in observed)
    assert all(mode == 0o600 for _path, _text, mode in observed)
    assert not candidate_env.exists()


def test_docker_target_image_preparation_reuses_complete_exact_image_set(
    tmp_path: Path,
) -> None:
    images = ("installed-fs-app:release", "traefik@sha256:" + ("a" * 64))
    process = _Process(
        [
            (0, "\n".join(images) + "\n"),
            (0, "sha256:" + ("b" * 64) + "\nsha256:" + ("c" * 64) + "\n"),
        ]
    )
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    gateway.prepare_target_images(
        _target(),
        environment={"CANDIDATE_ONLY": "value"},
        timeout_seconds=1234,
    )

    inventory, inspection = process.commands
    assert inventory[-2:] == ("config", "--images")
    assert inspection == (
        "docker",
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
        *images,
    )
    assert not {"pull", "build", "up", "stop", "down", "rm"}.intersection((*inventory, *inspection))


def test_docker_compose_commands_use_explicit_host_project_namespace(tmp_path: Path) -> None:
    container_root = tmp_path / "container-app"
    host_root = tmp_path / "host-repository"
    process = _Process([(0, "fs-app\n")])
    gateway = DockerLifecycleGateway(
        project_root=container_root,
        docker_host_project_root=host_root,
        process=process,
    )
    target = _target().model_copy(update={"project_name": "phase4-host-path-test"})

    assert gateway.services(target) == ("fs-app",)

    command = process.commands[0]
    assert str(host_root / ".env") in command
    assert str(host_root / "infra/docker/docker-compose.yaml") in command
    assert not any(str(container_root) in argument for argument in command)


def test_docker_configuration_apply_stops_excluded_services_then_reconciles_runtime(
    tmp_path: Path,
) -> None:
    target = _target().model_copy(
        update={
            "required_services": (*_target().required_services, "fs-cli"),
            "runtime_services": (*_target().runtime_services, "fs-cli"),
        }
    )
    process = _Process(
        [
            (0, "fs-app\nfs-worker\npg-fs\nseaweedfs\nmailpit\nfs-cli\n"),
            (0, ""),
            (0, ""),
            (0, ""),
        ]
    )
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    gateway.apply_configuration(
        target,
        managed_services=(*target.required_services, "old-proxy"),
        timeout_seconds=41,
    )

    inventory, stop, reconcile, names = process.commands
    assert names[-2:] == _NAMES_LISTING
    assert inventory[-2:] == ("config", "--services")
    assert stop[-2:] == ("stop", "mailpit")
    assert reconcile[-10:] == (
        "up",
        "--detach",
        "--wait",
        "--wait-timeout",
        "41",
        "--remove-orphans",
        "fs-app",
        "fs-worker",
        "pg-fs",
        "seaweedfs",
    )
    assert "fs-cli" not in stop[stop.index("stop") + 1 :]
    assert "fs-cli" not in reconcile[reconcile.index("up") + 1 :]
    forbidden = {"down", "rm", "-v", "--volumes", "--renew-anon-volumes"}
    assert all(not forbidden.intersection(command) for command in process.commands)


def test_docker_configuration_apply_can_reconcile_transition_services_without_dependencies(
    tmp_path: Path,
) -> None:
    target = _target().model_copy(
        update={
            "required_services": (*_target().required_services, "rabbitmq-fs", "fs-cli"),
            "runtime_services": (*_target().runtime_services, "rabbitmq-fs", "fs-cli"),
        }
    )
    process = _Process(
        [
            (0, "fs-app\nfs-worker\npg-fs\nrabbitmq-fs\nfs-cli\n"),
            (0, ""),
            (0, ""),
        ]
    )
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    gateway.apply_configuration(
        target,
        managed_services=target.required_services,
        reconcile_services=("fs-app", "fs-worker"),
        no_dependencies=True,
        remove_orphans=False,
        timeout_seconds=41,
    )

    inventory, reconcile, names = process.commands
    assert names[-2:] == _NAMES_LISTING
    assert inventory[-2:] == ("config", "--services")
    assert reconcile[-8:] == (
        "up",
        "--detach",
        "--wait",
        "--wait-timeout",
        "41",
        "--no-deps",
        "fs-app",
        "fs-worker",
    )
    assert "--remove-orphans" not in reconcile
    assert "pg-fs" not in reconcile[reconcile.index("up") + 1 :]
    assert "rabbitmq-fs" not in reconcile[reconcile.index("up") + 1 :]
    assert "fs-cli" not in reconcile[reconcile.index("up") + 1 :]


def test_docker_configuration_failure_retains_redacted_terminal_output(
    tmp_path: Path,
) -> None:
    terminal_error = (
        "Error response from daemon: candidate failed for "
        "postgresql://runtime_user:runtime_secret@db/runtime"
    )
    process = _Process(
        [
            (0, "fs-app\nfs-worker\npg-fs\nseaweedfs\n"),
            (
                1,
                "orphan warning\n" + ("Container candidate Creating\n" * 30) + terminal_error,
            ),
        ]
    )
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    target = _target()

    with pytest.raises(StackGatewayError) as raised:
        gateway.apply_configuration(
            target,
            no_dependencies=True,
            remove_orphans=False,
            timeout_seconds=41,
        )

    detail = raised.value.detail
    assert len(detail) <= 600
    assert detail.startswith("...")
    assert "orphan warning" not in detail
    assert (
        "Error response from daemon: candidate failed for "
        "postgresql://runtime_user:[REDACTED]@db/runtime"
    ) in detail
    assert "runtime_secret" not in detail


def test_docker_configuration_apply_removes_source_only_profile_orphans(
    tmp_path: Path,
) -> None:
    target = _target()
    process = _Process(
        [
            (0, "fs-app\nfs-worker\npg-fs\nseaweedfs\n"),
            (0, ""),
            (0, ""),
        ]
    )
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    gateway.apply_configuration(
        target,
        managed_services=(*target.required_services, "fs-frontend", "mailpit", "sonarqube"),
        timeout_seconds=41,
    )

    inventory, reconcile, names = process.commands
    assert names[-2:] == _NAMES_LISTING
    assert inventory[-2:] == ("config", "--services")
    assert "stop" not in reconcile
    assert "--remove-orphans" in reconcile
    assert {"fs-frontend", "mailpit", "sonarqube"}.isdisjoint(reconcile)


def test_reverse_to_local_stops_old_backend_port_holder_before_candidate_up(
    tmp_path: Path,
) -> None:
    process = _Process([(0, "traefik\nfs-app\n"), (0, ""), (0, ""), (0, ""), (0, "")])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)
    target = _target().model_copy(
        update={
            "runtime_services": ("traefik", "fs-app"),
            "required_services": ("traefik", "fs-app"),
        }
    )

    gateway.apply_configuration(
        target,
        managed_services=("traefik", "fs-app"),
        stop_before_reconcile=("fs-app",),
        timeout_seconds=41,
    )

    assert process.commands[0][-2:] == ("config", "--services")
    assert process.commands[1][-2:] == ("stop", "fs-app")
    assert process.commands[-1][-2:] == _NAMES_LISTING
    assert process.commands[-2][-8:] == (
        "up",
        "--detach",
        "--wait",
        "--wait-timeout",
        "41",
        "--remove-orphans",
        "traefik",
        "fs-app",
    )


def test_candidate_storage_probe_uses_explicit_read_only_s3_client_configuration() -> None:
    calls: list[tuple[str, object]] = []

    class Client:
        def head_bucket(self, **kwargs: object) -> None:
            calls.append(("head_bucket", kwargs))

    class Session:
        def __init__(self, **kwargs: object) -> None:
            calls.append(("session", kwargs))

        def client(self, service: str, **kwargs: object) -> Client:
            calls.append((service, kwargs))
            return Client()

    from core.infrastructure.lifecycle import configuration_gateway as _configuration_gateway

    probe_type = getattr(_configuration_gateway, "CandidateStorageProbe", None)
    assert probe_type is not None
    probe = probe_type(session_factory=Session)
    environment = {
        "ARTIFACT_S3_ENDPOINT_URL": "https://objects.example.test",
        "ARTIFACT_S3_BUCKET": "artifacts",
        "ARTIFACT_S3_REGION": "eu-test-1",
        "ARTIFACT_S3_USE_SSL": "true",
        "ARTIFACT_S3_ACCESS_KEY": "candidate-access-secret",
        "ARTIFACT_S3_SECRET_KEY": "candidate-secret-value",
        "AWS_CA_BUNDLE": "/app/extensions/minio-ca.pem",
    }

    assert probe.validate(environment) is True

    assert calls[0] == (
        "session",
        {
            "aws_access_key_id": "candidate-access-secret",
            "aws_secret_access_key": "candidate-secret-value",
            "region_name": "eu-test-1",
        },
    )
    assert calls[1][0] == "s3"
    client_options = calls[1][1]
    assert isinstance(client_options, dict)
    assert client_options["endpoint_url"] == "https://objects.example.test"
    assert client_options["region_name"] == "eu-test-1"
    assert client_options["use_ssl"] is True
    assert client_options["verify"] == "/app/extensions/minio-ca.pem"
    config = client_options["config"]
    assert config.connect_timeout == 3
    assert config.read_timeout == 5
    assert config.retries["max_attempts"] == 0
    assert calls[2] == ("head_bucket", {"Bucket": "artifacts"})


def test_occupied_target_port_is_allowed_only_when_prior_managed_stack_owns_it(
    tmp_path: Path,
) -> None:
    process = _Process(
        [
            (
                0,
                json.dumps(
                    {
                        "ID": "abc",
                        "Name": "installed-project-traefik-1",
                        "Service": "traefik",
                        "State": "running",
                        "Health": "healthy",
                        "ExitCode": 0,
                        "Publishers": [
                            {
                                "URL": "0.0.0.0",
                                "PublishedPort": 8099,
                                "TargetPort": 8099,
                                "Protocol": "tcp",
                            }
                        ],
                    }
                ),
            ),
            (0, ""),
        ]
    )
    gateway = DockerLifecycleGateway(
        project_root=tmp_path,
        process=process,
        port_available=lambda _host, _port, _protocol: False,
    )

    assert gateway.port_available_for_transition(_target(), "0.0.0.0", 8099, protocol="tcp") is True


@pytest.mark.parametrize(
    ("published_host", "published_protocol"),
    (("127.0.0.2", "tcp"), ("127.0.0.1", "udp")),
)
def test_prior_port_requires_conflicting_bind_address_and_exact_protocol(
    tmp_path: Path,
    published_host: str,
    published_protocol: str,
) -> None:
    process = _Process(
        [
            (
                0,
                json.dumps(
                    {
                        "ID": "abc",
                        "Name": "installed-project-fs-app-1",
                        "Service": "fs-app",
                        "State": "running",
                        "Health": "healthy",
                        "ExitCode": 0,
                        "Publishers": [
                            {
                                "URL": published_host,
                                "PublishedPort": 8099,
                                "TargetPort": 8099,
                                "Protocol": published_protocol,
                            }
                        ],
                    }
                ),
            ),
            (0, ""),
        ]
    )
    gateway = DockerLifecycleGateway(
        project_root=tmp_path,
        process=process,
        port_available=lambda _host, _port, _protocol: False,
    )

    assert (
        gateway.port_available_for_transition(_target(), "127.0.0.1", 8099, protocol="tcp") is False
    )


def test_prior_wildcard_bind_authorizes_conflicting_specific_tcp_bind(tmp_path: Path) -> None:
    process = _Process(
        [
            (
                0,
                json.dumps(
                    {
                        "ID": "abc",
                        "Name": "installed-project-fs-app-1",
                        "Service": "fs-app",
                        "State": "running",
                        "Health": "healthy",
                        "ExitCode": 0,
                        "Publishers": [
                            {
                                "URL": "0.0.0.0",
                                "PublishedPort": 8099,
                                "TargetPort": 8099,
                                "Protocol": "tcp",
                            }
                        ],
                    }
                ),
            ),
            (0, ""),
        ]
    )
    gateway = DockerLifecycleGateway(
        project_root=tmp_path,
        process=process,
        port_available=lambda _host, _port, _protocol: False,
    )

    assert (
        gateway.port_available_for_transition(_target(), "127.0.0.1", 8099, protocol="tcp") is True
    )


def test_live_foreign_udp_socket_blocks_candidate_udp_port(tmp_path: Path) -> None:
    process = _Process([(0, ""), (0, "")])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as blocker:
        blocker.bind(("127.0.0.1", 0))
        port = int(blocker.getsockname()[1])

        assert (
            gateway.port_available_for_transition(_target(), "127.0.0.1", port, protocol="udp")
            is False
        )


def test_exited_prior_container_never_authorizes_live_foreign_owner(tmp_path: Path) -> None:
    process = _Process(
        [
            (
                0,
                json.dumps(
                    {
                        "ID": "abc",
                        "Name": "installed-project-traefik-1",
                        "Service": "traefik",
                        "State": "exited",
                        "Health": "",
                        "ExitCode": 0,
                        "Publishers": [
                            {
                                "URL": "127.0.0.1",
                                "PublishedPort": 8099,
                                "TargetPort": 8099,
                                "Protocol": "tcp",
                            }
                        ],
                    }
                ),
            ),
            (0, ""),
        ]
    )
    gateway = DockerLifecycleGateway(
        project_root=tmp_path,
        process=process,
        port_available=lambda _host, _port, _protocol: False,
    )

    assert (
        gateway.port_available_for_transition(_target(), "127.0.0.1", 8099, protocol="tcp") is False
    )


def test_unknown_port_protocol_fails_closed_without_socket_probe(tmp_path: Path) -> None:
    gateway = DockerLifecycleGateway(
        project_root=tmp_path,
        process=_Process([]),
        port_available=lambda _host, _port, _protocol: True,
    )

    assert (
        gateway.port_available_for_transition(_target(), "127.0.0.1", 8099, protocol="sctp")
        is False
    )


def test_foreign_port_probe_fails_closed(tmp_path: Path) -> None:
    foreign_port = DockerLifecycleGateway(
        project_root=tmp_path,
        process=_Process([(0, ""), (0, "")]),
        port_available=lambda _host, _port, _protocol: False,
    )
    assert foreign_port.port_available_for_transition(_target(), "0.0.0.0", 8099) is False


def test_docker_gateway_uses_canonical_compose_target_and_parses_json_lines(
    tmp_path: Path,
) -> None:
    process = _Process(
        [
            (
                0,
                "\n".join(
                    (
                        json.dumps(
                            {
                                "ID": "abc",
                                "Name": "installed-project-fs-app-1",
                                "Service": "fs-app",
                                "State": "running",
                                "Health": "healthy",
                                "ExitCode": 0,
                                "Publishers": [],
                            }
                        ),
                        json.dumps(
                            {
                                "ID": "def",
                                "Name": "installed-project-pg-fs-1",
                                "Service": "pg-fs",
                                "State": "running",
                                "Health": "",
                                "ExitCode": 0,
                                "Publishers": None,
                            }
                        ),
                    )
                ),
            ),
            (0, "installed-project_postgres\n"),
            (
                0,
                json.dumps(
                    {
                        "Name": "installed-project_postgres",
                        "Driver": "local",
                        "Scope": "local",
                        "CreatedAt": "2026-07-24T10:00:00Z",
                    }
                ),
            ),
        ]
    )
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    snapshot = gateway.status(_target())

    assert snapshot.services[0].service == "fs-app"
    assert snapshot.services[0].health == "healthy"
    assert snapshot.volumes[0].driver == "local"
    compose_command = process.commands[0]
    assert compose_command[:3] == ("docker", "compose", "--env-file")
    assert (
        compose_command[compose_command.index("-p")],
        compose_command[compose_command.index("-p") + 1],
    ) == ("-p", "installed-project")
    assert compose_command.count("-f") == 2
    assert compose_command[-4:] == ("ps", "--all", "--format", "json")


def test_docker_rebuild_builds_then_force_recreates_without_any_volume_flag(
    tmp_path: Path,
) -> None:
    process = _Process([(0, ""), (0, "")])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    gateway.rebuild(
        _target(),
        ("fs-app", "fs-worker"),
        timeout_seconds=37,
        output_sink=lambda _line: None,
        heartbeat=lambda _detail: None,
    )

    build, recreate = process.commands
    assert build[-3:] == ("build", "fs-app", "fs-worker")
    assert recreate[-8:] == (
        "up",
        "--wait",
        "--wait-timeout",
        "37",
        "--force-recreate",
        "--no-deps",
        "fs-app",
        "fs-worker",
    )
    forbidden = {"-v", "--volumes", "--renew-anon-volumes"}
    assert forbidden.isdisjoint(build)
    assert forbidden.isdisjoint(recreate)


def test_docker_gateway_captures_exact_image_ids_and_running_stopped_topology(
    tmp_path: Path,
) -> None:
    app_image = f"sha256:{'a' * 64}"
    worker_image = f"sha256:{'b' * 64}"
    process = _Process(
        [
            (
                0,
                "\n".join(
                    (
                        json.dumps(
                            {
                                "ID": "container-app",
                                "Name": "installed-project-fs-app-1",
                                "Service": "fs-app",
                                "State": "running",
                                "Health": "healthy",
                            }
                        ),
                        json.dumps(
                            {
                                "ID": "container-worker",
                                "Name": "installed-project-fs-worker-1",
                                "Service": "fs-worker",
                                "State": "exited",
                                "Health": "",
                            }
                        ),
                    )
                ),
            ),
            (0, ""),
            (
                0,
                "\n".join(
                    (
                        json.dumps(
                            {
                                "Id": "container-app",
                                "Image": app_image,
                                "Config": {"Labels": {"com.docker.compose.service": "fs-app"}},
                            }
                        ),
                        json.dumps(
                            {
                                "Id": "container-worker",
                                "Image": worker_image,
                                "Config": {"Labels": {"com.docker.compose.service": "fs-worker"}},
                            }
                        ),
                    )
                ),
            ),
        ]
    )
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    snapshot = gateway.capture_rollback(
        _target(),
        operation_id="operation-rebuild",
        selected_services=("fs-app", "fs-worker"),
        managed_services=("fs-app", "fs-worker", "pg-fs"),
    )

    by_service = {item.service: item for item in snapshot.services}
    assert by_service["fs-app"].topology == "running"
    assert by_service["fs-app"].image_id == app_image
    assert by_service["fs-worker"].topology == "stopped"
    assert by_service["fs-worker"].image_id == worker_image
    assert by_service["pg-fs"].topology == "absent"
    assert process.commands[-1] == (
        "docker",
        "inspect",
        "--format",
        "{{json .}}",
        "container-app",
        "container-worker",
    )


def test_runtime_image_check_compares_inspected_image_ids(tmp_path: Path) -> None:
    before_image = f"sha256:{'a' * 64}"
    after_image = f"sha256:{'b' * 64}"
    snapshot = StackRollbackSnapshot(
        operation_id="operation-start",
        project_name="installed-project",
        selected_services=("fs-app",),
        services=(
            StackContainerRollbackState(
                service="fs-app",
                topology="stopped",
                container_id="container-before",
                image_id=before_image,
            ),
        ),
    )
    process = _Process(
        [
            (
                0,
                json.dumps(
                    {
                        "ID": "container-after",
                        "Name": "installed-project-fs-app-1",
                        "Service": "fs-app",
                        "State": "running",
                        "Health": "healthy",
                    }
                ),
            ),
            (0, ""),
            (
                0,
                json.dumps(
                    {
                        "Id": "container-after",
                        "Image": after_image,
                        "Config": {"Labels": {"com.docker.compose.service": "fs-app"}},
                    }
                ),
            ),
            (0, before_image),
        ]
    )
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    assert gateway.runtime_images_match(_target(), snapshot) is False
    assert process.commands[-2][-1] == "container-after"
    assert process.commands[-1][-1] == before_image


def test_runtime_image_drift_names_the_prior_and_running_identity(tmp_path: Path) -> None:
    before_image = f"sha256:{'a' * 64}"
    after_image = f"sha256:{'b' * 64}"
    snapshot = StackRollbackSnapshot(
        operation_id="operation-start",
        project_name="installed-project",
        selected_services=("fs-app",),
        services=(
            StackContainerRollbackState(
                service="fs-app",
                topology="running",
                container_id="container-before",
                image_id=before_image,
            ),
        ),
    )
    process = _Process(
        [
            (
                0,
                json.dumps(
                    {
                        "ID": "container-after",
                        "Name": "installed-project-fs-app-1",
                        "Service": "fs-app",
                        "State": "running",
                        "Health": "healthy",
                    }
                ),
            ),
            (0, ""),
            (
                0,
                json.dumps(
                    {
                        "Id": "container-after",
                        "Image": after_image,
                        "Config": {"Labels": {"com.docker.compose.service": "fs-app"}},
                    }
                ),
            ),
            (1, "No such image"),
        ]
    )
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    drift = gateway.runtime_image_drift(_target(), snapshot)

    assert len(drift) == 1
    assert drift[0].service == "fs-app"
    assert drift[0].expected_image_id == before_image
    assert drift[0].observed_image_id == after_image
    assert drift[0].expected_image_available is False
    assert drift[0].describe() == f"fs-app was {before_image}, now {after_image}"


def test_runtime_image_check_allows_source_only_services_to_leave_profile(
    tmp_path: Path,
) -> None:
    snapshot = StackRollbackSnapshot(
        operation_id="operation-profile-switch",
        project_name="installed-project",
        selected_services=("fs-app",),
        services=(
            StackContainerRollbackState(
                service="fs-app",
                topology="running",
                container_id="container-before",
                image_id=f"sha256:{'a' * 64}",
            ),
            StackContainerRollbackState(
                service="compact-runtime",
                topology="absent",
            ),
        ),
    )
    target = _target().model_copy(
        update={
            "required_services": ("compact-runtime", "pg-fs"),
            "runtime_services": ("compact-runtime", "pg-fs"),
        }
    )
    process = _Process([(0, ""), (0, "")])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    assert gateway.runtime_images_match(target, snapshot) is True


def test_rollback_snapshot_rejects_unproven_image_identity() -> None:
    def create_unproven_snapshot() -> StackRollbackSnapshot:
        return StackRollbackSnapshot(
            operation_id="operation-rebuild",
            project_name="installed-project",
            selected_services=("fs-app",),
            services=(
                StackContainerRollbackState(
                    service="fs-app",
                    topology="running",
                    container_id="container-app",
                    image_id="latest",
                ),
            ),
        )

    with pytest.raises(ValidationError):
        create_unproven_snapshot()


def test_rebuild_restore_recreates_exact_prior_images_and_topology_without_volumes(
    tmp_path: Path,
) -> None:
    app_image = f"sha256:{'a' * 64}"
    worker_image = f"sha256:{'b' * 64}"
    database_image = f"sha256:{'c' * 64}"
    snapshot = StackRollbackSnapshot(
        operation_id="operation-rebuild",
        project_name="installed-project",
        selected_services=("fs-app", "fs-worker"),
        services=(
            StackContainerRollbackState(
                service="fs-app",
                topology="running",
                container_id="old-app",
                image_id=app_image,
            ),
            StackContainerRollbackState(
                service="fs-worker",
                topology="stopped",
                container_id="old-worker",
                image_id=worker_image,
            ),
            StackContainerRollbackState(
                service="pg-fs",
                topology="running",
                container_id="old-database",
                image_id=database_image,
            ),
        ),
    )
    process = _Process(
        [
            (0, ""),
            (0, ""),
            (0, ""),
            (0, ""),
            (0, ""),
            (
                0,
                "\n".join(
                    (
                        json.dumps(
                            {
                                "ID": "restored-app",
                                "Name": "installed-project-fs-app-1",
                                "Service": "fs-app",
                                "State": "running",
                                "Health": "healthy",
                            }
                        ),
                        json.dumps(
                            {
                                "ID": "restored-database",
                                "Name": "installed-project-pg-fs-1",
                                "Service": "pg-fs",
                                "State": "running",
                                "Health": "healthy",
                            }
                        ),
                        json.dumps(
                            {
                                "ID": "restored-worker",
                                "Name": "installed-project-fs-worker-1",
                                "Service": "fs-worker",
                                "State": "exited",
                                "Health": "",
                            }
                        ),
                    )
                ),
            ),
            (0, ""),
            (
                0,
                "\n".join(
                    (
                        json.dumps(
                            {
                                "Id": "restored-app",
                                "Image": app_image,
                                "Config": {"Labels": {"com.docker.compose.service": "fs-app"}},
                            }
                        ),
                        json.dumps(
                            {
                                "Id": "restored-worker",
                                "Image": worker_image,
                                "Config": {"Labels": {"com.docker.compose.service": "fs-worker"}},
                            }
                        ),
                        json.dumps(
                            {
                                "Id": "restored-database",
                                "Image": database_image,
                                "Config": {"Labels": {"com.docker.compose.service": "pg-fs"}},
                            }
                        ),
                    )
                ),
            ),
            (0, ""),
            (0, ""),
        ]
    )
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    restored = gateway.restore(
        _target(),
        action="rebuild",
        snapshot=snapshot,
        timeout_seconds=37,
        output_sink=lambda _line: None,
        heartbeat=lambda _detail: None,
        environment={"POSTGRES_HOST_PORT": "23559"},
    )

    assert restored is True
    assert process.commands[0][:3] == ("docker", "image", "tag")
    assert process.commands[1][:3] == ("docker", "image", "tag")
    pinned_app = process.commands[0][-1]
    pinned_worker = process.commands[1][-1]
    assert pinned_app.startswith("flow-steward-rollback:")
    assert pinned_worker.startswith("flow-steward-rollback:")
    recreate_running = process.commands[2]
    assert (
        recreate_running[-8:]
        == (
            "up",
            "--wait",
            "--wait-timeout",
            "37",
            "--force-recreate",
            "--no-build",
            "--no-deps",
            "fs-app",
        )[-8:]
    )
    recreate_stopped = process.commands[3]
    assert recreate_stopped[-8:] == (
        "up",
        "--no-start",
        "--force-recreate",
        "--no-build",
        "--no-deps",
        # C4: at the count the snapshot recorded, not the restored environment's
        "--scale",
        "fs-worker=1",
        "fs-worker",
    )
    assert "operation-rebuild.rollback.compose.json" in " ".join(recreate_running)
    assert "operation-rebuild.rollback.compose.json" in " ".join(recreate_stopped)
    assert process.invocations[2]["environment"] == {"POSTGRES_HOST_PORT": "23559"}
    assert process.invocations[3]["environment"] == {"POSTGRES_HOST_PORT": "23559"}
    assert process.invocations[4]["environment"] == {"POSTGRES_HOST_PORT": "23559"}
    assert (
        process.commands[4][-6:]
        == (
            "up",
            "--wait",
            "--wait-timeout",
            "37",
            "--no-recreate",
            "--no-deps",
            "pg-fs",
        )[-6:]
    )
    assert not any(command[-2:] == ("stop", "fs-worker") for command in process.commands)
    assert not any(flag in command for command in process.commands for flag in ("-v", "--volumes"))
    override = tmp_path / ".flow-steward/operations/operation-rebuild.rollback.compose.json"
    payload = json.loads(override.read_text(encoding="utf-8"))
    assert payload == {
        "services": {
            "fs-app": {"image": pinned_app},
            "fs-worker": {"image": pinned_worker},
        }
    }
    # restore must NOT drop the pins: the override it just wrote names them, and any
    # later Compose call that reads it would fail to resolve a deleted tag.
    assert not any(command[:3] == ("docker", "image", "rm") for command in process.commands)
    assert override.is_file()

    process.responses[:] = [(0, "id"), (0, ""), (0, "id"), (0, ""), (1, "No such image")]
    before_release = len(process.commands)
    retained = gateway.release_rollback_pins(snapshot)
    released = process.commands[before_release:]

    assert retained == ()
    assert ("docker", "image", "rm", pinned_app) in released
    assert ("docker", "image", "rm", pinned_worker) in released
    # pg-fs was never pinned, so release probes and skips it without removing anything
    assert sum(command[:3] == ("docker", "image", "rm") for command in released) == 2
    assert not override.exists()


def test_rebuild_restore_failure_never_starts_a_stopped_exact_image(tmp_path: Path) -> None:
    snapshot = StackRollbackSnapshot(
        operation_id="operation-failed-rebuild",
        project_name="installed-project",
        selected_services=("fs-app", "fs-worker"),
        services=(
            StackContainerRollbackState(
                service="fs-app",
                topology="running",
                container_id="old-app",
                image_id=f"sha256:{'a' * 64}",
            ),
            StackContainerRollbackState(
                service="fs-worker",
                topology="stopped",
                container_id="old-worker",
                image_id=f"sha256:{'b' * 64}",
            ),
        ),
    )
    process = _Process([(0, ""), (0, ""), (1, "restore failed"), (0, ""), (0, "")])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    restored = gateway.restore(
        _target(),
        action="rebuild",
        snapshot=snapshot,
        timeout_seconds=37,
        output_sink=lambda _line: None,
        heartbeat=lambda _detail: None,
    )

    assert restored is False
    failed_recreate = process.commands[2]
    assert failed_recreate[-1] == "fs-app"
    assert "fs-worker" not in failed_recreate
    assert not any("--no-start" in command for command in process.commands)


def test_rebuild_restore_removes_candidate_only_service_by_compose_labels(
    tmp_path: Path,
) -> None:
    snapshot = StackRollbackSnapshot(
        operation_id="operation-profile-transition",
        project_name="installed-project",
        selected_services=("compact-runtime",),
        services=(
            StackContainerRollbackState(
                service="compact-runtime",
                topology="absent",
            ),
        ),
    )
    process = _Process(
        [
            (0, "0123456789ab\n"),
            (0, ""),
            (0, ""),
            (0, ""),
        ]
    )
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    restored = gateway.restore(
        _target(),
        action="rebuild",
        snapshot=snapshot,
        timeout_seconds=37,
        output_sink=lambda _line: None,
        heartbeat=lambda _detail: None,
    )

    assert restored is True
    assert process.commands[0] == (
        "docker",
        "ps",
        "--all",
        "--filter",
        "label=com.docker.compose.project=installed-project",
        "--filter",
        "label=com.docker.compose.service=compact-runtime",
        "--format",
        "{{.ID}}",
    )
    assert process.commands[1] == (
        "docker",
        "rm",
        "--force",
        "0123456789ab",
    )
    assert not any(command[-2:] == ("rm", "compact-runtime") for command in process.commands)


def test_rebuild_restore_stops_candidate_only_service_by_compose_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = StackRollbackSnapshot(
        operation_id="operation-profile-transition",
        project_name="installed-project",
        selected_services=(),
        services=(
            StackContainerRollbackState(
                service="compact-runtime",
                topology="stopped",
                container_id="old-compact",
                image_id=f"sha256:{'a' * 64}",
            ),
        ),
    )
    process = _Process([(0, "0123456789ab\n"), (0, "")])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)
    monkeypatch.setattr(gateway, "services", lambda _target: _target().required_services)
    monkeypatch.setattr(gateway, "capture_rollback", lambda *_args, **_kwargs: snapshot)

    restored = gateway.restore(
        _target(),
        action="rebuild",
        snapshot=snapshot,
        timeout_seconds=37,
        output_sink=lambda _line: None,
        heartbeat=lambda _detail: None,
    )

    assert restored is True
    assert process.commands[0] == (
        "docker",
        "ps",
        "--all",
        "--filter",
        "label=com.docker.compose.project=installed-project",
        "--filter",
        "label=com.docker.compose.service=compact-runtime",
        "--format",
        "{{.ID}}",
    )
    assert process.commands[1] == ("docker", "stop", "0123456789ab")


def test_rebuild_restore_removes_temporary_image_pin_after_compose_failure(
    tmp_path: Path,
) -> None:
    image_id = f"sha256:{'a' * 64}"
    snapshot = StackRollbackSnapshot(
        operation_id="operation-rebuild",
        project_name="installed-project",
        selected_services=("fs-app",),
        services=(
            StackContainerRollbackState(
                service="fs-app",
                topology="running",
                container_id="old-app",
                image_id=image_id,
            ),
        ),
    )
    process = _Process([(0, ""), (1, "compose failed"), (0, "")])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    restored = gateway.restore(
        _target(),
        action="rebuild",
        snapshot=snapshot,
        timeout_seconds=37,
        output_sink=lambda _line: None,
        heartbeat=lambda _detail: None,
    )

    assert restored is False
    pinned = process.commands[0][-1]
    assert process.commands[0] == ("docker", "image", "tag", image_id, pinned)
    assert not any(command[:3] == ("docker", "image", "rm") for command in process.commands)

    process.responses[:] = [(0, "id"), (0, "")]
    assert gateway.release_rollback_pins(snapshot) == ()
    assert process.commands[-1] == ("docker", "image", "rm", pinned)


def test_rebuild_restore_removes_partial_pins_when_later_image_pin_fails(
    tmp_path: Path,
) -> None:
    app_image = f"sha256:{'a' * 64}"
    worker_image = f"sha256:{'b' * 64}"
    snapshot = StackRollbackSnapshot(
        operation_id="operation-rebuild",
        project_name="installed-project",
        selected_services=("fs-app", "fs-worker"),
        services=(
            StackContainerRollbackState(
                service="fs-app",
                topology="running",
                container_id="old-app",
                image_id=app_image,
            ),
            StackContainerRollbackState(
                service="fs-worker",
                topology="running",
                container_id="old-worker",
                image_id=worker_image,
            ),
        ),
    )
    process = _Process([(0, ""), (1, "missing image"), (0, "")])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    restored = gateway.restore(
        _target(),
        action="rebuild",
        snapshot=snapshot,
        timeout_seconds=37,
        output_sink=lambda _line: None,
        heartbeat=lambda _detail: None,
    )

    assert restored is False
    first_pin = process.commands[0][-1]
    assert process.commands[0] == ("docker", "image", "tag", app_image, first_pin)
    assert process.commands[1][:4] == ("docker", "image", "tag", worker_image)
    assert not any(command[:3] == ("docker", "image", "rm") for command in process.commands)

    # release probes each pin and skips the one that was never created
    process.responses[:] = [(0, "id"), (0, ""), (1, "No such image")]
    before_release = len(process.commands)
    assert gateway.release_rollback_pins(snapshot) == ()
    released = process.commands[before_release:]
    assert ("docker", "image", "rm", first_pin) in released
    assert sum(command[:3] == ("docker", "image", "rm") for command in released) == 1


def test_release_keeps_the_override_when_a_temporary_pin_cannot_be_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_image = f"sha256:{'a' * 64}"
    worker_image = f"sha256:{'b' * 64}"
    snapshot = StackRollbackSnapshot(
        operation_id="operation-rebuild",
        project_name="installed-project",
        selected_services=("fs-app", "fs-worker"),
        services=(
            StackContainerRollbackState(
                service="fs-app",
                topology="running",
                container_id="old-app",
                image_id=app_image,
            ),
            StackContainerRollbackState(
                service="fs-worker",
                topology="running",
                container_id="old-worker",
                image_id=worker_image,
            ),
        ),
    )
    process = _Process([(0, ""), (0, ""), (0, "")])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)
    monkeypatch.setattr(gateway, "capture_rollback", lambda *_args, **_kwargs: snapshot)

    assert (
        gateway.restore(
            _target(),
            action="rebuild",
            snapshot=snapshot,
            timeout_seconds=37,
            output_sink=lambda _line: None,
            heartbeat=lambda _detail: None,
        )
        is True
    )
    override = tmp_path / ".flow-steward/operations/operation-rebuild.rollback.compose.json"
    assert override.is_file()

    # first pin removal fails, second succeeds
    process.responses[:] = [(0, "id"), (1, "in use"), (0, "id"), (0, "")]
    retained = gateway.release_rollback_pins(snapshot)

    # A tag that survived still resolves, so the override naming it must survive too.
    assert len(retained) == 2
    assert retained[0].startswith("flow-steward-rollback:")
    assert retained[1] == str(override)
    assert override.is_file()


def test_storage_connectivity_uses_authenticated_crud_selected_compose_probe(
    tmp_path: Path,
) -> None:
    process = _Process([(0, "")])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    assert gateway.storage_connectivity(_target()) is True

    command = process.commands[0]
    assert command[-9:-1] == (
        "run",
        "--rm",
        "-T",
        "--no-deps",
        "--entrypoint",
        "python",
        "fs-app",
        "-c",
    )
    script = command[-1]
    assert "head_bucket" in script
    assert "put_object" in script
    assert "get_object" in script
    assert "delete_object" in script
    assert "ARTIFACT_S3_ACCESS_KEY" in script
    assert "signature_version=UNSIGNED" not in script


def test_local_fs_storage_connectivity_probes_the_persistent_artifact_root(
    tmp_path: Path,
) -> None:
    process = _Process([(0, "")])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)
    target = _target().model_copy(update={"file_storage": "local_fs"})

    assert gateway.storage_connectivity(target) is True

    command = process.commands[0]
    assert command[-9:-1] == (
        "run",
        "--rm",
        "-T",
        "--no-deps",
        "--entrypoint",
        "python",
        "fs-app",
        "-c",
    )
    script = command[-1]
    assert "ARTIFACT_LOCAL_ROOT_DIR" in script
    assert "mkstemp" in script
    assert "head_bucket" not in script
    compile(script, "<local-storage-probe>", "exec")


def test_compact_storage_connectivity_uses_disposable_app_role_probe(tmp_path: Path) -> None:
    process = _Process([(0, "")])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)
    target = _target().model_copy(
        update={"deployment_profile": "compact", "file_storage": "local_fs"}
    )

    assert gateway.storage_connectivity(target) is True

    assert process.commands[0][-11:-1] == (
        "run",
        "--rm",
        "-T",
        "--no-deps",
        "--user",
        "10001:10100",
        "--entrypoint",
        "python",
        "compact-runtime",
        "-c",
    )


@pytest.mark.parametrize("deployment_profile", ["scalable", "compact"])
def test_disposable_storage_probe_failure_stays_failed_without_starting_dependencies(
    tmp_path: Path,
    deployment_profile: str,
) -> None:
    process = _Process([(1, "artifact storage denied")])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)
    target = _target().model_copy(
        update={"deployment_profile": deployment_profile, "file_storage": "local_fs"}
    )

    assert gateway.storage_connectivity(target) is False

    command = process.commands[0]
    assert "--rm" in command
    assert "--no-deps" in command
    assert "up" not in command
    assert "start" not in command


def test_the_raising_storage_probe_keeps_the_probe_output(tmp_path: Path) -> None:
    """AC17-R148: the boolean form drops this; `config apply` needs it to say why."""
    process = _Process([(1, "RuntimeError: local artifact root is unavailable")])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)
    target = _target().model_copy(update={"file_storage": "local_fs"})

    with pytest.raises(StackGatewayError) as raised:
        gateway.require_storage_connectivity(target)

    assert raised.value.action == "storage_readiness"
    assert raised.value.detail == "RuntimeError: local artifact root is unavailable"
    assert process.commands[0][-3:-1] == ("fs-app", "-c")


def test_workflow_knowledge_reconciliation_enqueues_inside_installed_control_plane(
    tmp_path: Path,
) -> None:
    process = _Process([(0, "")])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    gateway.reconcile_workflow_knowledge(_target())

    command = process.commands[0]
    assert command[-6:-3] == ("exec", "-T", "control-plane")
    assert command[-2] == "-c"
    script = command[-1]
    compile(script, "<workflow-knowledge-reconciliation>", "exec")
    assert "workflow_knowledge_sync_scan" in script
    assert "ControlConfig.from_env" in script
    assert "run_workflow_knowledge_sync_job" not in script
    assert "RegistryDBStore" not in script
    assert "PostgresAgeWorkflowKnowledgeStore" not in script
    assert (
        "from core.infrastructure.runtime.entrypoint_db_adapters import "
        "create_control_stores, resolve_platform_dsn"
    ) in script
    assert "config.connection_strings import resolve_platform_dsn" not in script


def test_compact_workflow_knowledge_reconciliation_executes_inside_compact_runtime(
    tmp_path: Path,
) -> None:
    process = _Process([(0, "")])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)
    target = _target().model_copy(update={"deployment_profile": "compact"})

    gateway.reconcile_workflow_knowledge(target)

    assert process.commands[0][-6:-3] == ("exec", "-T", "compact-runtime")


def test_bundled_storage_write_probe_uses_ephemeral_target_network_service(
    tmp_path: Path,
) -> None:
    process = _Process([(0, "")])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    gateway.probe_bundled_storage(_target())

    command = process.commands[0]
    assert command[-9:-5] == ("run", "--rm", "-T", "--no-deps")
    assert command[-5:-2] == ("--entrypoint", "python", "fs-cli")
    script = command[-1]
    assert "put_object" in script
    assert "get_object" in script
    assert "delete_object" in script
    assert "ARTIFACT_S3_SECRET_KEY" in script
    assert "Config(connect_timeout=1, read_timeout=2" in script
    assert '"total_max_attempts": 1' in script
    assert "from botocore.exceptions import BotoCoreError, ClientError" in script
    assert "except ClientError as exc" in script
    assert "{'404', 'NoSuchBucket', 'NotFound'}" in script
    assert "client.create_bucket(Bucket=bucket)" in script
    assert "except BotoCoreError" in script
    assert "readiness_deadline = time.monotonic() + 30" in script
    assert "def probe_once()" in script
    assert "while True:\n    try:\n        probe_once()" in script
    assert process.invocations[0]["timeout_seconds"] > 30
    assert "bundled-access" not in " ".join(command)
    compose = (Path(__file__).parents[3] / "infra/docker/docker-compose.yaml").read_text(
        encoding="utf-8"
    )
    fs_cli = compose.split("  fs-cli:", 1)[1].split("  fs-worker:", 1)[0]
    assert "artifact-storage" in fs_cli


@pytest.mark.parametrize(
    ("action", "expected_suffixes"),
    (
        (
            "start",
            (
                (
                    "up",
                    "--wait",
                    "--wait-timeout",
                    "37",
                    "--no-build",
                    "--pull",
                    "never",
                    "fs-app",
                    "fs-worker",
                ),
                _NAMES_LISTING,
            ),
        ),
        ("stop", (("stop", "fs-app", "fs-worker"),)),
        (
            "restart",
            (
                ("restart", "fs-app", "fs-worker"),
                (
                    "up",
                    "--wait",
                    "--wait-timeout",
                    "37",
                    "--no-recreate",
                    "fs-app",
                    "fs-worker",
                ),
                _NAMES_LISTING,
            ),
        ),
    ),
)
def test_docker_mutation_argv_is_exact_for_selected_services(
    tmp_path: Path,
    action: str,
    expected_suffixes: tuple[tuple[str, ...], ...],
) -> None:
    process = _Process([(0, "")] * len(expected_suffixes))
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)

    getattr(gateway, action)(
        _target(),
        ("fs-app", "fs-worker"),
        timeout_seconds=37,
        output_sink=lambda _line: None,
        heartbeat=lambda _detail: None,
    )

    assert len(process.commands) == len(expected_suffixes)
    for command, suffix in zip(process.commands, expected_suffixes, strict=True):
        assert command[-len(suffix) :] == suffix


def test_docker_logs_argv_is_exact_and_does_not_merge_output(tmp_path: Path) -> None:
    process = _Process([(0, "line one\nline two")])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)
    output: list[str] = []

    lines = gateway.logs(
        _target(),
        service="mailpit",
        tail=17,
        follow=True,
        output_sink=output.append,
    )

    assert lines == 2
    assert process.commands[0][-6:] == (
        "logs",
        "--no-color",
        "--tail",
        "17",
        "--follow",
        "mailpit",
    )
    assert output == ["line one", "line two"]


def _recovery_attempts(journal) -> int:
    recorded = journal.step_outputs.get("recovery")
    return int(recorded.get("attempts", 0)) if isinstance(recorded, dict) else 0


def _start_drift_snapshot() -> StackRollbackSnapshot:
    return StackRollbackSnapshot(
        operation_id="operation-start-drift",
        project_name="installed-project",
        selected_services=("fs-app", "fs-worker"),
        services=(
            StackContainerRollbackState(
                service="fs-app",
                topology="running",
                container_id="old-app",
                image_id=f"sha256:{'a' * 64}",
            ),
            StackContainerRollbackState(
                service="fs-worker",
                topology="running",
                container_id="old-worker",
                image_id=f"sha256:{'b' * 64}",
            ),
        ),
    )


def _drifted_observation(snapshot: StackRollbackSnapshot) -> StackRollbackSnapshot:
    return snapshot.model_copy(
        update={
            "services": (
                snapshot.services[0].model_copy(
                    update={
                        "container_id": "new-app",
                        "image_id": f"sha256:{'c' * 64}",
                    }
                ),
                snapshot.services[1],
            )
        }
    )


def test_start_restore_recreates_only_the_drifted_service_from_its_exact_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _start_drift_snapshot()
    observations = [_drifted_observation(snapshot), snapshot]
    process = _Process([(0, f"sha256:{'a' * 64}"), (0, ""), (0, ""), (0, ""), (0, "")])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)
    monkeypatch.setattr(
        gateway,
        "capture_rollback",
        lambda *_args, **_kwargs: observations.pop(0),
    )

    restored = gateway.restore(
        _target(),
        action="start",
        snapshot=snapshot,
        timeout_seconds=37,
        output_sink=lambda _line: None,
        heartbeat=lambda _detail: None,
    )

    assert restored is True
    tag_commands = [
        command for command in process.commands if command[:3] == ("docker", "image", "tag")
    ]
    assert len(tag_commands) == 1
    assert tag_commands[0][3] == f"sha256:{'a' * 64}"
    force_recreate = next(command for command in process.commands if "--force-recreate" in command)
    assert force_recreate[-1] == "fs-app"
    assert "fs-worker" not in force_recreate
    override = (
        tmp_path / ".flow-steward" / "operations" / f"{snapshot.operation_id}.rollback.compose.json"
    )
    assert json.loads(override.read_text())["services"]["fs-app"]["image"] == tag_commands[0][4]


def test_start_restore_keeps_the_cheap_path_when_no_image_drifted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _start_drift_snapshot()
    process = _Process([(0, "")])
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=process)
    monkeypatch.setattr(gateway, "capture_rollback", lambda *_args, **_kwargs: snapshot)

    restored = gateway.restore(
        _target(),
        action="start",
        snapshot=snapshot,
        timeout_seconds=37,
        output_sink=lambda _line: None,
        heartbeat=lambda _detail: None,
    )

    assert restored is True
    assert not [
        command for command in process.commands if command[:3] == ("docker", "image", "tag")
    ]
    assert "--force-recreate" not in process.commands[0]


def _open_start_recovery(
    tmp_path: Path,
    docker: FakeDockerGateway,
) -> tuple[StackService, str]:
    """Reproduce the audited trigger: a start rejected by drifted images."""
    docker.images_preserved = False
    docker.restore_success = False
    service, _docker, _preservation, _store = _service(tmp_path, docker=docker)

    request = StackStartRequest()

    with pytest.raises(LifecycleError) as raised:
        service.start(request)

    assert raised.value.code == "stack_start_recovery_required"
    operation_id = raised.value.operation_id
    assert operation_id
    journal = service.lifecycle_state.load_journal(operation_id)
    assert journal is not None
    assert journal.status is LifecycleOperationStatus.RECOVERY_REQUIRED
    docker.images_preserved = False
    return service, operation_id


def test_open_start_recovery_is_closed_by_the_prescribed_command(tmp_path: Path) -> None:
    docker = FakeDockerGateway()
    service, operation_id = _open_start_recovery(tmp_path, docker)
    before = service.lifecycle_state.load_journal(operation_id)
    assert before is not None
    docker.restore_success = True

    result = service.start(StackStartRequest())

    assert result.operation_id == operation_id
    assert result.status == "recovered"
    assert result.runtime_restored is True
    journal = service.lifecycle_state.load_journal(operation_id)
    assert journal is not None
    assert journal.status is LifecycleOperationStatus.FAILED
    assert journal.last_error_code == "stack_recovery_completed"
    assert _recovery_attempts(journal) == _recovery_attempts(before) + 1
    assert journal.step_outputs["recovery"]["last_attempt_at"]
    # The unfinished-operations gate that blocks `maintenance disable` is now clear.
    assert service.lifecycle_state.unfinished_journals() == ()
    # Recovery asks the gateway for an exact restore of the recorded start snapshot,
    # which is what DockerLifecycleGateway.restore now performs for `start`.
    action, snapshot = docker.restore_requests[-1]
    assert action == "start"
    assert {item.service for item in snapshot.services} >= {"fs-app", "fs-worker"}


def test_repeated_recovery_attempt_is_recorded_in_the_operation_journal(
    tmp_path: Path,
) -> None:
    docker = FakeDockerGateway()
    service, operation_id = _open_start_recovery(tmp_path, docker)
    before = service.lifecycle_state.load_journal(operation_id)
    assert before is not None

    request = StackStartRequest()

    with pytest.raises(LifecycleError) as raised:
        service.start(request)

    assert raised.value.code == "stack_start_recovery_required"
    journal = service.lifecycle_state.load_journal(operation_id)
    assert journal is not None
    assert journal.status is LifecycleOperationStatus.RECOVERY_REQUIRED
    assert _recovery_attempts(journal) == _recovery_attempts(before) + 1
    assert journal.step_outputs["recovery"]["last_attempt_at"]
    assert journal.step_outputs["recovery"]["last_error_code"] == raised.value.code
    assert journal.updated_at > before.updated_at
    assert journal.log_path != before.log_path


def test_unrestorable_recovery_names_the_images_and_a_different_next_action(
    tmp_path: Path,
) -> None:
    docker = FakeDockerGateway()
    docker.prior_image_available = False
    service, _operation_id = _open_start_recovery(tmp_path, docker)

    request = StackStartRequest()

    with pytest.raises(LifecycleError) as raised:
        service.start(request)

    failure = raised.value
    assert failure.code == "stack_start_recovery_required"
    assert f"fs-app was sha256:{1:064x}" in failure.remediation
    assert "no longer in the local image store" in failure.remediation
    assert "--adopt-image-pins" in failure.remediation
    assert failure.remediation.strip() != f"Run exactly: {failure.recovery_command}"
    assert failure.details["image_transitions"]


def test_declared_image_pin_adoption_completes_a_same_profile_redeploy(
    tmp_path: Path,
) -> None:
    docker = FakeDockerGateway()
    docker.images_preserved = False
    service, _docker, _preservation, _store = _service(tmp_path, docker=docker)

    result = service.start(StackStartRequest(adopt_image_pins=True))

    assert result.preservation_verified is True
    assert not [call for call in docker.calls if call[0] == "restore"]
    assert ("configured_image_identities", ()) in docker.calls
    journal = service.lifecycle_state.load_journal(result.operation_id)
    assert journal is None or journal.parameters.get("adopt_image_pins") is True


def test_declared_adoption_still_rejects_an_image_the_configuration_never_pinned(
    tmp_path: Path,
) -> None:
    docker = FakeDockerGateway()
    docker.images_preserved = False
    docker.configured_image_ids = {"fs-app": f"sha256:{7:064x}"}
    service, _docker, _preservation, _store = _service(tmp_path, docker=docker)

    request = StackStartRequest(adopt_image_pins=True)

    with pytest.raises(LifecycleError) as raised:
        service.start(request)

    assert raised.value.code == "stack_start_failed"
    assert "without matching the configured image pins" in raised.value.cause
    assert any(call[0] == "restore" for call in docker.calls)


def test_recovery_attempt_record_stays_readable_by_the_previous_journal_contract(
    tmp_path: Path,
) -> None:
    """A redeploy runs a new CLI against roles still on the previous image.

    Those roles validate the same journal with a closed model, so an attempt
    record must never introduce a new top-level journal field.
    """
    docker = FakeDockerGateway()
    service, operation_id = _open_start_recovery(tmp_path, docker)

    request = StackStartRequest()

    with pytest.raises(LifecycleError):
        service.start(request)

    raw = json.loads(service.lifecycle_state.journal_path(operation_id).read_text())
    assert set(raw) <= set(LifecycleOperationJournal.model_fields)
    assert raw["step_outputs"]["recovery"]["attempts"] == 1


def test_recovery_releases_rollback_pins_only_after_every_postcondition(
    tmp_path: Path,
) -> None:
    """The override must outlive any check that could resolve its temporary tags."""
    docker = FakeDockerGateway()
    service, _operation_id = _open_start_recovery(tmp_path, docker)
    docker.restore_success = True
    docker.calls.clear()

    service.start(StackStartRequest())

    order = [name for name, _ in docker.calls]
    assert "release_rollback_pins" in order
    release = order.index("release_rollback_pins")
    # every post-restore check that touches Compose happens before the release
    assert order.index("restore") < release
    assert order.index("storage_connectivity") < release
    assert max(i for i, name in enumerate(order) if name == "storage_connectivity") < release


def test_failed_recovery_names_the_substep_and_reports_readiness_ids(
    tmp_path: Path,
) -> None:
    docker = FakeDockerGateway()
    service, _operation_id = _open_start_recovery(tmp_path, docker)
    docker.restore_success = True
    readiness = FakeReadinessService()
    readiness.ready = False
    readiness.failed_ids = ("security_posture", "workflow_knowledge_sync")
    service.readiness_service = readiness
    lines: list[str] = []

    request = StackStartRequest()

    with pytest.raises(LifecycleError) as raised:
        service.start(request, technical_sink=lines.append)

    failure = raised.value
    assert failure.code == "stack_start_recovery_required"
    assert "Readiness was not proven after the exact restore" in failure.remediation
    assert "security_posture" in failure.remediation
    assert failure.details["failed_readiness_ids"] == [
        "security_posture",
        "workflow_knowledge_sync",
    ]
    assert any(line.startswith("recovery step: restore exact prior") for line in lines)
    assert any(line.startswith("recovery failed:") for line in lines)
    # the pins are still released so the next attempt is not blocked by a stale override
    assert ("release_rollback_pins", ()) in docker.calls


def test_recovery_reconciles_derived_readiness_before_polling(tmp_path: Path) -> None:
    docker = FakeDockerGateway()
    reconciled: list[float] = []
    service, _operation_id = _open_start_recovery(tmp_path, docker)
    service.readiness_reconciler = reconciled.append
    docker.restore_success = True

    service.start(StackStartRequest())

    assert reconciled == [37.0]


def test_failed_restore_names_the_service_and_the_mismatch(tmp_path: Path) -> None:
    docker = FakeDockerGateway()
    service, _operation_id = _open_start_recovery(tmp_path, docker)
    docker.restore_success = False
    docker.restore_mismatch_report = (
        "fs-app: expected topology running, observed stopped",
        f"fs-worker: expected image sha256:{'a' * 64}, observed sha256:{'b' * 64}",
    )
    lines: list[str] = []

    request = StackStartRequest()

    with pytest.raises(LifecycleError) as raised:
        service.start(request, technical_sink=lines.append)

    failure = raised.value
    assert "Exact image and topology recovery was not proven." in failure.remediation
    assert "fs-app: expected topology running, observed stopped" in failure.remediation
    assert "fs-worker: expected image sha256:" in failure.remediation
    assert any("fs-app: expected topology running" in line for line in lines)


def test_restore_mismatches_name_topology_and_image_per_service(tmp_path: Path) -> None:
    app_image = f"sha256:{'a' * 64}"
    worker_image = f"sha256:{'b' * 64}"
    snapshot = StackRollbackSnapshot(
        operation_id="operation-start",
        project_name="installed-project",
        selected_services=("fs-app", "fs-worker"),
        services=(
            StackContainerRollbackState(
                service="fs-app",
                topology="running",
                container_id="old-app",
                image_id=app_image,
            ),
            StackContainerRollbackState(
                service="fs-worker",
                topology="running",
                container_id="old-worker",
                image_id=worker_image,
            ),
            StackContainerRollbackState(service="pg-fs", topology="absent"),
        ),
    )
    observed = snapshot.model_copy(
        update={
            "services": (
                snapshot.services[0].model_copy(update={"topology": "stopped"}),
                snapshot.services[1].model_copy(update={"image_id": f"sha256:{'c' * 64}"}),
                snapshot.services[2],
            )
        }
    )
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=_Process([]))
    object.__setattr__(gateway, "capture_rollback", lambda *_args, **_kwargs: observed)

    mismatches = gateway.restore_mismatches(_target(), snapshot)

    assert mismatches == (
        "fs-app: expected topology running, observed stopped",
        f"fs-worker: expected image {worker_image}, observed sha256:{'c' * 64}",
    )
    # an absent service that stayed absent is not a mismatch
    assert not any("pg-fs" in item for item in mismatches)


def test_configuration_recovery_releases_the_rollback_pins_it_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config apply recovery must not leave temporary tags or an override behind."""
    snapshot = StackRollbackSnapshot(
        operation_id="operation-config-apply",
        project_name="installed-project",
        selected_services=("fs-app",),
        services=(
            StackContainerRollbackState(
                service="fs-app",
                topology="running",
                container_id="old-app",
                image_id=f"sha256:{'a' * 64}",
            ),
        ),
    )
    docker = DockerLifecycleGateway(project_root=tmp_path, process=_Process([]))
    released: list[str] = []
    monkeypatch.setattr(docker, "restore", lambda *_a, **_k: True)
    monkeypatch.setattr(
        docker,
        "release_rollback_pins",
        lambda captured: released.append(captured.operation_id) or (),
    )
    gateway = ConfigurationLifecycleGateway(
        project_root=tmp_path,
        docker=docker,
        preservation=cast(Any, None),
        manifest_store=cast(Any, None),
        readiness_service=cast(Any, None),
        release_provider=lambda: "1.2.3",
    )
    monkeypatch.setattr(gateway, "_restore_managed_files", lambda _id: None)
    monkeypatch.setattr(gateway, "_managed_files_match", lambda _id: True)

    assert (
        gateway.restore_recovery(
            _target(),
            operation_id="operation-config-apply",
            snapshot=snapshot.model_dump(mode="json"),
        )
        is True
    )
    assert released == ["operation-config-apply"]


@pytest.mark.parametrize("candidate_only", [True, False])
def test_a_restore_takes_the_candidates_own_services_away_before_it_starts_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, candidate_only: bool
) -> None:
    """AC17-R175: behind a host proxy both profiles publish the same backend port. The
    restore started the previous services first and removed the candidate's after, so
    the previous one failed on "port is already allocated", on every retry. The green
    direction: a rollback within one profile has nothing to take away first."""
    image = f"sha256:{'a' * 64}"
    services = [
        StackContainerRollbackState(
            service="fs-worker", topology="running", container_id="worker-1", image_id=image
        )
    ]
    if candidate_only:
        services.append(StackContainerRollbackState(service="fs-app", topology="absent"))
    snapshot = StackRollbackSnapshot(
        operation_id="operation-switch",
        project_name="installed-project",
        selected_services=tuple(item.service for item in services),
        services=tuple(services),
    )
    calls: list[str] = []
    gateway = DockerLifecycleGateway(project_root=tmp_path, process=_Process([]))
    monkeypatch.setattr(gateway, "_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        gateway,
        "_run_compose",
        lambda _target, arguments, **_kwargs: calls.append("compose " + arguments[0]),
    )
    monkeypatch.setattr(
        gateway,
        "_remove_project_services",
        lambda _project, removed, **_kwargs: calls.append("remove " + " ".join(removed)),
    )
    monkeypatch.setattr(gateway, "capture_rollback", lambda *_args, **_kwargs: snapshot)

    restored = gateway.restore(
        _target(),
        action="rebuild",
        snapshot=snapshot,
        timeout_seconds=37,
        output_sink=lambda _line: None,
        heartbeat=lambda _detail: None,
    )

    assert restored is True
    assert calls == (["remove fs-app", "compose up"] if candidate_only else ["compose up"])
