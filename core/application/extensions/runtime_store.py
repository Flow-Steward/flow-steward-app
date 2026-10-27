"""Storage adapters for descriptor-backed extension runtime foundation entities."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from core.application.extensions.extension_runtime import (
    EntitlementStatus,
    ExtensionArtifactSnapshotRecord,
    ExtensionEntitlementRecord,
    ExtensionRegistryProjectionRecord,
    ExtensionReleaseRecord,
    ExtensionUsageEventRecord,
    GeneratedExtensionJobRecord,
    GeneratedExtensionJobStatus,
)
from core.application.extensions.runtime_store_db import DBExtensionRuntimeStore


class ExtensionRuntimeStore:
    """In-memory runtime foundation store for tests and local/offline usage."""

    def __init__(self) -> None:
        self._releases: dict[str, ExtensionReleaseRecord] = {}
        self._artifact_snapshots: list[ExtensionArtifactSnapshotRecord] = []
        self._entitlements: dict[str, ExtensionEntitlementRecord] = {}
        self._usage_events: list[ExtensionUsageEventRecord] = []
        self._generated_jobs: dict[str, GeneratedExtensionJobRecord] = {}
        self._projections: dict[tuple[str, str, str], ExtensionRegistryProjectionRecord] = {}

    @staticmethod
    def participates_in_transaction(_transaction_connection: Any) -> bool:
        return False

    # -- Releases --
    def create_release(
        self, record: ExtensionReleaseRecord | dict[str, Any]
    ) -> ExtensionReleaseRecord:
        release = (
            record
            if isinstance(record, ExtensionReleaseRecord)
            else ExtensionReleaseRecord.model_validate(record)
        )
        self._releases[release.release_id] = release
        return release

    def get_release(self, release_id: str) -> ExtensionReleaseRecord | None:
        return self._releases.get(str(release_id or "").strip())

    def update_release_bundle_root(
        self, release_id: str, *, bundle_root: str
    ) -> ExtensionReleaseRecord | None:
        release = self.get_release(release_id)
        if release is None:
            return None
        descriptor = {**release.descriptor, "bundle_root": str(bundle_root or "").strip()}
        release.descriptor = descriptor
        release.descriptor_sha256 = hashlib.sha256(
            json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return release

    def list_releases(
        self,
        *,
        extension_id: str = "",
        account_id: str = "",
        release_ids: list[str] | None = None,
    ) -> list[ExtensionReleaseRecord]:
        rows = list(self._releases.values())
        if extension_id:
            rows = [r for r in rows if r.extension_id == extension_id]
        if account_id:
            rows = [r for r in rows if (r.account_id or "") == account_id]
        if release_ids:
            wanted = {str(item or "").strip() for item in release_ids if str(item or "").strip()}
            rows = [r for r in rows if r.release_id in wanted]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows

    def delete_release(self, release_id: str) -> None:
        normalized_release_id = str(release_id or "").strip()
        self._releases.pop(normalized_release_id, None)
        self._artifact_snapshots = [
            row for row in self._artifact_snapshots if row.release_id != normalized_release_id
        ]

    # -- Artifact snapshots --
    def replace_artifact_snapshots(
        self,
        release_id: str,
        snapshots: list[ExtensionArtifactSnapshotRecord | dict[str, Any]],
    ) -> list[ExtensionArtifactSnapshotRecord]:
        normalized_release_id = str(release_id or "").strip()
        self._artifact_snapshots = [
            row for row in self._artifact_snapshots if row.release_id != normalized_release_id
        ]
        saved: list[ExtensionArtifactSnapshotRecord] = []
        for row in snapshots:
            payload = (
                row
                if isinstance(row, ExtensionArtifactSnapshotRecord)
                else ExtensionArtifactSnapshotRecord.model_validate(row)
            )
            saved.append(payload)
        self._artifact_snapshots.extend(saved)
        return saved

    def list_artifact_snapshots(self, release_id: str) -> list[ExtensionArtifactSnapshotRecord]:
        normalized_release_id = str(release_id or "").strip()
        return [row for row in self._artifact_snapshots if row.release_id == normalized_release_id]

    def list_artifact_snapshots_for_releases(
        self, release_ids: list[str]
    ) -> list[ExtensionArtifactSnapshotRecord]:
        normalized_ids = {
            str(item or "").strip() for item in release_ids if str(item or "").strip()
        }
        if not normalized_ids:
            return []
        return [row for row in self._artifact_snapshots if row.release_id in normalized_ids]

    # -- Entitlements --
    def upsert_entitlement(
        self,
        *,
        install_id: str,
        account_id: str,
        status: str = EntitlementStatus.trial_active.value,
        trial_ends_at: float | None = None,
        grace_ends_at: float | None = None,
        suspended_reason: str = "",
        metadata: dict[str, Any] | None = None,
        updated_by: str = "",
    ) -> ExtensionEntitlementRecord:
        existing = self._entitlements.get(str(install_id or "").strip())
        now = time.time()
        if existing is not None:
            existing.status = EntitlementStatus(status)
            existing.trial_ends_at = trial_ends_at
            existing.grace_ends_at = grace_ends_at
            existing.suspended_reason = str(suspended_reason or "").strip()
            existing.metadata = dict(metadata or {})
            existing.updated_by = str(updated_by or "").strip()
            existing.updated_at = now
            return existing
        record = ExtensionEntitlementRecord(
            install_id=install_id,
            account_id=account_id,
            status=EntitlementStatus(status),
            trial_ends_at=trial_ends_at,
            grace_ends_at=grace_ends_at,
            suspended_reason=suspended_reason,
            metadata=metadata or {},
            updated_by=updated_by,
            created_at=now,
            updated_at=now,
        )
        self._entitlements[record.install_id] = record
        return record

    def get_entitlement(self, install_id: str) -> ExtensionEntitlementRecord | None:
        return self._entitlements.get(str(install_id or "").strip())

    def list_entitlements(
        self,
        *,
        account_id: str = "",
        install_ids: list[str] | None = None,
    ) -> list[ExtensionEntitlementRecord]:
        rows = list(self._entitlements.values())
        if account_id:
            rows = [row for row in rows if row.account_id == str(account_id or "").strip()]
        if install_ids:
            wanted = {str(item or "").strip() for item in install_ids if str(item or "").strip()}
            rows = [row for row in rows if row.install_id in wanted]
        rows.sort(key=lambda row: row.updated_at, reverse=True)
        return rows

    def delete_entitlement(self, install_id: str) -> None:
        self._entitlements.pop(str(install_id or "").strip(), None)

    # -- Usage events --
    def record_usage_event(
        self, payload: ExtensionUsageEventRecord | dict[str, Any]
    ) -> ExtensionUsageEventRecord:
        event = (
            payload
            if isinstance(payload, ExtensionUsageEventRecord)
            else ExtensionUsageEventRecord.model_validate(payload)
        )
        self._usage_events.append(event)
        return event

    def list_usage_events(
        self, *, install_id: str = "", account_id: str = "", limit: int = 200
    ) -> list[ExtensionUsageEventRecord]:
        rows = list(self._usage_events)
        if install_id:
            rows = [r for r in rows if r.install_id == install_id]
        if account_id:
            rows = [r for r in rows if r.account_id == account_id]
        rows.sort(key=lambda r: r.occurred_at, reverse=True)
        return rows[: max(1, int(limit or 1))]

    # -- Generated jobs --
    def create_generated_extension_job(
        self, payload: GeneratedExtensionJobRecord | dict[str, Any]
    ) -> GeneratedExtensionJobRecord:
        job = (
            payload
            if isinstance(payload, GeneratedExtensionJobRecord)
            else GeneratedExtensionJobRecord.model_validate(payload)
        )
        self._generated_jobs[job.generated_job_id] = job
        return job

    def update_generated_extension_job(
        self,
        generated_job_id: str,
        *,
        status: str | None = None,
        result_release_id: str | None = None,
        error_text: str | None = None,
    ) -> GeneratedExtensionJobRecord | None:
        row = self._generated_jobs.get(str(generated_job_id or "").strip())
        if row is None:
            return None
        if status is not None:
            row.status = GeneratedExtensionJobStatus(status)
        if result_release_id is not None:
            row.result_release_id = str(result_release_id or "").strip() or None
        if error_text is not None:
            row.error_text = str(error_text or "").strip()
        row.updated_at = time.time()
        return row

    def get_generated_extension_job(
        self, generated_job_id: str
    ) -> GeneratedExtensionJobRecord | None:
        return self._generated_jobs.get(str(generated_job_id or "").strip())

    def list_generated_extension_jobs(
        self, *, account_id: str, limit: int = 100
    ) -> list[GeneratedExtensionJobRecord]:
        rows = [row for row in self._generated_jobs.values() if row.account_id == account_id]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[: max(1, int(limit or 1))]

    # -- Registry projections --
    def upsert_projection(
        self, payload: ExtensionRegistryProjectionRecord | dict[str, Any]
    ) -> ExtensionRegistryProjectionRecord:
        row = (
            payload
            if isinstance(payload, ExtensionRegistryProjectionRecord)
            else ExtensionRegistryProjectionRecord.model_validate(payload)
        )
        key = (row.install_id, row.artifact_kind, row.artifact_id)
        self._projections[key] = row
        return row

    def list_projections(
        self,
        *,
        account_id: str,
        artifact_kind: str = "",
        only_active: bool = True,
    ) -> list[ExtensionRegistryProjectionRecord]:
        rows = [row for row in self._projections.values() if row.account_id == account_id]
        if artifact_kind:
            rows = [row for row in rows if row.artifact_kind == artifact_kind]
        if only_active:
            rows = [row for row in rows if row.is_active]
        rows.sort(key=lambda r: (r.extension_id, r.artifact_kind, r.artifact_id))
        return rows

    def set_projection_active_for_install(self, install_id: str, *, is_active: bool) -> None:
        normalized_install_id = str(install_id or "").strip()
        now = time.time()
        for row in self._projections.values():
            if row.install_id == normalized_install_id:
                row.is_active = bool(is_active)
                row.updated_at = now

    def delete_projections_for_install(self, install_id: str) -> None:
        normalized_install_id = str(install_id or "").strip()
        keys = [
            key for key, row in self._projections.items() if row.install_id == normalized_install_id
        ]
        for key in keys:
            self._projections.pop(key, None)


__all__ = ["DBExtensionRuntimeStore", "ExtensionRuntimeStore"]
