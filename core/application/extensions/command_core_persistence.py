from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from core.application.management.errors import ServiceValidationError

from .extension_runtime import (
    ExtensionArtifactSnapshotRecord,
    ExtensionReleaseRecord,
)
from .service_support import (
    _artifact_id_from_entry,
    _artifact_kind_from_section,
    _descriptor_sha256,
    _deterministic_release_id,
    _normalize_text,
)


class ReleasePersistenceStore(Protocol):
    def create_release(
        self, record: ExtensionReleaseRecord | dict[str, Any]
    ) -> ExtensionReleaseRecord: ...

    def replace_artifact_snapshots(
        self,
        release_id: str,
        snapshots: list[ExtensionArtifactSnapshotRecord],
    ) -> list[ExtensionArtifactSnapshotRecord]: ...


@dataclass(frozen=True, slots=True)
class DescriptorReleasePersistenceRequest:
    normalized_descriptor: dict[str, Any]
    account_id: str
    actor_id: str
    source_kind: str
    source_ref: str
    origin_override: str = ""


def persist_descriptor_release(
    *,
    runtime_store: ReleasePersistenceStore,
    request: DescriptorReleasePersistenceRequest,
) -> tuple[ExtensionReleaseRecord, list[ExtensionArtifactSnapshotRecord]]:
    release = _build_release_record(request)
    _ensure_source_lineage_available(runtime_store, release)
    saved_release = runtime_store.create_release(release)
    snapshots = _build_artifact_snapshots(
        request.normalized_descriptor,
        release_id=saved_release.release_id,
    )
    saved_snapshots = runtime_store.replace_artifact_snapshots(
        saved_release.release_id,
        snapshots,
    )
    return saved_release, saved_snapshots


def _build_release_record(
    request: DescriptorReleasePersistenceRequest,
) -> ExtensionReleaseRecord:
    descriptor = request.normalized_descriptor
    provenance = dict(descriptor.get("provenance") or {})
    if request.origin_override:
        provenance["origin"] = request.origin_override
    extension_id = _normalize_text(descriptor.get("extension_id"))
    release_version = _normalize_text(descriptor.get("release_version")) or "1.0.0"
    descriptor_sha256 = _descriptor_sha256(descriptor)
    distribution = _mapping_block(descriptor, "distribution")
    runtime_block = _mapping_block(descriptor, "runtime")
    execution_modes = distribution.get("execution_modes")
    return ExtensionReleaseRecord(
        release_id=_deterministic_release_id(
            extension_id=extension_id,
            release_version=release_version,
            source_kind=request.source_kind,
            source_ref=request.source_ref,
            descriptor_sha256=descriptor_sha256,
        ),
        extension_id=extension_id,
        release_version=release_version,
        descriptor_schema_version=_normalize_text(descriptor.get("schema_version"))
        or "extension_descriptor_v1",
        descriptor=descriptor,
        descriptor_sha256=descriptor_sha256,
        channel=_normalize_text(distribution.get("channel")) or "private_byo",
        execution_modes=_normalize_execution_modes(execution_modes),
        contract_version=_normalize_text(runtime_block.get("contract_version")),
        provenance=provenance,
        source_kind=_normalize_text(request.source_kind) or "file",
        source_ref=_normalize_text(request.source_ref),
        account_id=request.account_id,
        created_by=request.actor_id,
    )


def _ensure_source_lineage_available(
    runtime_store: ReleasePersistenceStore,
    candidate: ExtensionReleaseRecord,
) -> None:
    list_releases = getattr(runtime_store, "list_releases", None)
    if not callable(list_releases):
        return
    existing_releases = list_releases(extension_id=candidate.extension_id) or []
    for existing in existing_releases:
        existing_lineage = (
            _normalize_text(getattr(existing, "source_kind", "")),
            _normalize_text(getattr(existing, "source_ref", "")),
            _normalize_text(getattr(existing, "account_id", "")),
        )
        candidate_lineage = (
            candidate.source_kind,
            candidate.source_ref,
            _normalize_text(candidate.account_id),
        )
        if existing_lineage != candidate_lineage:
            raise _source_lineage_collision(candidate, existing_lineage)


def _source_lineage_collision(
    candidate: ExtensionReleaseRecord,
    existing_lineage: tuple[str, str, str],
) -> ServiceValidationError:
    existing_source_kind, existing_source_ref, _existing_account_id = existing_lineage
    return ServiceValidationError(
        (
            f"Extension id collision for '{candidate.extension_id}': package identity "
            "is already owned by another source lineage"
        ),
        payload={
            "extension_id": candidate.extension_id,
            "existing_source_kind": existing_source_kind,
            "existing_source_ref": existing_source_ref,
            "candidate_source_kind": candidate.source_kind,
            "candidate_source_ref": candidate.source_ref,
        },
    )


def _build_artifact_snapshots(
    descriptor: dict[str, Any],
    *,
    release_id: str,
) -> list[ExtensionArtifactSnapshotRecord]:
    artifacts_block = descriptor.get("artifacts")
    if not isinstance(artifacts_block, dict):
        return []
    snapshots: list[ExtensionArtifactSnapshotRecord] = []
    for section_name, entries in artifacts_block.items():
        if not isinstance(entries, list):
            continue
        artifact_kind = _artifact_kind_from_section(section_name)
        snapshots.extend(_section_snapshots(entries, release_id, artifact_kind))
    return snapshots


def _section_snapshots(
    entries: list[Any],
    release_id: str,
    artifact_kind: str,
) -> list[ExtensionArtifactSnapshotRecord]:
    snapshots: list[ExtensionArtifactSnapshotRecord] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        artifact_id = _artifact_id_from_entry(entry)
        if artifact_id:
            snapshots.append(
                ExtensionArtifactSnapshotRecord(
                    release_id=release_id,
                    artifact_kind=artifact_kind,
                    artifact_id=artifact_id,
                    artifact_ref=_normalize_text(entry.get("ref")),
                    publish=bool(entry.get("publish", True)),
                    snapshot=dict(entry.get("snapshot") or entry),
                )
            )
    return snapshots


def _mapping_block(descriptor: dict[str, Any], key: str) -> dict[str, Any]:
    value = descriptor.get(key)
    return value if isinstance(value, dict) else {}


def _normalize_execution_modes(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_normalize_text(mode).lower() for mode in value if _normalize_text(mode)]


__all__ = [
    "DescriptorReleasePersistenceRequest",
    "ReleasePersistenceStore",
    "persist_descriptor_release",
]
