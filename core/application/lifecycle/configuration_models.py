"""Contracts for installed deployment configuration and its technical effects."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from core.application.lifecycle.contracts import (
    ComposeTarget,
    ConfigurationPatch,
    DeploymentConfig,
)
from core.application.lifecycle.ports import (
    TechnicalSink,
)


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConfigShowRequest(_ClosedModel):
    """Read active and pending configuration without mutation."""


class ConfigProxyFragmentRequest(_ClosedModel):
    """Render the reverse-proxy fragment for the installed configuration."""


class ConfigProxyFragmentResult(_ClosedModel):
    """The fragment an operator pastes into their own proxy, and what it assumes."""

    proxy_kind: Literal["nginx", "apache"]
    proxy_placement: Literal["host", "docker"]
    public_host: str
    app_backend: str
    mcp_backend: str = ""
    fragment: str


class ConfigValidateRequest(_ClosedModel):
    """Validate active and pending configuration without mutation."""


class ConfigSetRequest(ConfigurationPatch):
    """Closed declarative patch accepted by the public config command."""

    @model_validator(mode="after")
    def refuse_data_placement(self) -> ConfigSetRequest:
        if self.data_placement is not None:
            raise ValueError(
                "data_placement is chosen by ./install.sh --deployment-profile, which switches "
                "the profile and keeps the data where it is; it is not set by hand."
            )
        return self


class ConfigApplyRequest(_ClosedModel):
    """Apply the sole pending declarative patch."""


class ConfigSecretState(_ClosedModel):
    """One secret name with presence only, never its value."""

    name: str
    state: Literal["configured", "missing"]


class ConfigShowResult(_ClosedModel):
    """Secret-free active configuration, pending patch, and exact diff."""

    active: DeploymentConfig
    pending: ConfigurationPatch | None = None
    pending_diff: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)
    protected_setting_states: tuple[ConfigSecretState, ...] = ()


class ConfigSetResult(_ClosedModel):
    """The secret-free patch staged for later apply."""

    pending: ConfigurationPatch


class ConfigApplyCommandResult(_ClosedModel):
    """Public successful result from the shared lifecycle operation."""

    active: DeploymentConfig
    operation_id: str
    journal_path: str
    log_path: str
    snapshot_path: str = ""
    preservation_verified: bool = True
    runtime_restored: bool = False


class ConfigurationState(Protocol):
    pending_path: Path

    def read_active(self) -> DeploymentConfig | None: ...

    def write_active(self, config: DeploymentConfig) -> None: ...

    def read_pending(self) -> ConfigurationPatch | None: ...

    def read_pending_with_revision(self) -> tuple[ConfigurationPatch, str] | None: ...

    def write_pending(self, patch: ConfigurationPatch) -> None: ...

    def clear_pending(self) -> None: ...

    def pending_revision_matches(self, revision: str) -> bool: ...

    def clear_pending_if_revision(self, revision: str) -> bool: ...

    def save_snapshot(self, operation_id: str, config: DeploymentConfig) -> Path: ...

    def env_path(self, config: DeploymentConfig) -> Path: ...

    def read_env_values(self, path: Path) -> dict[str, str]: ...

    def read_durable_environment(self, config: DeploymentConfig) -> dict[str, str]: ...

    def build_environment(
        self,
        config: DeploymentConfig,
        existing: Mapping[str, str],
    ) -> dict[str, str]: ...

    def write_runtime_environment(self, path: Path, managed: Mapping[str, str]) -> None: ...

    def write_transition_runtime_environment(
        self,
        path: Path,
        managed: Mapping[str, str],
        *,
        source_path: Path,
    ) -> None: ...

    def transition_runtime_environment_matches(
        self,
        path: Path,
        managed: Mapping[str, str],
        *,
        source_path: Path,
        allow_installed_storage_credentials: bool = False,
    ) -> bool: ...

    def write_compose_environment(self, values: Mapping[str, str]) -> None: ...

    def begin_active_publication(
        self,
        previous: DeploymentConfig,
        candidate: DeploymentConfig,
        *,
        target_env_path: Path,
        compose_values: Mapping[str, str],
    ) -> str: ...

    def commit_active_publication(
        self,
        candidate: DeploymentConfig,
        *,
        revision: str,
        target_env_path: Path,
    ) -> None: ...

    def active_publication_matches(self, candidate: DeploymentConfig) -> bool: ...

    def project_name(self, config: DeploymentConfig) -> str: ...

    def transition_project_name(self, active: DeploymentConfig) -> str: ...

    def resolve_project_path(self, raw_path: str) -> Path: ...

    def secret_statuses(self, config: DeploymentConfig) -> Mapping[str, bool]: ...


class ConfigurationApplyGateway(Protocol):
    """Technical effects orchestrated by ``config apply``."""

    def validate_target(
        self,
        config: DeploymentConfig,
        target: ComposeTarget,
        environment: Mapping[str, str],
        managed_target: ComposeTarget | None = None,
    ) -> None: ...

    def capture_recovery(
        self,
        previous_target: ComposeTarget,
        candidate_target: ComposeTarget,
        *,
        operation_id: str,
    ) -> Mapping[str, object]: ...

    def recovery_available(self, operation_id: str) -> bool: ...

    def apply_artifacts(self, config: DeploymentConfig, *, operation_id: str) -> None: ...

    def restore_recovery(
        self,
        target: ComposeTarget,
        *,
        operation_id: str,
        snapshot: Mapping[str, object],
        output_sink: TechnicalSink | None = None,
    ) -> bool: ...

    def capture_manifest(
        self,
        config: DeploymentConfig,
        target: ComposeTarget,
        *,
        operation_id: str,
        stage: Literal["before", "after"],
    ) -> Mapping[str, object]: ...

    def validate_preservation_baseline(self, manifest: Mapping[str, object]) -> None: ...

    def runtime_images_match(
        self,
        target: ComposeTarget,
        snapshot: Mapping[str, object],
    ) -> bool: ...

    def runtime_image_drift(
        self,
        target: ComposeTarget,
        snapshot: Mapping[str, object],
    ) -> tuple[str, ...]: ...

    def load_manifest(
        self,
        operation_id: str,
        stage: Literal["before", "after"],
    ) -> Mapping[str, object] | None: ...

    def activate(
        self,
        previous: DeploymentConfig,
        config: DeploymentConfig,
        previous_target: ComposeTarget,
        target: ComposeTarget,
        runtime_snapshot: Mapping[str, object],
        *,
        source_environment: Mapping[str, str] | None = None,
    ) -> None: ...

    def healthy(self, config: DeploymentConfig, target: ComposeTarget) -> bool: ...

    def candidate_diagnostics(
        self,
        config: DeploymentConfig,
        target: ComposeTarget,
        output_sink: TechnicalSink,
    ) -> None: ...

    def recovery_healthy(
        self,
        config: DeploymentConfig,
        target: ComposeTarget,
        snapshot: Mapping[str, object],
    ) -> bool: ...

    def compare_manifests(
        self,
        before: Mapping[str, object],
        after: Mapping[str, object],
        *,
        profile_transition: bool = False,
    ) -> None: ...
