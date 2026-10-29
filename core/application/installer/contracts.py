"""Validated contracts for the one-time installer state machine."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.application.lifecycle.contracts import DeploymentConfig, validate_s3_endpoint


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InstallationKind(StrEnum):
    FRESH = "fresh"
    RESUMABLE = "resumable"
    INSTALLED = "installed"
    AMBIGUOUS = "ambiguous"


class InstallationEvidence(_ClosedModel):
    """Read-only facts used to classify an installation before mutation."""

    journal_exists: bool = False
    journal_valid: bool = True
    journal_completed: bool = False
    journal_completed_steps: tuple[str, ...] = ()
    config_exists: bool = False
    env_exists: bool = False
    project_identity: str = ""
    observed_project_identity: str = ""
    resource_inventory_valid: bool = True
    durable_secrets_present: bool = False
    backup_encryption_key_present: bool = False
    protected_backups_exist: bool = False
    owned_containers: tuple[str, ...] = ()
    owned_volumes: tuple[str, ...] = ()

    @property
    def has_owned_resources(self) -> bool:
        return bool(
            self.journal_exists
            or self.config_exists
            or self.env_exists
            or self.owned_containers
            or self.owned_volumes
            or self.protected_backups_exist
        )


class InstallationClassification(_ClosedModel):
    kind: InstallationKind
    reasons: tuple[str, ...] = ()
    resources: tuple[str, ...] = ()


class InstallConfig(DeploymentConfig):
    """Installer-specific name and validation over the shared deployment shape."""

    @field_validator("operator_settings", mode="before")
    @classmethod
    def parse_operator_settings_text(cls, value: Any) -> dict[str, str]:
        """Keep an empty value: on ./install.sh --setting NAME= it resets NAME."""
        from core.application.lifecycle.operator_settings import parse_operator_settings

        return parse_operator_settings(value)

    @model_validator(mode="after")
    def require_external_s3_inputs(self) -> Self:
        if "%" in self.public_host:
            raise ValueError("public_host must not contain an IPv6 scope identifier")
        if self.file_storage == "external_s3" and (
            not self.s3_endpoint.strip() or not self.s3_credentials_path.strip()
        ):
            raise ValueError("external_s3 requires s3_endpoint and s3_credentials_path")
        if self.file_storage == "external_s3":
            parsed_endpoint = validate_s3_endpoint(self.s3_endpoint)
            scheme = parsed_endpoint.scheme.lower()
            development_http = (
                scheme == "http"
                and self.environment == "development"
                and self.allow_insecure_s3_http
            )
            if scheme != "https" and not development_http:
                raise ValueError(
                    "External S3 requires HTTPS; development HTTP requires explicit unsafe opt-in"
                )
        return self


class InstallJournal(_ClosedModel):
    schema_version: Literal[1] = 1
    operation_id: str
    status: Literal["running", "failed", "completed", "recovery_required"] = "running"
    project_identity: str
    config: InstallConfig
    completed_steps: tuple[str, ...] = ()
    failed_step: str | None = None
    created_paths: tuple[str, ...] = ()
    observed_project_identity: str = ""
    observed_containers: tuple[str, ...] = ()
    observed_volumes: tuple[str, ...] = ()
    last_error_code: str | None = None
    retryable: bool = True
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class InstallRunResult(_ClosedModel):
    classification: InstallationKind
    status: Literal["installed", "already_installed", "failed", "ambiguous"]
    product_url: str = ""
    operation_id: str | None = None
    log_path: str | None = None
    bootstrap_token_path: str | None = None
    bootstrap_url: str = ""
    # The complete one-time link, token and all. Excluded from every dump for the same
    # reason BootstrapVerification.token is: `install.sh --json > result.json` is a normal
    # thing to do, and a redirected stdout must not capture a live setup token. The CLI
    # writes this to stderr so the operator still sees a link that works.
    bootstrap_setup_url: str = Field(default="", exclude=True, repr=False)
    bootstrap_instruction: str = ""
    completed_steps: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    # What this run changed on the installation's behalf -- a worker count applied,
    # superseded releases removed -- said on screen, not only in the log (R157, R159).
    changes: tuple[str, ...] = ()
    # AC17-R141: set only when this run replaced the running version. Additive on
    # purpose -- `status` is a closed Literal that JSON consumers switch on, and a new
    # value there would break them where two empty strings cannot.
    updated_from_revision: str = ""
    updated_to_revision: str = ""


class ImageCurrency(_ClosedModel):
    """Whether what an installation runs is what these sources would produce.

    AC17-R141. A rerun of ./install.sh on a completed installation reported "already
    installed" and recreated the runtime on the image from the previous install, so
    an update was reported and not applied. The answer is not a property of the
    journal, which says the installation completed; it is a comparison between the
    sources, what the installer recorded, and what is actually running.
    """

    source_revision: str
    # The revision the running runtime was built from, read from its image label.
    # Empty when the image does not say.
    running_revision: str = ""
    update_required: bool = False
    # True when an earlier run prepared this update and it never reached the runtime
    # -- a schema refusal, most often. The operator may have migrated the database in
    # between, so a passing schema check says nothing about whether this update
    # carried a migration.
    resumes_prepared_update: bool = False
    # Why an update is required, in words the installer log can carry as-is.
    reason: str = ""
    # Image variables set in the shell that ran ./install.sh. They make the images the
    # operator's, which are never rebuilt -- and the runbook's own `export
    # FS_COMPACT_IMAGE=...` made a rollback a silent "already installed" (AC17-R162).
    shell_pins: tuple[str, ...] = ()


class BootstrapVerification(_ClosedModel):
    bootstrap_required: bool
    token_path: str | None = None
    token_expired: bool = False
    # Excluded from every dump so the token cannot reach a log or a serialized result.
    # It exists only to compose the one-time handoff URL the operator actually opens.
    token: str = Field(default="", exclude=True, repr=False)
