"""Clone source allowlist and manifest-completeness policy."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from core.application.clone.contracts import (
    ARCHIVE_ROOT_BY_DATA_CLASS,
    CloneDataClass,
    CloneManifest,
)
from core.application.lifecycle.contracts import DeploymentConfig
from core.application.release.contracts import ReleaseIdentity


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CloneSourceItem(_ClosedModel):
    """One logical capture input; infrastructure supplies its concrete source."""

    data_class: CloneDataClass
    archive_root: str
    capture_kind: Literal["database", "volume", "filesystem", "metadata"]


class CloneSourcePlan(_ClosedModel):
    """Explicit source-owned state selected from canonical installation config."""

    deployment_profile: Literal["compact", "scalable"]
    data_placement: Literal["host_directories", "compact_volume"]
    file_storage: Literal["local_fs", "bundled", "external_s3"]
    items: tuple[CloneSourceItem, ...]


_CAPTURE_KIND: dict[CloneDataClass, Literal["database", "volume", "filesystem", "metadata"]] = {
    CloneDataClass.POSTGRESQL: "database",
    CloneDataClass.RABBITMQ: "volume",
    CloneDataClass.BUNDLED_STORAGE: "volume",
    CloneDataClass.LOCAL_ARTIFACTS: "filesystem",
    CloneDataClass.EXTENSIONS: "filesystem",
    CloneDataClass.DURABLE_STATE: "filesystem",
    CloneDataClass.SOURCE_SECRETS: "metadata",  # pragma: allowlist secret - capture kind only
    CloneDataClass.RELEASE_EVIDENCE: "metadata",
}


def build_source_allowlist(
    config: DeploymentConfig,
    release: ReleaseIdentity,
) -> CloneSourcePlan:
    """Select only installation-owned product classes for the active topology."""
    if config.deployment_profile != release.profile:
        raise ValueError("Installed configuration and release profile do not match")
    selected = [
        CloneDataClass.POSTGRESQL,
        CloneDataClass.RABBITMQ,
        CloneDataClass.EXTENSIONS,
        CloneDataClass.DURABLE_STATE,
        CloneDataClass.SOURCE_SECRETS,
        CloneDataClass.RELEASE_EVIDENCE,
    ]
    if config.file_storage == "local_fs":
        selected.append(CloneDataClass.LOCAL_ARTIFACTS)
    elif config.file_storage == "bundled":
        selected.append(CloneDataClass.BUNDLED_STORAGE)
    selected.sort(key=lambda item: ARCHIVE_ROOT_BY_DATA_CLASS[item])
    return CloneSourcePlan(
        deployment_profile=config.deployment_profile,
        data_placement=config.data_placement,
        file_storage=config.file_storage,
        items=tuple(
            CloneSourceItem(
                data_class=data_class,
                archive_root=ARCHIVE_ROOT_BY_DATA_CLASS[data_class],
                capture_kind=_CAPTURE_KIND[data_class],
            )
            for data_class in selected
        ),
    )


def validate_clone_manifest_completeness(manifest: CloneManifest) -> None:
    """Require every payload consumed by destructive restore before target mutation."""
    required_paths = {
        "postgresql/database.dump",
        "rabbitmq",
        "extensions",
        "state",
        "secrets/source.json",
        "evidence/release.json",
        "evidence/configuration.json",
        "evidence/database-parity.json",
        "evidence/preservation.json",
    }
    if manifest.file_storage == "local_fs":
        required_paths.add("artifacts")
    elif manifest.file_storage == "bundled":
        required_paths.add("bundled-storage")
    entries = {entry.path: entry for entry in manifest.entries}
    missing = sorted(required_paths - set(entries))
    if missing:
        raise ValueError(f"Clone manifest is missing required Clone payload: {missing[0]}")
    for root in (
        "rabbitmq",
        "extensions",
        "state",
        *(root for root in ("artifacts", "bundled-storage") if root in required_paths),
    ):
        if entries[root].entry_type != "directory":
            raise ValueError(f"Clone manifest required payload must be a directory: {root}")
    for path in required_paths - {
        "rabbitmq",
        "extensions",
        "state",
        "artifacts",
        "bundled-storage",
    }:
        if entries[path].entry_type != "file":
            raise ValueError(f"Clone manifest required payload must be a file: {path}")
    parity_digest = str(manifest.database_parity_evidence.get("snapshot_sha256") or "")
    if parity_digest != entries["evidence/database-parity.json"].sha256:
        raise ValueError("Clone manifest database parity evidence is incomplete")
    if manifest.capture_evidence.get("postgresql_format") != "custom":
        raise ValueError("Clone manifest PostgreSQL capture evidence is incomplete")
    if manifest.capture_evidence.get("maintenance") != "manual":
        raise ValueError("Clone manifest maintenance capture evidence is incomplete")


__all__ = [
    "CloneSourceItem",
    "CloneSourcePlan",
    "build_source_allowlist",
    "validate_clone_manifest_completeness",
]
