"""Docker configuration application, preservation, and candidate readiness adapters."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from core.application.installer.redaction import redact_installer_text
from core.application.lifecycle.contracts import (
    ComposeTarget,
    DeploymentConfig,
    publishes_host_port,
)
from core.application.lifecycle.errors import CandidateReadinessProbePendingError
from core.application.lifecycle.preservation_service import (
    PreservationManifest,
    PreservationService,
    validate_manifest,
)
from core.application.lifecycle.stack_service import (
    UNKNOWN,
    LifecycleOperationKind,
    StackGatewayError,
    StackRollbackSnapshot,
)
from core.application.readiness import ReadinessArea, ReadinessReport, ReadinessStatus
from core.infrastructure.lifecycle import docker_runtime_support
from core.infrastructure.lifecycle.docker_runtime_support import (
    _IMAGE_PREPARATION_TIMEOUT_SECONDS,
    _RETAINED_MANAGEMENT_SERVICES,
    _TOKEN_INSTALLER_COMPOSE_OVERRIDE_YAML,
    _TRANSIENT_CANDIDATE_READINESS_IDS,
    _TRANSITION_RETAINED_SERVICES,
)
from core.infrastructure.lifecycle.preservation_repository import PreservationManifestStore

if TYPE_CHECKING:  # Preserve annotation-only Docker gateway dependency.
    from core.infrastructure.lifecycle.docker_gateway import DockerLifecycleGateway

from core.infrastructure.lifecycle.configuration_files import (
    _PATH_FLOW_STEWARD,
    _RUNTIME_CERTIFICATE_RELATIVE_PATH,
    _RUNTIME_PRIVATE_KEY_RELATIVE_PATH,
    _candidate_tls_bytes,
    _capture_configuration_files,
    _configuration_files_match,
    _load_configuration_file_snapshot,
    _restore_configuration_files,
    _reverse_proxy_override,
    _secure_atomic_write_bytes,
    _secure_unlink,
    _target_override_content,
)

_CANDIDATE_LOG_TAIL = 40


def _configuration_recovery_selected_services(
    source_target: ComposeTarget,
    services: Sequence[str],
) -> tuple[str, ...]:
    retained = set(_TRANSITION_RETAINED_SERVICES)
    if source_target.file_storage == "bundled":
        retained.add("seaweedfs")
    configured = set(source_target.required_services)
    return tuple(
        service for service in services if service in configured and service not in retained
    )


class PreservationBaselineError(ValueError):
    """A preservation baseline with explicit observations that failed closed."""

    def __init__(self, failed_fields: Sequence[str]) -> None:
        self.failed_fields = tuple(sorted(dict.fromkeys(failed_fields)))
        failed = ", ".join(self.failed_fields)
        super().__init__(f"Pre-apply preservation evidence is incomplete: {failed}")


class CandidateStorageProbe:
    """Validate candidate S3 storage read-only in the current process."""

    def __init__(self, *, session_factory: Callable[..., Any] | None = None) -> None:
        self._session_factory = session_factory

    def validate(self, environment: Mapping[str, str]) -> bool:
        try:
            import boto3
            from botocore import UNSIGNED
            from botocore.config import Config as BotoConfig
            from botocore.exceptions import BotoCoreError, ClientError
        except ImportError:
            return False
        try:
            endpoint = str(environment["ARTIFACT_S3_ENDPOINT_URL"]).strip()
            bucket = str(environment["ARTIFACT_S3_BUCKET"]).strip()
            region = str(environment.get("ARTIFACT_S3_REGION") or "us-east-1").strip()
            access_key = str(environment.get("ARTIFACT_S3_ACCESS_KEY") or "").strip()
            secret_key = str(environment.get("ARTIFACT_S3_SECRET_KEY") or "").strip()
            raw_use_ssl = str(environment.get("ARTIFACT_S3_USE_SSL") or "true").lower()
            if (
                not endpoint
                or not bucket
                or not region
                or raw_use_ssl not in {"true", "false"}
                or bool(access_key) != bool(secret_key)
            ):
                return False
            session_factory = self._session_factory or boto3.Session
            session = session_factory(
                aws_access_key_id=access_key or None,
                aws_secret_access_key=secret_key or None,
                region_name=region,
            )
            config_options: dict[str, Any] = {
                "connect_timeout": 3,
                "read_timeout": 5,
                "retries": {"max_attempts": 0, "mode": "standard"},
                "s3": {"addressing_style": "path"},
            }
            if not access_key:
                config_options["signature_version"] = UNSIGNED
            use_ssl = raw_use_ssl == "true"
            ca_bundle = str(environment.get("AWS_CA_BUNDLE") or "").strip()
            client = session.client(
                "s3",
                endpoint_url=endpoint,
                region_name=region,
                use_ssl=use_ssl,
                verify=(ca_bundle if use_ssl and ca_bundle else use_ssl),
                config=BotoConfig(**config_options),
            )
            client.head_bucket(Bucket=bucket)
        except (KeyError, ValueError, OSError, BotoCoreError, ClientError):
            return False
        return True


def _exception_text(exc: BaseException) -> str:
    text = redact_installer_text(str(exc)).strip()[:300]
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _core_failures_text(report: ReadinessReport) -> str:
    """Each failed core check with the detail it gave, for an operator to act on."""
    parts: list[str] = []
    for finding in report.findings:
        if finding.area is not ReadinessArea.CORE or finding.status is not ReadinessStatus.FAILED:
            continue
        detail = redact_installer_text(finding.detail).strip()[:200]
        parts.append(f"{finding.check_id} ({detail})" if detail else finding.check_id)
    return ", ".join(sorted(parts))


class ConfigurationLifecycleGateway:
    """Compose Docker, preservation, and readiness adapters for config apply."""

    def __init__(
        self,
        *,
        project_root: Path,
        docker: DockerLifecycleGateway,
        preservation: PreservationService,
        manifest_store: PreservationManifestStore,
        readiness_service: Any,
        release_provider: Callable[[], str],
        timeout_seconds: int = 60,
        storage_probe: Callable[[Mapping[str, str]], bool] | None = None,
        candidate_readiness: Callable[[DeploymentConfig], Any] | None = None,
        readiness_clock: Callable[[], float] = time.monotonic,
        readiness_sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._project_root = Path(project_root).resolve()
        self._docker = docker
        self._preservation = preservation
        self._manifest_store = manifest_store
        self._readiness = readiness_service
        self._release_provider = release_provider
        self._timeout_seconds = max(1, int(timeout_seconds))
        self._storage_probe = storage_probe or CandidateStorageProbe().validate
        self._candidate_readiness = candidate_readiness
        self._readiness_clock = readiness_clock
        self._readiness_sleep = readiness_sleep

    def prepare_target_images(
        self,
        config: DeploymentConfig,
        target: ComposeTarget,
        environment: Mapping[str, str],
    ) -> None:
        override_content = _target_override_content(config, target)
        self._docker.prepare_target_images(
            target,
            environment=environment,
            override_content=override_content,
            timeout_seconds=_IMAGE_PREPARATION_TIMEOUT_SECONDS,
        )

    def validate_target(
        self,
        config: DeploymentConfig,
        target: ComposeTarget,
        environment: Mapping[str, str],
        managed_target: ComposeTarget | None = None,
    ) -> None:
        docker_runtime_support.assert_control_directory_safe(self._project_root)
        certificate, private_key = _candidate_tls_bytes(self._project_root, config)
        if (certificate is None) != (private_key is None):
            raise ValueError("candidate TLS material is incomplete")
        override_content = _target_override_content(config, target)
        self._docker.validate_target(
            target,
            environment=environment,
            override_content=override_content,
        )
        if config.file_storage == "external_s3" and not self._storage_probe(environment):
            raise ValueError("candidate storage read-only validation failed")
        if not publishes_host_port(config):
            # P2: behind an existing Traefik, or a proxy in Docker, the public port is
            # that component's; Flow Steward binds none, so there is nothing to probe.
            return
        # Local/provided TLS intentionally validates the public listener.
        bind_host, bind_port = (
            (config.reverse_proxy_backend_host, config.reverse_proxy_backend_port)
            if config.tls_mode == "reverse_proxy"
            else ("0.0.0.0", config.public_port)  # nosec B104
        )
        protocols = ("tcp",) if config.tls_mode == "reverse_proxy" else ("tcp", "udp")
        for protocol in protocols:
            if not self._docker.port_available_for_transition(
                managed_target or target,
                bind_host,
                bind_port,
                protocol=protocol,
            ):
                raise ValueError(
                    f"target {protocol} port is unavailable outside the prior managed stack"
                )

    def capture_recovery(
        self,
        previous_target: ComposeTarget,
        candidate_target: ComposeTarget,
        *,
        operation_id: str,
    ) -> Mapping[str, object]:
        if previous_target.project_name != candidate_target.project_name:
            raise ValueError("Configuration recovery targets use different project identities")
        self._capture_managed_files(operation_id)
        managed = tuple(
            service
            for service in dict.fromkeys(
                (*previous_target.required_services, *candidate_target.required_services)
            )
            if service not in _RETAINED_MANAGEMENT_SERVICES
        )
        selected = _configuration_recovery_selected_services(previous_target, managed)
        snapshot = self._docker.capture_rollback(
            previous_target,
            operation_id=operation_id,
            selected_services=selected,
            managed_services=managed,
        )
        return snapshot.model_dump(mode="json")

    def recovery_available(self, operation_id: str) -> bool:
        try:
            self._load_file_snapshot(operation_id)
        except (OSError, ValueError):
            return False
        return True

    def apply_artifacts(self, config: DeploymentConfig, *, operation_id: str) -> None:
        del operation_id
        if config.tls_mode == "reverse_proxy":
            _secure_atomic_write_bytes(
                self._project_root,
                _PATH_FLOW_STEWARD,
                _TOKEN_INSTALLER_COMPOSE_OVERRIDE_YAML,
                _reverse_proxy_override(config).encode("utf-8"),
                mode=0o600,
            )
            return
        _secure_unlink(
            self._project_root,
            _PATH_FLOW_STEWARD,
            _TOKEN_INSTALLER_COMPOSE_OVERRIDE_YAML,
        )
        certificate, private_key = _candidate_tls_bytes(self._project_root, config)
        if certificate is None or private_key is None:
            raise ValueError("TLS material staging did not produce a certificate and key")
        _secure_atomic_write_bytes(
            self._project_root,
            ".ssl",
            "cert.pem",
            certificate,
            mode=0o644,
        )
        _secure_atomic_write_bytes(
            self._project_root,
            ".ssl",
            "key.pem",
            private_key,
            mode=0o600,
        )

    def artifacts_match(self, config: DeploymentConfig) -> bool:
        """Prove the candidate-owned runtime files are already installed exactly."""
        override = self._project_root / ".flow-steward/installer.compose.override.yaml"
        certificate_path = self._project_root / _RUNTIME_CERTIFICATE_RELATIVE_PATH
        private_key_path = self._project_root / _RUNTIME_PRIVATE_KEY_RELATIVE_PATH
        try:
            if (
                override.is_symlink()
                or certificate_path.is_symlink()
                or private_key_path.is_symlink()
            ):
                return False
            if config.tls_mode == "reverse_proxy":
                return override.is_file() and override.read_bytes() == (
                    _reverse_proxy_override(config).encode("utf-8")
                )
            if override.exists():
                return False
            certificate, private_key = _candidate_tls_bytes(self._project_root, config)
            return bool(
                certificate is not None
                and private_key is not None
                and certificate_path.is_file()
                and private_key_path.is_file()
                and certificate_path.read_bytes() == certificate
                and private_key_path.read_bytes() == private_key
            )
        except (OSError, ValueError):
            return False

    def restore_recovery(
        self,
        target: ComposeTarget,
        *,
        operation_id: str,
        snapshot: Mapping[str, object],
        output_sink: Callable[[str], None] | None = None,
    ) -> bool:
        try:
            runtime = StackRollbackSnapshot.model_validate(snapshot)
            runtime = runtime.model_copy(
                update={
                    "selected_services": _configuration_recovery_selected_services(
                        target,
                        runtime.selected_services,
                    )
                }
            )
            self._restore_managed_files(operation_id)
            try:
                restored = self._docker.restore(
                    target,
                    action="rebuild",
                    snapshot=runtime,
                    timeout_seconds=self._timeout_seconds,
                    # AC17-R167: into the operation log; it used to be dropped.
                    output_sink=output_sink or (lambda _line: None),
                    heartbeat=lambda _detail: None,
                )
                return bool(restored and self._managed_files_match(operation_id))
            finally:
                # restore() no longer releases its own pins, so this path has to, or a
                # configuration recovery leaves temporary tags and a rollback override
                # behind for good.
                self._docker.release_rollback_pins(runtime)
        except (OSError, ValueError, StackGatewayError):
            return False

    def _capture_managed_files(self, operation_id: str) -> None:
        _capture_configuration_files(self._project_root, operation_id)

    def _load_file_snapshot(self, operation_id: str) -> dict[str, object]:
        return _load_configuration_file_snapshot(self._project_root, operation_id)

    def _restore_managed_files(self, operation_id: str) -> None:
        _restore_configuration_files(self._project_root, operation_id)

    def _managed_files_match(self, operation_id: str) -> bool:
        return _configuration_files_match(self._project_root, operation_id)

    def capture_manifest(
        self,
        config: DeploymentConfig,
        target: ComposeTarget,
        *,
        operation_id: str,
        stage: Literal["before", "after"],
    ) -> Mapping[str, object]:
        manifest = self._preservation.capture(
            operation_id=operation_id,
            stage=stage,
            compose_project=target.project_name,
            release=str(self._release_provider() or "").strip() or UNKNOWN,
            environment=config.environment,
            storage_backend=config.file_storage,
            storage_connectivity=self._docker.storage_connectivity(target),
        )
        self._manifest_store.save(manifest)
        return manifest.to_dict()

    def validate_preservation_baseline(self, manifest: Mapping[str, object]) -> None:
        before = _preservation_manifest_from_mapping(manifest)
        after = PreservationManifest(
            operation_id=before.operation_id,
            stage="after",
            database_reachable=before.database_reachable,
            fields=before.fields,
        )
        comparison = self._preservation.compare(
            before,
            after,
            operation=LifecycleOperationKind.CONFIG_APPLY,
        )
        if not comparison.success:
            raise PreservationBaselineError(tuple(failure.field for failure in comparison.failures))

    def runtime_images_match(
        self,
        target: ComposeTarget,
        snapshot: Mapping[str, object],
    ) -> bool:
        try:
            runtime = StackRollbackSnapshot.model_validate(snapshot)
            return self._docker.runtime_images_match(target, runtime)
        except (ValueError, StackGatewayError):
            return False

    def runtime_image_drift(
        self,
        target: ComposeTarget,
        snapshot: Mapping[str, object],
    ) -> tuple[str, ...]:
        """Each retained container whose image changed: recorded, then running."""
        try:
            runtime = StackRollbackSnapshot.model_validate(snapshot)
            drift = self._docker.runtime_image_drift(target, runtime)
        except (ValueError, StackGatewayError) as exc:
            return (f"the running images could not be read: {_exception_text(exc)}",)
        return tuple(item.describe() for item in drift)

    def load_manifest(
        self,
        operation_id: str,
        stage: Literal["before", "after"],
    ) -> Mapping[str, object] | None:
        manifest = self._manifest_store.load(operation_id, stage)
        return None if manifest is None else manifest.to_dict()

    def activate(
        self,
        previous: DeploymentConfig,
        config: DeploymentConfig,
        previous_target: ComposeTarget,
        target: ComposeTarget,
        runtime_snapshot: Mapping[str, object],
        *,
        source_environment: Mapping[str, str] | None = None,
    ) -> None:
        snapshot = StackRollbackSnapshot.model_validate(runtime_snapshot)
        self._docker.stage_profile_runtime_state(
            previous_target,
            target,
            source_environment=source_environment,
        )
        stop_before = (
            ("fs-app",)
            if previous.tls_mode == "reverse_proxy" and config.tls_mode != "reverse_proxy"
            else ()
        )
        target_managed = set(target.required_services)
        source_only = tuple(
            service
            for service in previous_target.required_services
            if service not in target_managed and service not in _RETAINED_MANAGEMENT_SERVICES
        )
        if source_only:
            self._docker.stop_project_services(
                previous_target.project_name,
                source_only,
                timeout_seconds=self._timeout_seconds,
                output_sink=lambda _line: None,
                heartbeat=lambda _detail: None,
            )
        retained = set(_TRANSITION_RETAINED_SERVICES)
        if previous.file_storage == config.file_storage == "bundled":
            retained.add("seaweedfs")
        reconcile_services = tuple(
            service for service in target.runtime_services if service not in retained
        )
        self._docker.apply_configuration(
            target,
            managed_services=tuple(item.service for item in snapshot.services),
            stop_before_reconcile=stop_before,
            reconcile_services=reconcile_services,
            no_dependencies=True,
            remove_orphans=False,
            timeout_seconds=self._timeout_seconds,
        )

    def healthy(self, config: DeploymentConfig, target: ComposeTarget) -> bool:
        """Return True once the candidate is ready; otherwise raise naming why.

        AC17-R148: seven of the ways this could fail returned a bare False, and `config
        apply` then said only that the target configuration failed -- with the blocking
        finding, the probe error, or the storage probe's own output nowhere. Every
        failure now raises StackGatewayError, whose action and detail the apply error and
        the operation journal carry. Callers asking "does this already hold?" treat the
        raise as False, as they did before.
        """
        deadline = self._readiness_clock() + self._timeout_seconds
        last_failures = ""
        last_transient_probe_detail = ""
        security_reconciled = False
        workflow_knowledge_reconciled = False
        while True:
            if self._readiness_clock() >= deadline:
                reason = (
                    last_failures
                    or last_transient_probe_detail
                    or "no readiness report was received"
                )
                raise StackGatewayError(
                    action="candidate_readiness",
                    detail=f"Candidate readiness timed out: {reason}.",
                )
            try:
                report = (
                    self._candidate_readiness(config)
                    if self._candidate_readiness is not None
                    else self._readiness.check()
                )
            except (OSError, CandidateReadinessProbePendingError) as exc:
                raw_detail = redact_installer_text(str(exc)).strip()
                last_transient_probe_detail = raw_detail[:300] if raw_detail else type(exc).__name__
                remaining = deadline - self._readiness_clock()
                if remaining <= 0:
                    raise StackGatewayError(
                        action="candidate_readiness",
                        detail=(f"Candidate readiness timed out: {last_transient_probe_detail}."),
                    ) from exc
                self._readiness_sleep(min(1.0, remaining))
                continue
            except StackGatewayError:
                raise
            except Exception as exc:
                raise StackGatewayError(
                    action="candidate_readiness",
                    detail=f"Candidate readiness probe failed: {_exception_text(exc)}.",
                ) from exc
            last_transient_probe_detail = ""
            if not isinstance(report, ReadinessReport):
                raise StackGatewayError(
                    action="candidate_readiness",
                    detail=(
                        "Candidate readiness returned no readiness report "
                        f"({type(report).__name__})."
                    ),
                )

            core_failed_ids = {
                finding.check_id
                for finding in report.findings
                if finding.area is ReadinessArea.CORE and finding.status is ReadinessStatus.FAILED
            }
            last_failures = _core_failures_text(report)
            if self._readiness_clock() >= deadline:
                reason = last_failures or "the readiness report arrived at the deadline"
                raise StackGatewayError(
                    action="candidate_readiness",
                    detail=f"Candidate readiness timed out: {reason}.",
                )
            try:
                if "security_posture" in core_failed_ids and not security_reconciled:
                    self._docker.reconcile_security_posture(target)
                    security_reconciled = True
                if (
                    "workflow_knowledge_sync" in core_failed_ids
                    and not workflow_knowledge_reconciled
                ):
                    self._docker.reconcile_workflow_knowledge(target)
                    workflow_knowledge_reconciled = True
            except StackGatewayError:
                raise
            except OSError as exc:
                raise StackGatewayError(
                    action="candidate_readiness",
                    detail=f"Candidate readiness reconciliation failed: {_exception_text(exc)}.",
                ) from exc
            if report.status == "ready":
                if report.core_failed != 0 or core_failed_ids:
                    raise StackGatewayError(
                        action="candidate_readiness",
                        detail=(
                            "Candidate readiness reported ready with failed core checks: "
                            f"{last_failures or report.core_failed}."
                        ),
                    )
                self._require_candidate_storage(target)
                if self._readiness_clock() >= deadline:
                    raise StackGatewayError(
                        action="candidate_readiness",
                        detail="Candidate readiness timed out while storage was being verified.",
                    )
                return True
            if (
                report.status != "blocked"
                or report.core_failed != len(core_failed_ids)
                or not core_failed_ids
                or not core_failed_ids.issubset(_TRANSIENT_CANDIDATE_READINESS_IDS)
            ):
                failed = last_failures or "unknown core readiness check"
                raise StackGatewayError(
                    action="candidate_readiness",
                    detail=f"Candidate readiness failed: {failed}.",
                )
            remaining = deadline - self._readiness_clock()
            if remaining <= 0:
                raise StackGatewayError(
                    action="candidate_readiness",
                    detail=f"Candidate readiness timed out: {last_failures}.",
                )
            self._readiness_sleep(min(1.0, remaining))

    def _require_candidate_storage(self, target: ComposeTarget) -> None:
        try:
            self._docker.require_storage_connectivity(target)
        except StackGatewayError as exc:
            raise StackGatewayError(
                action="storage_readiness",
                detail=f"Candidate storage probe failed: {exc.detail}",
            ) from exc
        except Exception as exc:
            raise StackGatewayError(
                action="storage_readiness",
                detail=f"Candidate storage probe failed: {_exception_text(exc)}.",
            ) from exc

    def candidate_diagnostics(
        self,
        config: DeploymentConfig,
        target: ComposeTarget,
        output_sink: Callable[[str], None],
    ) -> None:
        """Write the candidate primary service's last log lines, redacted, to the sink."""
        service = "compact-runtime" if config.deployment_profile == "compact" else "fs-app"
        output_sink(f"Candidate {service} log before rollback (last {_CANDIDATE_LOG_TAIL} lines):")
        self._docker.logs(
            target,
            service=service,
            tail=_CANDIDATE_LOG_TAIL,
            follow=False,
            output_sink=lambda line: output_sink(redact_installer_text(line)),
        )

    def recovery_healthy(
        self,
        config: DeploymentConfig,
        target: ComposeTarget,
        snapshot: Mapping[str, object],
    ) -> bool:
        """Verify the primary service against its captured pre-mutation topology.

        ``restore_recovery`` has already proved the exact prior container topology
        and image identities. A primary that was stopped must remain stopped; its
        storage is verified immediately afterwards by the disposable probe used by
        ``capture_manifest``. A previously running primary retains its liveness,
        health, and storage checks, and is then waited for until it is ready, through
        the candidate readiness probe: that one allows the running operation, where the
        normal readiness report would block on the recovery itself. Not ready in time
        raises, naming why.
        """
        primary_service = "compact-runtime" if config.deployment_profile == "compact" else "fs-app"
        try:
            expected_snapshot = StackRollbackSnapshot.model_validate(snapshot)
            if expected_snapshot.project_name != target.project_name:
                return False
            expected = [
                item for item in expected_snapshot.services if item.service == primary_service
            ]
            if len(expected) != 1:
                return False
            runtime = self._docker.status(target)
            observed = [item for item in runtime.services if item.service == primary_service]
            topology = expected[0].topology
            if topology == "absent":
                return not observed
            if len(observed) != 1:
                return False
            primary = observed[0]
            running = primary.state.strip().lower() == "running"
            if topology == "stopped":
                return not running
            if not (
                running
                and primary.health.strip().lower() != "unhealthy"
                and self._docker.storage_connectivity(target) is True
            ):
                return False
        except (OSError, ValueError, StackGatewayError):
            return False
        # AC17-R167: running is not back. A failed switch reported the previous runtime
        # restored while it still answered 502, then 503, for another half a minute, and
        # the operator's next command failed in that window. Restored now means ready,
        # by the same readiness the candidate had to reach.
        try:
            return self.healthy(config, target)
        except StackGatewayError as exc:
            raise StackGatewayError(
                action="recovery_readiness",
                detail=f"The restored runtime did not become ready: {exc.detail}",
            ) from exc

    def runtime_release(self, config: DeploymentConfig, target: ComposeTarget) -> str:
        del config
        return self._docker.service_release(target)

    def compare_manifests(
        self,
        before: Mapping[str, object],
        after: Mapping[str, object],
        *,
        profile_transition: bool = False,
    ) -> None:
        comparison = self._preservation.compare(
            _preservation_manifest_from_mapping(before),
            _preservation_manifest_from_mapping(after),
            operation=LifecycleOperationKind.CONFIG_APPLY,
            allow_volume_additions=profile_transition,
        )
        if not comparison.success:
            raise ValueError("Configuration apply preservation checks failed")

    def compare_environment_manifests(
        self,
        before: Mapping[str, object],
        after: Mapping[str, object],
    ) -> None:
        comparison = self._preservation.compare(
            _preservation_manifest_from_mapping(before),
            _preservation_manifest_from_mapping(after),
            operation=LifecycleOperationKind.ENVIRONMENT_SWITCH,
        )
        if not comparison.success:
            raise ValueError("Environment switch preservation checks failed")


def _preservation_manifest_from_mapping(
    value: Mapping[str, object],
) -> PreservationManifest:
    expected = {
        "operation_id",
        "stage",
        "database_reachable",
        "fields",
        "schema_version",
        "captured_at",
    }
    if set(value) != expected:
        raise ValueError("Configuration preservation manifest has an invalid shape")
    stage = value["stage"]
    fields = value["fields"]
    if stage not in {"before", "after"} or not isinstance(fields, Mapping):
        raise ValueError("Configuration preservation manifest has invalid fields")
    if value["schema_version"] != 1:
        raise ValueError("Configuration preservation manifest has an unsupported version")
    manifest = PreservationManifest(
        operation_id=str(value["operation_id"]),
        stage=cast(Literal["before", "after"], stage),
        database_reachable=bool(value["database_reachable"]),
        fields=fields,
        schema_version=1,
        captured_at=str(value["captured_at"]),
    )
    validate_manifest(manifest, expected_stage=manifest.stage)
    return manifest
