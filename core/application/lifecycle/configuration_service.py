"""Application ownership for installed and pending deployment configuration."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

from pydantic import JsonValue, ValidationError

from core.application.lifecycle.configuration_models import (
    ConfigApplyCommandResult as ConfigApplyCommandResult,
)
from core.application.lifecycle.configuration_models import (
    ConfigApplyRequest as ConfigApplyRequest,
)
from core.application.lifecycle.configuration_models import (
    ConfigProxyFragmentRequest as ConfigProxyFragmentRequest,
)
from core.application.lifecycle.configuration_models import (
    ConfigProxyFragmentResult as ConfigProxyFragmentResult,
)
from core.application.lifecycle.configuration_models import (
    ConfigSecretState as ConfigSecretState,
)
from core.application.lifecycle.configuration_models import (
    ConfigSetRequest as ConfigSetRequest,
)
from core.application.lifecycle.configuration_models import (
    ConfigSetResult as ConfigSetResult,
)
from core.application.lifecycle.configuration_models import (
    ConfigShowRequest as ConfigShowRequest,
)
from core.application.lifecycle.configuration_models import (
    ConfigShowResult as ConfigShowResult,
)
from core.application.lifecycle.configuration_models import (
    ConfigurationApplyGateway as ConfigurationApplyGateway,
)
from core.application.lifecycle.configuration_models import (
    ConfigurationState as ConfigurationState,
)
from core.application.lifecycle.configuration_models import (
    ConfigValidateRequest as ConfigValidateRequest,
)
from core.application.lifecycle.configuration_operation import (
    _MESSAGE_FLOW_STEWARD_CONFIG_APPLY,
    _configuration_journal_error,
    _configuration_operation_steps,
    _ConfigurationOperationContext,
    _ConfigurationOperationGateway,
)
from core.application.lifecycle.configuration_rendering import ConfigurationRenderingMixin
from core.application.lifecycle.configuration_validation import (
    _provided_tls_issues,
    _valid_public_host,
)
from core.application.lifecycle.contracts import (
    CancellationToken,
    ComposeTarget,
    ConfigurationPatch,
    ConfigurationValidationResult,
    DeploymentConfig,
    LifecycleLockStatus,
    LifecycleOperationDefinition,
    LifecycleOperationJournal,
    LifecycleOperationKind,
    LifecycleOperationStatus,
)
from core.application.lifecycle.errors import LifecycleError
from core.application.lifecycle.operation_service import LifecycleOperationRunner
from core.application.lifecycle.ports import (
    LifecycleState,
    ProgressSink,
    TechnicalSink,
)
from core.application.management.errors import ServicePreconditionError

_MESSAGE_RETRY_FROM_THE_INSTALLED_FLOW_STEWARD_CLI_RUNTIME = (
    "Retry from the installed Flow Steward CLI runtime."
)


class ConfigurationService(ConfigurationRenderingMixin):
    """Single application service for active and pending installed configuration."""

    def __init__(
        self,
        *,
        store: ConfigurationState,
        gateway: ConfigurationApplyGateway | None = None,
        lifecycle_state: LifecycleState | None = None,
        docker_host_project_root: Path | None = None,
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.lifecycle_state = lifecycle_state
        self.docker_host_project_root = (
            Path(docker_host_project_root).resolve()
            if docker_host_project_root is not None
            else None
        )

    def read_active(self, *, required: bool = True) -> DeploymentConfig | None:
        try:
            config = self.store.read_active()
        except (OSError, ValueError, ValidationError) as exc:
            raise LifecycleError.stable_failure(
                code="configuration_invalid",
                cause="The installed configuration is unreadable or invalid.",
                remediation="Run flow-steward config validate and repair the reported fields.",
                step="configuration",
            ) from exc
        if config is None and required:
            raise LifecycleError.stable_failure(
                code="configuration_unavailable",
                cause="Flow Steward has not been installed.",
                remediation="Run ./install.sh.",
                step="configuration",
            )
        return config

    def read_pending(self) -> ConfigurationPatch | None:
        try:
            return self.store.read_pending()
        except (OSError, ValueError, ValidationError) as exc:
            raise LifecycleError.stable_failure(
                code="pending_configuration_invalid",
                cause="The pending configuration patch is unreadable or invalid.",
                remediation="Run flow-steward config set again with valid values.",
                step="configuration",
            ) from exc

    def proxy_fragment(
        self, request: ConfigProxyFragmentRequest | None = None
    ) -> ConfigProxyFragmentResult:
        """Render the operator's proxy fragment from the configuration already saved.

        AC17-R113: the renderer and its input validation have existed for some time and
        nothing reached them -- `render_proxy_fragment` had no caller in the product at
        all, only two test files, while the operations guide told an operator to open
        the source and call a Python function by hand. The installer offers "existing
        reverse proxy" beside the other two ingress modes, so that instruction is on a
        path the product invites people down.

        No new configuration surface: proxy kind, public host, backend host and ports
        are answers the installation already recorded.
        """
        del request
        active = self.read_active()
        assert active is not None
        if active.ingress_mode != "external_proxy":
            raise ServicePreconditionError(
                "This installation does not front itself with an external proxy.",
                payload={
                    "code": "ingress_mode_is_not_external_proxy",
                    "message": (
                        f"ingress_mode is {active.ingress_mode!r}; a proxy fragment is only "
                        "meaningful for 'external_proxy'."
                    ),
                    "details": {"ingress_mode": active.ingress_mode},
                },
            )
        app_backend, mcp_backend = self._proxy_backends(active)
        from core.application.lifecycle.proxy_renderer import (
            ProxyRenderConfig,
            render_proxy_fragment,
        )

        fragment = render_proxy_fragment(
            ProxyRenderConfig(
                kind=active.proxy_kind,
                deployment_profile=active.deployment_profile,
                public_host=active.public_host,
                app_backend=app_backend,
                mcp_backend=mcp_backend or None,
            )
        )
        return ConfigProxyFragmentResult(
            proxy_kind=active.proxy_kind,
            proxy_placement=active.proxy_placement,
            public_host=active.public_host,
            app_backend=app_backend,
            mcp_backend=mcp_backend,
            fragment=fragment,
        )

    def _proxy_backends(self, active: DeploymentConfig) -> tuple[str, str]:
        """Where the operator's proxy should send traffic, per placement and profile.

        Derived from the ingress overlays rather than assumed: host placement publishes
        the backend ports the operator answered, and docker placement joins the proxy
        network under the aliases infra/docker/*/ingress-external_proxy-docker.yaml
        declares, where the container ports rather than the published ones apply.
        """
        compact = active.deployment_profile == "compact"
        if active.proxy_placement == "host":
            host = active.reverse_proxy_backend_host
            app = f"{host}:{active.reverse_proxy_backend_port}"
            return app, "" if compact else f"{host}:{active.reverse_proxy_mcp_port}"
        from core.infrastructure.runtime_env import runtime_env

        project = str(runtime_env.get("FS_COMPOSE_PROJECT_NAME") or "flow-steward").strip()
        if compact:
            return f"{project}-compact-proxy:{active.runtime_http_port}", ""
        return f"{project}-app-proxy:8099", f"{project}-mcp-proxy:8100"

    def show(self, request: ConfigShowRequest | None = None) -> ConfigShowResult:
        del request
        active = self.read_active()
        assert active is not None
        pending = self.read_pending()
        diff: dict[str, dict[str, JsonValue]] = {}
        if pending is not None:
            # W3 (auditor): both sides as JSON. The active side was the raw attribute, and a
            # tuple (proxy_trusted_ips) is not a JSON value, so `config show` crashed while
            # such a change was pending.
            active_values = active.model_dump(mode="json")
            for field_name, pending_value in pending.model_dump(
                mode="json", exclude_none=True
            ).items():
                diff[field_name] = {
                    "active": active_values.get(field_name),
                    "pending": pending_value,
                }
        statuses = self.store.secret_statuses(active)
        return ConfigShowResult(
            active=active,
            pending=pending,
            pending_diff=diff,
            protected_setting_states=tuple(
                ConfigSecretState(
                    name=name,
                    state="configured" if configured else "missing",
                )
                for name, configured in sorted(statuses.items())
            ),
        )

    def set(self, patch: ConfigurationPatch) -> ConfigSetResult:
        """Atomically replace the pending typed patch without runtime effects."""
        if self.lifecycle_state is None:
            raise LifecycleError.dependency(
                code="configuration_lifecycle_unavailable",
                cause="The durable lifecycle state adapter is unavailable.",
                remediation=_MESSAGE_RETRY_FROM_THE_INSTALLED_FLOW_STEWARD_CLI_RUNTIME,
                step="runtime",
            )
        existing = self._resumable_apply_journal()
        if existing is not None:
            raise _configuration_journal_error(
                operation_id=existing.operation_id,
                cause="Pending configuration cannot change while an apply journal is unfinished.",
                recovery_command=existing.recovery_command,
            )
        operation_id = f"config-set-{uuid4().hex}"
        with self.lifecycle_state.operation_lock(operation_id, mutating=True):
            existing = self._resumable_apply_journal(diagnose_lock=False)
            if existing is not None:
                raise _configuration_journal_error(
                    operation_id=existing.operation_id,
                    cause="Pending configuration cannot change while an apply journal is unfinished.",
                    recovery_command=existing.recovery_command,
                )
            active = self.read_active()
            assert active is not None
            self._candidate(active, patch)
            self.store.write_pending(patch)
        return ConfigSetResult(pending=patch)

    stage_patch = set

    def validate(
        self, request: ConfigValidateRequest | None = None
    ) -> ConfigurationValidationResult:
        del request
        active = self.read_active()
        assert active is not None
        pending = self.read_pending()
        active_target = self.derive_target(active)
        issues = list(self._validate_one("active", active, managed_target=active_target))
        if pending is not None:
            issues.extend(
                self._validate_one(
                    "pending",
                    self._candidate(active, pending),
                    managed_target=active_target,
                )
            )
        return ConfigurationValidationResult(valid=not issues, issues=tuple(issues))

    def _validate_one(
        self,
        label: str,
        config: DeploymentConfig,
        *,
        managed_target: ComposeTarget,
    ) -> tuple[str, ...]:
        issues = [f"{label}: {issue}" for issue in self.validate_config(config).issues]
        if self.gateway is None:
            issues.append(f"{label}: configuration runtime adapter is unavailable")
            return tuple(issues)
        try:
            target, _path, environment = self._prepared_environment(config)
            self.gateway.validate_target(
                config,
                target,
                environment,
                managed_target=managed_target,
            )
        except Exception as exc:
            issues.append(f"{label}: {str(exc).strip() or type(exc).__name__}")
        return tuple(issues)

    def plan_apply(self, request: ConfigApplyRequest) -> dict[str, object]:
        del request
        active = self.read_active()
        assert active is not None
        pending = self.read_pending()
        if pending is None:
            raise LifecycleError.stable_failure(
                code="nothing_to_apply",
                cause="There is no pending configuration patch to apply.",
                remediation="Run flow-steward config set first.",
                step="configuration",
            )
        candidate = self._candidate(active, pending)
        validation = self.validate_config(candidate)
        if not validation.valid:
            raise LifecycleError.stable_failure(
                code="configuration_invalid",
                cause="The pending configuration is invalid: " + "; ".join(validation.issues),
                remediation="Correct the pending values before applying them.",
                step="validation",
            )
        return {
            "pending_fields": sorted(pending.model_dump(exclude_none=True)),
            "runtime_change": True,
            "compose_project": self.derive_target(candidate).project_name,
            "preservation": "required",
            "rollback": "exact previous configuration and runtime",
        }

    def validate_config(self, config: DeploymentConfig) -> ConfigurationValidationResult:
        issues: list[str] = []
        if not config.public_host.strip():
            issues.append("public_host must not be empty")
        elif not _valid_public_host(config.public_host):
            issues.append("public_host must be a strict DNS name or IP address")
        if config.file_storage == "external_s3" and not config.s3_endpoint.strip():
            issues.append("external_s3 requires a configured endpoint")
        if config.tls_mode == "provided":
            paths: dict[str, Path] = {}
            for label, raw_path in (
                ("certificate", config.certificate_path),
                ("private key", config.private_key_path),
            ):
                path = self.store.resolve_project_path(raw_path)
                if not path.is_file():
                    issues.append(f"provided TLS {label} path is unavailable: {raw_path}")
                else:
                    paths[label] = path
            if len(paths) == 2:
                issues.extend(
                    _provided_tls_issues(
                        config.public_host,
                        cert_path=paths["certificate"],
                        key_path=paths["private key"],
                    )
                )
        return ConfigurationValidationResult(valid=not issues, issues=tuple(issues))

    def write_environment(self, config: DeploymentConfig) -> Path:
        _target, path, managed = self._prepared_environment(config)
        self.store.write_runtime_environment(path, managed)
        return path

    def prepare_environment(
        self,
        config: DeploymentConfig,
    ) -> tuple[ComposeTarget, Path, dict[str, str]]:
        """Prepare candidate Compose/environment values without writing them."""
        return self._prepared_environment(config)

    def prepare_transition_environment(
        self,
        active: DeploymentConfig,
        candidate: DeploymentConfig,
        target_overlay: Mapping[str, str] | None = None,
    ) -> tuple[ComposeTarget, Path, dict[str, str]]:
        """Render a transition candidate from the active durable environment only."""
        return self._prepared_environment(
            candidate,
            source=active,
            environment_overlay=target_overlay,
        )

    def stage_environment(
        self,
        config: DeploymentConfig,
        prepared: tuple[ComposeTarget, Path, dict[str, str]],
    ) -> Path:
        """Write only the candidate environment; keep active selection unchanged."""
        del config
        _target, path, managed = prepared
        self.store.write_runtime_environment(path, managed)
        return path

    def stage_transition_environment(
        self,
        active: DeploymentConfig,
        candidate: DeploymentConfig,
        prepared: tuple[ComposeTarget, Path, dict[str, str]],
    ) -> Path:
        """Stage a candidate using protected source lines from the active environment."""
        del candidate
        _target, path, managed = prepared
        self.store.write_transition_runtime_environment(
            path,
            managed,
            source_path=self.store.env_path(active),
        )
        return path

    def transition_environment_matches(
        self,
        active: DeploymentConfig,
        candidate: DeploymentConfig,
        prepared: tuple[ComposeTarget, Path, dict[str, str]],
    ) -> bool:
        """Check that a staged target is the exact active-source rendering."""
        del candidate
        _target, path, managed = prepared
        return self.store.transition_runtime_environment_matches(
            path,
            managed,
            source_path=self.store.env_path(active),
            allow_installed_storage_credentials=True,
        )

    def mark_active(
        self,
        previous: DeploymentConfig,
        config: DeploymentConfig,
        prepared: tuple[ComposeTarget, Path, dict[str, str]],
    ) -> None:
        """Idempotently publish all canonical active files and a final revision marker."""
        target, path, managed = prepared
        compose_values = self._active_compose_values(config, target)
        revision = self.store.begin_active_publication(
            previous,
            config,
            target_env_path=path,
            compose_values=compose_values,
        )
        self.store.write_active(config)
        self.store.write_runtime_environment(path, managed)
        self.store.write_compose_environment(compose_values)
        self.store.commit_active_publication(
            config,
            revision=revision,
            target_env_path=path,
        )

    def active_publication_matches(
        self,
        config: DeploymentConfig,
        prepared: tuple[ComposeTarget, Path, dict[str, str]],
    ) -> bool:
        _target, path, _managed = prepared
        return self.store.active_publication_matches(config) and path == self.store.env_path(config)

    def write_active_configuration(
        self,
        config: DeploymentConfig,
        *,
        prepared: tuple[ComposeTarget, Path, dict[str, str]] | None = None,
    ) -> ComposeTarget:
        target, path, managed = prepared or self._prepared_environment(config)
        self.store.write_active(config)
        self.store.write_runtime_environment(path, managed)
        self.store.write_compose_environment(self.compose_environment(config, target))
        return target

    def apply(
        self,
        request: ConfigApplyRequest | None = None,
        *,
        progress_sink: ProgressSink | None = None,
        technical_sink: TechnicalSink | None = None,
        cancellation: CancellationToken | None = None,
    ) -> ConfigApplyCommandResult:
        del request
        if self.lifecycle_state is None:
            raise LifecycleError.dependency(
                code="configuration_lifecycle_unavailable",
                cause="The durable lifecycle state adapter is unavailable.",
                remediation=_MESSAGE_RETRY_FROM_THE_INSTALLED_FLOW_STEWARD_CLI_RUNTIME,
                step="runtime",
            )
        return self._apply_lifecycle(
            progress_sink=progress_sink,
            technical_sink=technical_sink,
            cancellation=cancellation,
        )

    apply_pending = apply

    def _apply_lifecycle(
        self,
        *,
        progress_sink: ProgressSink | None,
        technical_sink: TechnicalSink | None,
        cancellation: CancellationToken | None,
    ) -> ConfigApplyCommandResult:
        if self.gateway is None:
            raise LifecycleError.dependency(
                code="configuration_runtime_unavailable",
                cause="The configuration runtime adapter is unavailable.",
                remediation=_MESSAGE_RETRY_FROM_THE_INSTALLED_FLOW_STEWARD_CLI_RUNTIME,
                step="runtime",
            )
        assert self.lifecycle_state is not None
        existing = self._resumable_apply_journal()
        before_manifest: Mapping[str, object] | None = None
        runtime_snapshot: Mapping[str, object] | None = None
        if existing is None:
            active = self.read_active()
            assert active is not None
            selected_pending = self.store.read_pending_with_revision()
            if selected_pending is None:
                raise LifecycleError.stable_failure(
                    code="nothing_to_apply",
                    cause="There is no pending configuration patch to apply.",
                    remediation="Run flow-steward config set first.",
                    step="configuration",
                )
            pending, pending_revision = selected_pending
            candidate = self._candidate(active, pending)
            operation_id = uuid4().hex
            pending_fields = sorted(pending.model_dump(mode="json", exclude_none=True))
        else:
            (
                active,
                candidate,
                operation_id,
                pending_fields,
                pending_revision,
                before_manifest,
                runtime_snapshot,
            ) = self._resume_apply_evidence(existing)
        validation = self.validate_config(candidate)
        if not validation.valid:
            raise LifecycleError.stable_failure(
                code="configuration_invalid",
                cause="The pending configuration is invalid: " + "; ".join(validation.issues),
                remediation="Correct the pending values before applying them.",
                step="validation",
            )
        previous_environment = self._prepared_environment(active)[2]
        prepared = self._prepared_environment(candidate)
        target = prepared[0]
        previous_target = self.derive_target(active)
        context = _ConfigurationOperationContext(
            operation_id=operation_id,
            service=self,
            previous=active,
            candidate=candidate,
            previous_target=previous_target,
            target=target,
            prepared=prepared,
            previous_environment=previous_environment,
            lifecycle_state=self.lifecycle_state,
            durable_completed_steps=existing.completed_steps if existing is not None else (),
            before_manifest=before_manifest,
            runtime_snapshot=runtime_snapshot,
            pending_revision=pending_revision,
        )
        gateway = _ConfigurationOperationGateway(
            adapter=self.gateway,
            context=context,
        )
        if existing is not None and existing.status is LifecycleOperationStatus.RECOVERY_REQUIRED:
            return self._recover_existing_apply(existing, gateway)
        definition = LifecycleOperationDefinition(
            operation_id=operation_id,
            kind=LifecycleOperationKind.CONFIG_APPLY,
            steps=_configuration_operation_steps(),
            parameters={
                "candidate": candidate.model_dump(mode="json"),
                "project_name": target.project_name,
                "pending_fields": pending_fields,
                "pending_revision": pending_revision,
            },
            previous_config={
                "configuration": active.model_dump(mode="json"),
                "pending_revision": pending_revision,
            },
            recovery_command=_MESSAGE_FLOW_STEWARD_CONFIG_APPLY,
        )
        operation = LifecycleOperationRunner(
            gateway=gateway,
            state=self.lifecycle_state,
        ).execute(
            definition,
            progress_sink=progress_sink,
            technical_sink=technical_sink,
            cancellation=cancellation,
        )
        return ConfigApplyCommandResult(
            active=candidate,
            operation_id=operation.operation_id,
            journal_path=operation.journal_path,
            log_path=operation.log_path,
            snapshot_path=operation.previous_config_path or "",
            preservation_verified=gateway.preservation_verified,
            runtime_restored=False,
        )

    def _resume_apply_evidence(
        self, existing: LifecycleOperationJournal
    ) -> tuple[
        DeploymentConfig,
        DeploymentConfig,
        str,
        list[str],
        str,
        Mapping[str, object] | None,
        Mapping[str, object] | None,
    ]:
        assert self.gateway is not None
        operation_id = existing.operation_id
        snapshot = self._load_apply_snapshot(existing)
        try:
            active = DeploymentConfig.model_validate(snapshot["configuration"])
            raw_candidate = existing.parameters.get("candidate")
            if not isinstance(raw_candidate, dict):
                raise ValueError("candidate configuration is absent")
            candidate = DeploymentConfig.model_validate(raw_candidate)
        except (TypeError, ValueError, ValidationError) as exc:
            raise _configuration_journal_error(
                operation_id=operation_id,
                cause=(
                    "The interrupted apply journal contains invalid configuration "
                    "recovery evidence."
                ),
                recovery_command=existing.recovery_command,
            ) from exc
        raw_pending_fields = existing.parameters.get("pending_fields")
        if not isinstance(raw_pending_fields, list) or not all(
            isinstance(item, str) for item in raw_pending_fields
        ):
            raise _configuration_journal_error(
                operation_id=operation_id,
                cause="The interrupted apply journal is missing its pending field inventory.",
                recovery_command=existing.recovery_command,
            )
        pending_fields = [str(item) for item in raw_pending_fields]
        raw_pending_revision = existing.parameters.get("pending_revision")
        if not isinstance(raw_pending_revision, str) or not raw_pending_revision:
            raise _configuration_journal_error(
                operation_id=operation_id,
                cause="The interrupted apply journal is missing its pending revision.",
                recovery_command=existing.recovery_command,
            )
        pending_revision = raw_pending_revision
        before_manifest, runtime_snapshot = self._resume_preservation_evidence(existing, snapshot)
        return (
            active,
            candidate,
            operation_id,
            pending_fields,
            pending_revision,
            before_manifest,
            runtime_snapshot,
        )

    def _resume_preservation_evidence(
        self, existing: LifecycleOperationJournal, snapshot: Mapping[str, object]
    ) -> tuple[Mapping[str, object] | None, Mapping[str, object] | None]:
        assert self.gateway is not None
        operation_id = existing.operation_id
        runtime_snapshot: Mapping[str, object] | None = None
        raw_runtime_snapshot = snapshot.get("runtime")
        if isinstance(raw_runtime_snapshot, dict):
            runtime_snapshot = raw_runtime_snapshot
        try:
            before_manifest = self.gateway.load_manifest(operation_id, "before")
        except (OSError, TypeError, ValueError) as exc:
            raise _configuration_journal_error(
                operation_id=operation_id,
                cause="The interrupted apply preservation evidence is unreadable.",
                recovery_command=existing.recovery_command,
            ) from exc
        if "capture_before" in existing.completed_steps and before_manifest is None:
            raise _configuration_journal_error(
                operation_id=operation_id,
                cause="The interrupted apply lacks its pre-change preservation manifest.",
                recovery_command=existing.recovery_command,
            )
        if "capture_before" in existing.completed_steps and (
            runtime_snapshot is None or not self.gateway.recovery_available(operation_id)
        ):
            raise _configuration_journal_error(
                operation_id=operation_id,
                cause="The interrupted apply lacks exact file/runtime rollback evidence.",
                recovery_command=existing.recovery_command,
            )
        return before_manifest, runtime_snapshot

    def _resumable_apply_journal(
        self,
        *,
        diagnose_lock: bool = True,
    ) -> LifecycleOperationJournal | None:
        assert self.lifecycle_state is not None
        diagnosed_operation_id: str | None = None
        if diagnose_lock:
            diagnosis = self.lifecycle_state.diagnose_active_lock()
            diagnosed_operation_id = diagnosis.operation_id
            if diagnosis.status is LifecycleLockStatus.ACTIVE:
                raise LifecycleError.conflict(
                    operation_id=diagnosis.operation_id,
                    cause=diagnosis.detail,
                )
        try:
            candidates = self.lifecycle_state.unfinished_journals()
        except (OSError, ValueError) as exc:
            raise _configuration_journal_error(
                operation_id=diagnosed_operation_id or "unknown-operation",
                cause="Lifecycle journals are unreadable and safe resume cannot be proven.",
                recovery_command=(
                    "operator: repair or quarantine unreadable lifecycle journals under "
                    ".flow-steward/operations"
                ),
            ) from exc
        if not candidates:
            return None
        if len(candidates) != 1:
            paths = ", ".join(
                sorted(
                    f".flow-steward/operations/{journal.operation_id}.json"
                    for journal in candidates
                )
            )
            raise _configuration_journal_error(
                operation_id=candidates[0].operation_id,
                cause="Multiple unfinished lifecycle journals require operator reconciliation.",
                recovery_command=(
                    f"operator: reconcile lifecycle journals {paths}; retain exactly one "
                    "unfinished journal"
                ),
            )
        journal = candidates[0]
        if journal.kind is not LifecycleOperationKind.CONFIG_APPLY:
            raise _configuration_journal_error(
                operation_id=journal.operation_id,
                cause="An unfinished lifecycle operation has a different action.",
                recovery_command=journal.recovery_command,
            )
        return journal

    def _load_apply_snapshot(self, journal: LifecycleOperationJournal) -> dict[str, object]:
        assert self.lifecycle_state is not None
        try:
            snapshot = self.lifecycle_state.load_previous_config(journal.operation_id)
        except (OSError, ValueError) as exc:
            raise _configuration_journal_error(
                operation_id=journal.operation_id,
                cause="Saved previous configuration evidence is unreadable.",
                recovery_command=journal.recovery_command,
            ) from exc
        if snapshot is None or not isinstance(snapshot.get("configuration"), dict):
            raise _configuration_journal_error(
                operation_id=journal.operation_id,
                cause="The interrupted apply lacks its exact previous configuration snapshot.",
                recovery_command=journal.recovery_command,
            )
        return snapshot

    def _recover_existing_apply(
        self,
        journal: LifecycleOperationJournal,
        gateway: _ConfigurationOperationGateway,
    ) -> ConfigApplyCommandResult:
        assert self.lifecycle_state is not None
        with self.lifecycle_state.operation_lock(journal.operation_id, mutating=True):
            log = self.lifecycle_state.open_log(journal.operation_id)
            try:
                gateway.recover(
                    output_sink=log.write,
                    heartbeat=lambda _detail: None,
                )
                recovered = journal.model_copy(
                    update={
                        "status": LifecycleOperationStatus.FAILED,
                        "current_step": None,
                        "failed_step": "recovery",
                        "last_error_code": "configuration_recovery_completed",
                        "retryable": False,
                        "log_path": str(log.path),
                    }
                )
                self.lifecycle_state.save_journal(recovered)
            except LifecycleError as exc:
                exc.operation_id = journal.operation_id
                exc.log_path = str(log.path)
                raise
            finally:
                log.close()
        return ConfigApplyCommandResult(
            active=gateway.context.previous,
            operation_id=journal.operation_id,
            journal_path=str(self.lifecycle_state.journal_path(journal.operation_id)),
            log_path=str(log.path),
            snapshot_path=journal.previous_config_path or "",
            preservation_verified=gateway.preservation_verified,
            runtime_restored=True,
        )

    @staticmethod
    def _candidate(active: DeploymentConfig, patch: ConfigurationPatch) -> DeploymentConfig:
        values = active.model_dump(mode="json")
        changes = patch.model_dump(mode="json", exclude_none=True)
        if (
            changes.get("deployment_profile", values["deployment_profile"])
            != values["deployment_profile"]
        ):
            # The documented `config apply` switch copies the trees into the target
            # profile's own place, so they are re-derived from it -- unless the patch
            # names the placement, which only ./install.sh --deployment-profile writes
            # (item 5, step 4): it keeps the trees where they are.
            values.pop("data_placement", None)
        values.update(changes)
        return DeploymentConfig.model_validate(values)
