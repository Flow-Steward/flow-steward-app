"""Extension runtime foundation models (descriptor-backed releases, installs, and projections)."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _require_text(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _descriptor_sha256(descriptor: dict[str, Any]) -> str:
    canonical = json.dumps(descriptor, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ExtensionReleaseRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    release_id: str = Field(default_factory=lambda: _new_id("pr"))
    extension_id: str
    release_version: str = "1.0.0"
    descriptor_schema_version: str = "extension_descriptor_v1"
    descriptor: dict[str, Any] = Field(default_factory=dict)
    descriptor_sha256: str = ""
    channel: str = "private_byo"
    execution_modes: list[str] = Field(default_factory=list)
    contract_version: str = ""
    provenance: dict[str, Any] = Field(default_factory=dict)
    source_kind: str = "file"
    source_ref: str = ""
    account_id: str | None = None
    created_by: str = ""
    created_at: float = Field(default_factory=time.time)

    @field_validator("release_id", "extension_id", mode="before")
    @classmethod
    def _validate_ids(cls, value: Any, info):
        return _require_text(value, info.field_name)

    @model_validator(mode="after")
    def _validate_descriptor_sha256(self) -> ExtensionReleaseRecord:
        digest = str(self.descriptor_sha256 or "").strip().lower()
        if not digest:
            digest = _descriptor_sha256(dict(self.descriptor or {}))
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("descriptor_sha256 must be a valid sha256 hex digest")
        self.descriptor_sha256 = digest
        return self


class ExtensionArtifactSnapshotRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    release_id: str
    artifact_kind: str
    artifact_id: str
    artifact_ref: str = ""
    publish: bool = True
    snapshot: dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)

    @field_validator("release_id", "artifact_kind", "artifact_id", mode="before")
    @classmethod
    def _validate_required(cls, value: Any, info):
        return _require_text(value, info.field_name)


class EntitlementStatus(StrEnum):
    trial_active = "trial_active"
    active = "active"
    grace_period = "grace_period"
    trial_expired = "trial_expired"
    suspended_nonpayment = "suspended_nonpayment"
    disabled = "disabled"


class ExtensionEntitlementRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entitlement_id: str = Field(default_factory=lambda: _new_id("pe"))
    install_id: str
    account_id: str
    status: EntitlementStatus = EntitlementStatus.trial_active
    trial_ends_at: float | None = None
    grace_ends_at: float | None = None
    suspended_reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    updated_by: str = ""
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @field_validator("install_id", "account_id", mode="before")
    @classmethod
    def _validate_required(cls, value: Any, info):
        return _require_text(value, info.field_name)


class ExtensionUsageEventRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str
    install_id: str
    release_id: str | None = None
    project_id: str | None = None
    connection_id: str | None = None
    event_kind: str
    quantity: float = 1.0
    unit: str = "unit"
    metadata: dict[str, Any] = Field(default_factory=dict)
    occurred_at: float = Field(default_factory=time.time)
    recorded_at: float = Field(default_factory=time.time)

    @field_validator("account_id", "install_id", "event_kind", mode="before")
    @classmethod
    def _validate_required(cls, value: Any, info):
        return _require_text(value, info.field_name)


class GeneratedExtensionJobStatus(StrEnum):
    requested = "requested"
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class GeneratedExtensionJobRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_job_id: str = Field(default_factory=lambda: _new_id("gpj"))
    account_id: str
    requester_user: str
    source_job_id: str | None = None
    status: GeneratedExtensionJobStatus = GeneratedExtensionJobStatus.requested
    visibility: str = "private"
    request_payload: dict[str, Any] = Field(default_factory=dict)
    result_release_id: str | None = None
    error_text: str = ""
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @field_validator("account_id", "requester_user", mode="before")
    @classmethod
    def _validate_required(cls, value: Any, info):
        return _require_text(value, info.field_name)


class ExtensionRegistryProjectionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str
    install_id: str
    release_id: str
    extension_id: str
    artifact_kind: str
    artifact_id: str
    artifact_ref: str = ""
    projection: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @field_validator(
        "account_id",
        "install_id",
        "release_id",
        "extension_id",
        "artifact_kind",
        "artifact_id",
        mode="before",
    )
    @classmethod
    def _validate_required(cls, value: Any, info):
        return _require_text(value, info.field_name)


def runtime_has_no_manifest(runtime: dict[str, Any]) -> bool:
    """The scan found a folder but no extension.yaml in it."""
    errors = runtime.get("compile_errors") or runtime.get("compile_errors_jsonb") or []
    return isinstance(errors, list) and any(
        isinstance(item, dict) and item.get("code") == "missing_manifest" for item in errors
    )
