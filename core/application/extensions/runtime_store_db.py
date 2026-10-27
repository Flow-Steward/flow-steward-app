"""PostgreSQL adapter for extension runtime foundation entities."""

from __future__ import annotations

import hashlib
import json
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
from core.application.extensions.runtime_store_common import (
    ACCOUNT_ID_S_SQL,
    _compose_where_clause,
    _json_dict,
    _placeholders,
    _row_to_dict,
    _string_list,
    _to_timestamp,
)
from core.infrastructure.db.psycopg_runtime import sql as psycopg_sql


class DBExtensionRuntimeStore:
    """PostgreSQL runtime foundation store."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def participates_in_transaction(self, transaction_connection: Any) -> bool:
        from core.infrastructure.db.db_pool import connection_participates_in_transaction

        return connection_participates_in_transaction(self._conn, transaction_connection)

    # -- Releases --
    def create_release(
        self, record: ExtensionReleaseRecord | dict[str, Any]
    ) -> ExtensionReleaseRecord:
        row = (
            record
            if isinstance(record, ExtensionReleaseRecord)
            else ExtensionReleaseRecord.model_validate(record)
        )
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO extension_releases (
                    release_id, extension_id, release_version, descriptor_schema_version,
                    descriptor_jsonb, descriptor_sha256, channel, execution_modes_jsonb, contract_version,
                    provenance_jsonb, source_kind, source_ref, account_id, created_by
                ) VALUES (
                    %s, %s, %s, %s,
                    %s::jsonb, %s, %s, %s::jsonb, %s,
                    %s::jsonb, %s, %s, %s, %s
                )
                ON CONFLICT (release_id) DO NOTHING
                """,
                (
                    row.release_id,
                    row.extension_id,
                    row.release_version,
                    row.descriptor_schema_version,
                    json.dumps(row.descriptor),
                    row.descriptor_sha256,
                    row.channel,
                    json.dumps(row.execution_modes),
                    row.contract_version,
                    json.dumps(row.provenance),
                    row.source_kind,
                    row.source_ref,
                    row.account_id,
                    row.created_by,
                ),
            )
        return row

    def get_release(self, release_id: str) -> ExtensionReleaseRecord | None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT release_id, extension_id, release_version, descriptor_schema_version,
                       descriptor_jsonb, descriptor_sha256, channel, execution_modes_jsonb, contract_version,
                       provenance_jsonb, source_kind, source_ref, account_id, created_by, created_at
                FROM extension_releases
                WHERE release_id = %s
                """,
                (str(release_id or "").strip(),),
            )
            row = cur.fetchone()
            if not row:
                return None
            payload = _row_to_dict(row, cur.description)
        return ExtensionReleaseRecord(
            release_id=str(payload.get("release_id") or ""),
            extension_id=str(payload.get("extension_id") or ""),
            release_version=str(payload.get("release_version") or "1.0.0"),
            descriptor_schema_version=str(
                payload.get("descriptor_schema_version") or "extension_descriptor_v1"
            ),
            descriptor=_json_dict(payload.get("descriptor_jsonb")),
            descriptor_sha256=str(payload.get("descriptor_sha256") or "").strip(),
            channel=str(payload.get("channel") or "private_byo"),
            execution_modes=_string_list(payload.get("execution_modes_jsonb")),
            contract_version=str(payload.get("contract_version") or ""),
            provenance=_json_dict(payload.get("provenance_jsonb")),
            source_kind=str(payload.get("source_kind") or "file"),
            source_ref=str(payload.get("source_ref") or ""),
            account_id=str(payload.get("account_id") or "").strip() or None,
            created_by=str(payload.get("created_by") or ""),
            created_at=_to_timestamp(payload.get("created_at")),
        )

    def update_release_bundle_root(
        self, release_id: str, *, bundle_root: str
    ) -> ExtensionReleaseRecord | None:
        release = self.get_release(release_id)
        if release is None:
            return None
        descriptor = {**release.descriptor, "bundle_root": str(bundle_root or "").strip()}
        descriptor_json = json.dumps(descriptor, sort_keys=True, separators=(",", ":"))
        descriptor_sha256 = hashlib.sha256(descriptor_json.encode("utf-8")).hexdigest()
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE extension_releases
                   SET descriptor_jsonb = %s::jsonb,
                       descriptor_sha256 = %s
                 WHERE release_id = %s
                """,
                (descriptor_json, descriptor_sha256, str(release_id or "").strip()),
            )
        release.descriptor = descriptor
        release.descriptor_sha256 = descriptor_sha256
        return release

    def list_releases(
        self,
        *,
        extension_id: str = "",
        account_id: str = "",
        release_ids: list[str] | None = None,
    ) -> list[ExtensionReleaseRecord]:
        conditions: list[Any] = []
        params: list[Any] = []
        if extension_id:
            conditions.append(psycopg_sql.SQL("extension_id = %s"))
            params.append(str(extension_id).strip())
        if account_id:
            conditions.append(psycopg_sql.SQL(ACCOUNT_ID_S_SQL))
            params.append(str(account_id).strip())
        normalized_release_ids = [
            str(item or "").strip() for item in (release_ids or []) if str(item or "").strip()
        ]
        if normalized_release_ids:
            conditions.append(
                psycopg_sql.SQL("release_id IN ({placeholders})").format(
                    placeholders=_placeholders(len(normalized_release_ids)),
                )
            )
            params.extend(normalized_release_ids)
        where_clause = _compose_where_clause(conditions)
        with self._conn.cursor() as cur:
            cur.execute(
                psycopg_sql.SQL(
                    """
                SELECT release_id, extension_id, release_version, descriptor_schema_version,
                       descriptor_jsonb, descriptor_sha256, channel, execution_modes_jsonb, contract_version,
                       provenance_jsonb, source_kind, source_ref, account_id, created_by, created_at
                FROM extension_releases
                {where_clause}
                ORDER BY created_at DESC
                """
                ).format(where_clause=where_clause),
                tuple(params),
            )
            rows = cur.fetchall() or []
            desc = cur.description
        result: list[ExtensionReleaseRecord] = []
        for row in rows:
            payload = _row_to_dict(row, desc)
            result.append(
                ExtensionReleaseRecord(
                    release_id=str(payload.get("release_id") or ""),
                    extension_id=str(payload.get("extension_id") or ""),
                    release_version=str(payload.get("release_version") or "1.0.0"),
                    descriptor_schema_version=str(
                        payload.get("descriptor_schema_version") or "extension_descriptor_v1"
                    ),
                    descriptor=_json_dict(payload.get("descriptor_jsonb")),
                    descriptor_sha256=str(payload.get("descriptor_sha256") or "").strip(),
                    channel=str(payload.get("channel") or "private_byo"),
                    execution_modes=_string_list(payload.get("execution_modes_jsonb")),
                    contract_version=str(payload.get("contract_version") or ""),
                    provenance=_json_dict(payload.get("provenance_jsonb")),
                    source_kind=str(payload.get("source_kind") or "file"),
                    source_ref=str(payload.get("source_ref") or ""),
                    account_id=str(payload.get("account_id") or "").strip() or None,
                    created_by=str(payload.get("created_by") or ""),
                    created_at=_to_timestamp(payload.get("created_at")),
                )
            )
        return result

    def delete_release(self, release_id: str) -> None:
        normalized_release_id = str(release_id or "").strip()
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM extension_artifact_snapshots WHERE release_id = %s",
                (normalized_release_id,),
            )
            cur.execute(
                "DELETE FROM extension_releases WHERE release_id = %s",
                (normalized_release_id,),
            )

    # -- Artifact snapshots --
    def replace_artifact_snapshots(
        self,
        release_id: str,
        snapshots: list[ExtensionArtifactSnapshotRecord | dict[str, Any]],
    ) -> list[ExtensionArtifactSnapshotRecord]:
        normalized_release_id = str(release_id or "").strip()
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM extension_artifact_snapshots WHERE release_id = %s",
                (normalized_release_id,),
            )
            for row in snapshots:
                record = (
                    row
                    if isinstance(row, ExtensionArtifactSnapshotRecord)
                    else ExtensionArtifactSnapshotRecord.model_validate(row)
                )
                cur.execute(
                    """
                    INSERT INTO extension_artifact_snapshots (
                        release_id, artifact_kind, artifact_id, artifact_ref, publish, snapshot_jsonb
                    ) VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        record.release_id,
                        record.artifact_kind,
                        record.artifact_id,
                        record.artifact_ref,
                        bool(record.publish),
                        json.dumps(record.snapshot),
                    ),
                )
        return self.list_artifact_snapshots(normalized_release_id)

    def list_artifact_snapshots(self, release_id: str) -> list[ExtensionArtifactSnapshotRecord]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT release_id, artifact_kind, artifact_id, artifact_ref, publish, snapshot_jsonb, created_at
                FROM extension_artifact_snapshots
                WHERE release_id = %s
                ORDER BY artifact_kind, artifact_id
                """,
                (str(release_id or "").strip(),),
            )
            rows = cur.fetchall() or []
            desc = cur.description
        result: list[ExtensionArtifactSnapshotRecord] = []
        for row in rows:
            payload = _row_to_dict(row, desc)
            result.append(
                ExtensionArtifactSnapshotRecord(
                    release_id=str(payload.get("release_id") or ""),
                    artifact_kind=str(payload.get("artifact_kind") or ""),
                    artifact_id=str(payload.get("artifact_id") or ""),
                    artifact_ref=str(payload.get("artifact_ref") or ""),
                    publish=bool(payload.get("publish")),
                    snapshot=_json_dict(payload.get("snapshot_jsonb")),
                    created_at=_to_timestamp(payload.get("created_at")),
                )
            )
        return result

    def list_artifact_snapshots_for_releases(
        self, release_ids: list[str]
    ) -> list[ExtensionArtifactSnapshotRecord]:
        normalized_ids = [
            str(item or "").strip() for item in (release_ids or []) if str(item or "").strip()
        ]
        if not normalized_ids:
            return []
        placeholder_sql = _placeholders(len(normalized_ids))
        with self._conn.cursor() as cur:
            cur.execute(
                psycopg_sql.SQL(
                    """
                SELECT release_id, artifact_kind, artifact_id, artifact_ref, publish, snapshot_jsonb, created_at
                FROM extension_artifact_snapshots
                WHERE release_id IN ({placeholders})
                ORDER BY release_id, artifact_kind, artifact_id
                """
                ).format(placeholders=placeholder_sql),
                tuple(normalized_ids),
            )
            rows = cur.fetchall() or []
            desc = cur.description
        result: list[ExtensionArtifactSnapshotRecord] = []
        for row in rows:
            payload = _row_to_dict(row, desc)
            result.append(
                ExtensionArtifactSnapshotRecord(
                    release_id=str(payload.get("release_id") or ""),
                    artifact_kind=str(payload.get("artifact_kind") or ""),
                    artifact_id=str(payload.get("artifact_id") or ""),
                    artifact_ref=str(payload.get("artifact_ref") or ""),
                    publish=bool(payload.get("publish")),
                    snapshot=_json_dict(payload.get("snapshot_jsonb")),
                    created_at=_to_timestamp(payload.get("created_at")),
                )
            )
        return result

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
        existing = self.get_entitlement(install_id)
        entitlement_id = (
            str(existing.entitlement_id)
            if existing is not None
            else ExtensionEntitlementRecord(
                install_id=install_id, account_id=account_id
            ).entitlement_id
        )
        normalized_install_id = str(install_id or "").strip()
        normalized_account_id = str(account_id or "").strip()
        normalized_status = str(status or EntitlementStatus.trial_active.value)
        normalized_suspended_reason = str(suspended_reason or "").strip()
        normalized_metadata = json.dumps(metadata or {})
        normalized_updated_by = str(updated_by or "").strip()
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE extension_entitlements
                SET account_id = %s,
                    status = %s,
                    trial_ends_at = to_timestamp(%s),
                    grace_ends_at = to_timestamp(%s),
                    suspended_reason = %s,
                    metadata_jsonb = %s::jsonb,
                    updated_by = %s,
                    updated_at = now()
                WHERE install_id = %s
                """,
                (
                    normalized_account_id,
                    normalized_status,
                    trial_ends_at,
                    grace_ends_at,
                    normalized_suspended_reason,
                    normalized_metadata,
                    normalized_updated_by,
                    normalized_install_id,
                ),
            )
            if cur.rowcount == 0:
                cur.execute(
                    """
                    INSERT INTO extension_entitlements (
                        entitlement_id, install_id, account_id, status,
                        trial_ends_at, grace_ends_at, suspended_reason, metadata_jsonb, updated_by
                    ) VALUES (
                        %s, %s, %s, %s,
                        to_timestamp(%s), to_timestamp(%s), %s, %s::jsonb, %s
                    )
                    """,
                    (
                        entitlement_id,
                        normalized_install_id,
                        normalized_account_id,
                        normalized_status,
                        trial_ends_at,
                        grace_ends_at,
                        normalized_suspended_reason,
                        normalized_metadata,
                        normalized_updated_by,
                    ),
                )
        refreshed = self.get_entitlement(install_id)
        assert refreshed is not None
        return refreshed

    def get_entitlement(self, install_id: str) -> ExtensionEntitlementRecord | None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT entitlement_id, install_id, account_id, status,
                       trial_ends_at, grace_ends_at, suspended_reason, metadata_jsonb,
                       updated_by, created_at, updated_at
                FROM extension_entitlements
                WHERE install_id = %s
                LIMIT 1
                """,
                (str(install_id or "").strip(),),
            )
            row = cur.fetchone()
            if not row:
                return None
            payload = _row_to_dict(row, cur.description)
        return ExtensionEntitlementRecord(
            entitlement_id=str(payload.get("entitlement_id") or ""),
            install_id=str(payload.get("install_id") or ""),
            account_id=str(payload.get("account_id") or ""),
            status=EntitlementStatus(
                str(payload.get("status") or EntitlementStatus.trial_active.value)
            ),
            trial_ends_at=_to_timestamp(payload.get("trial_ends_at"))
            if payload.get("trial_ends_at")
            else None,
            grace_ends_at=_to_timestamp(payload.get("grace_ends_at"))
            if payload.get("grace_ends_at")
            else None,
            suspended_reason=str(payload.get("suspended_reason") or ""),
            metadata=_json_dict(payload.get("metadata_jsonb")),
            updated_by=str(payload.get("updated_by") or ""),
            created_at=_to_timestamp(payload.get("created_at")),
            updated_at=_to_timestamp(payload.get("updated_at")),
        )

    def delete_entitlement(self, install_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM extension_entitlements WHERE install_id = %s",
                (str(install_id or "").strip(),),
            )

    def list_entitlements(
        self,
        *,
        account_id: str = "",
        install_ids: list[str] | None = None,
    ) -> list[ExtensionEntitlementRecord]:
        conditions: list[Any] = []
        params: list[Any] = []
        normalized_account = str(account_id or "").strip()
        if normalized_account:
            conditions.append(psycopg_sql.SQL(ACCOUNT_ID_S_SQL))
            params.append(normalized_account)
        normalized_install_ids = [
            str(item or "").strip() for item in (install_ids or []) if str(item or "").strip()
        ]
        if normalized_install_ids:
            conditions.append(
                psycopg_sql.SQL("install_id IN ({placeholders})").format(
                    placeholders=_placeholders(len(normalized_install_ids)),
                )
            )
            params.extend(normalized_install_ids)
        where_clause = _compose_where_clause(conditions)
        with self._conn.cursor() as cur:
            cur.execute(
                psycopg_sql.SQL(
                    """
                SELECT entitlement_id, install_id, account_id, status,
                       trial_ends_at, grace_ends_at, suspended_reason, metadata_jsonb,
                       updated_by, created_at, updated_at
                FROM extension_entitlements
                {where_clause}
                ORDER BY updated_at DESC
                """
                ).format(where_clause=where_clause),
                tuple(params),
            )
            rows = cur.fetchall() or []
            desc = cur.description
        result: list[ExtensionEntitlementRecord] = []
        for row in rows:
            payload = _row_to_dict(row, desc)
            result.append(
                ExtensionEntitlementRecord(
                    entitlement_id=str(payload.get("entitlement_id") or ""),
                    install_id=str(payload.get("install_id") or ""),
                    account_id=str(payload.get("account_id") or ""),
                    status=EntitlementStatus(
                        str(payload.get("status") or EntitlementStatus.trial_active.value)
                    ),
                    trial_ends_at=_to_timestamp(payload.get("trial_ends_at"))
                    if payload.get("trial_ends_at")
                    else None,
                    grace_ends_at=_to_timestamp(payload.get("grace_ends_at"))
                    if payload.get("grace_ends_at")
                    else None,
                    suspended_reason=str(payload.get("suspended_reason") or ""),
                    metadata=_json_dict(payload.get("metadata_jsonb")),
                    updated_by=str(payload.get("updated_by") or ""),
                    created_at=_to_timestamp(payload.get("created_at")),
                    updated_at=_to_timestamp(payload.get("updated_at")),
                )
            )
        return result

    # -- Usage events --
    def record_usage_event(
        self, payload: ExtensionUsageEventRecord | dict[str, Any]
    ) -> ExtensionUsageEventRecord:
        row = (
            payload
            if isinstance(payload, ExtensionUsageEventRecord)
            else ExtensionUsageEventRecord.model_validate(payload)
        )
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO extension_usage_events (
                    account_id, install_id, release_id, project_id, connection_id,
                    event_kind, quantity, unit, metadata_jsonb, occurred_at, recorded_at
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s::jsonb, to_timestamp(%s), to_timestamp(%s)
                )
                """,
                (
                    row.account_id,
                    row.install_id,
                    row.release_id,
                    row.project_id,
                    row.connection_id,
                    row.event_kind,
                    row.quantity,
                    row.unit,
                    json.dumps(row.metadata),
                    row.occurred_at,
                    row.recorded_at,
                ),
            )
        return row

    def list_usage_events(
        self, *, install_id: str = "", account_id: str = "", limit: int = 200
    ) -> list[ExtensionUsageEventRecord]:
        conditions: list[Any] = []
        params: list[Any] = []
        if install_id:
            conditions.append(psycopg_sql.SQL("install_id = %s"))
            params.append(str(install_id).strip())
        if account_id:
            conditions.append(psycopg_sql.SQL(ACCOUNT_ID_S_SQL))
            params.append(str(account_id).strip())
        where_clause = _compose_where_clause(conditions)
        with self._conn.cursor() as cur:
            cur.execute(
                psycopg_sql.SQL(
                    """
                SELECT account_id, install_id, release_id, project_id, connection_id,
                       event_kind, quantity, unit, metadata_jsonb, occurred_at, recorded_at
                FROM extension_usage_events
                {where_clause}
                ORDER BY occurred_at DESC
                LIMIT %s
                """
                ).format(where_clause=where_clause),
                (*params, max(1, int(limit or 1))),
            )
            rows = cur.fetchall() or []
            desc = cur.description
        result: list[ExtensionUsageEventRecord] = []
        for row in rows:
            payload = _row_to_dict(row, desc)
            result.append(
                ExtensionUsageEventRecord(
                    account_id=str(payload.get("account_id") or ""),
                    install_id=str(payload.get("install_id") or ""),
                    release_id=str(payload.get("release_id") or "").strip() or None,
                    project_id=str(payload.get("project_id") or "").strip() or None,
                    connection_id=str(payload.get("connection_id") or "").strip() or None,
                    event_kind=str(payload.get("event_kind") or ""),
                    quantity=float(payload.get("quantity") or 0.0),
                    unit=str(payload.get("unit") or "unit"),
                    metadata=_json_dict(payload.get("metadata_jsonb")),
                    occurred_at=_to_timestamp(payload.get("occurred_at")),
                    recorded_at=_to_timestamp(payload.get("recorded_at")),
                )
            )
        return result

    # -- Generated jobs --
    def create_generated_extension_job(
        self, payload: GeneratedExtensionJobRecord | dict[str, Any]
    ) -> GeneratedExtensionJobRecord:
        row = (
            payload
            if isinstance(payload, GeneratedExtensionJobRecord)
            else GeneratedExtensionJobRecord.model_validate(payload)
        )
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO generated_extension_jobs (
                    generated_job_id, account_id, requester_user, source_job_id, status, visibility,
                    request_payload_jsonb, result_release_id, error_text
                ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                """,
                (
                    row.generated_job_id,
                    row.account_id,
                    row.requester_user,
                    row.source_job_id,
                    row.status.value,
                    row.visibility,
                    json.dumps(row.request_payload),
                    row.result_release_id,
                    row.error_text,
                ),
            )
        saved = self.get_generated_extension_job(row.generated_job_id)
        assert saved is not None
        return saved

    def update_generated_extension_job(
        self,
        generated_job_id: str,
        *,
        status: str | None = None,
        result_release_id: str | None = None,
        error_text: str | None = None,
    ) -> GeneratedExtensionJobRecord | None:
        clauses: list[str] = []
        values: list[Any] = []
        if status is not None:
            clauses.append("status = %s")
            values.append(str(status).strip())
        if result_release_id is not None:
            clauses.append("result_release_id = %s")
            values.append(str(result_release_id or "").strip() or None)
        if error_text is not None:
            clauses.append("error_text = %s")
            values.append(str(error_text or "").strip())
        if not clauses:
            return self.get_generated_extension_job(generated_job_id)
        values.append(str(generated_job_id or "").strip())
        set_clause = psycopg_sql.SQL(", ").join(psycopg_sql.SQL(clause) for clause in clauses)
        with self._conn.cursor() as cur:
            cur.execute(
                psycopg_sql.SQL(
                    "UPDATE generated_extension_jobs SET {set_clause}, updated_at = now() WHERE generated_job_id = %s"
                ).format(set_clause=set_clause),
                tuple(values),
            )
        return self.get_generated_extension_job(generated_job_id)

    def get_generated_extension_job(
        self, generated_job_id: str
    ) -> GeneratedExtensionJobRecord | None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT generated_job_id, account_id, requester_user, source_job_id, status, visibility,
                       request_payload_jsonb, result_release_id, error_text, created_at, updated_at
                FROM generated_extension_jobs
                WHERE generated_job_id = %s
                LIMIT 1
                """,
                (str(generated_job_id or "").strip(),),
            )
            row = cur.fetchone()
            if not row:
                return None
            payload = _row_to_dict(row, cur.description)
        return GeneratedExtensionJobRecord(
            generated_job_id=str(payload.get("generated_job_id") or ""),
            account_id=str(payload.get("account_id") or ""),
            requester_user=str(payload.get("requester_user") or ""),
            source_job_id=str(payload.get("source_job_id") or "").strip() or None,
            status=GeneratedExtensionJobStatus(
                str(payload.get("status") or GeneratedExtensionJobStatus.requested.value)
            ),
            visibility=str(payload.get("visibility") or "private"),
            request_payload=_json_dict(payload.get("request_payload_jsonb")),
            result_release_id=str(payload.get("result_release_id") or "").strip() or None,
            error_text=str(payload.get("error_text") or ""),
            created_at=_to_timestamp(payload.get("created_at")),
            updated_at=_to_timestamp(payload.get("updated_at")),
        )

    def list_generated_extension_jobs(
        self, *, account_id: str, limit: int = 100
    ) -> list[GeneratedExtensionJobRecord]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT generated_job_id, account_id, requester_user, source_job_id, status, visibility,
                       request_payload_jsonb, result_release_id, error_text, created_at, updated_at
                FROM generated_extension_jobs
                WHERE account_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (str(account_id or "").strip(), max(1, int(limit or 1))),
            )
            rows = cur.fetchall() or []
            desc = cur.description
        result: list[GeneratedExtensionJobRecord] = []
        for row in rows:
            payload = _row_to_dict(row, desc)
            mapped = self.get_generated_extension_job(str(payload.get("generated_job_id") or ""))
            if mapped is not None:
                result.append(mapped)
        return result

    # -- Registry projections --
    def upsert_projection(
        self, payload: ExtensionRegistryProjectionRecord | dict[str, Any]
    ) -> ExtensionRegistryProjectionRecord:
        row = (
            payload
            if isinstance(payload, ExtensionRegistryProjectionRecord)
            else ExtensionRegistryProjectionRecord.model_validate(payload)
        )
        normalized_projection = json.dumps(row.projection)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE extension_registry_projections
                SET account_id = %s,
                    release_id = %s,
                    extension_id = %s,
                    artifact_ref = %s,
                    projection_jsonb = %s::jsonb,
                    is_active = %s,
                    updated_at = now()
                WHERE install_id = %s
                  AND artifact_kind = %s
                  AND artifact_id = %s
                """,
                (
                    row.account_id,
                    row.release_id,
                    row.extension_id,
                    row.artifact_ref,
                    normalized_projection,
                    bool(row.is_active),
                    row.install_id,
                    row.artifact_kind,
                    row.artifact_id,
                ),
            )
            if cur.rowcount == 0:
                cur.execute(
                    """
                    INSERT INTO extension_registry_projections (
                        account_id, install_id, release_id, extension_id, artifact_kind, artifact_id, artifact_ref, projection_jsonb, is_active
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    """,
                    (
                        row.account_id,
                        row.install_id,
                        row.release_id,
                        row.extension_id,
                        row.artifact_kind,
                        row.artifact_id,
                        row.artifact_ref,
                        normalized_projection,
                        bool(row.is_active),
                    ),
                )
        return row

    def list_projections(
        self,
        *,
        account_id: str,
        artifact_kind: str = "",
        only_active: bool = True,
    ) -> list[ExtensionRegistryProjectionRecord]:
        conditions: list[Any] = [psycopg_sql.SQL("p.account_id = %s")]
        params: list[Any] = [str(account_id or "").strip()]
        if artifact_kind:
            conditions.append(psycopg_sql.SQL("p.artifact_kind = %s"))
            params.append(str(artifact_kind).strip())
        if only_active:
            conditions.extend(
                [
                    psycopg_sql.SQL("p.is_active = true"),
                    psycopg_sql.SQL(
                        """
                        EXISTS (
                            SELECT 1
                            FROM extension_installs i
                            WHERE i.install_id = p.install_id
                              AND i.account_id = p.account_id
                              AND i.state = 'active'
                        )
                        """
                    ),
                ]
            )
        where_clause = _compose_where_clause(conditions)
        with self._conn.cursor() as cur:
            cur.execute(
                psycopg_sql.SQL(
                    """
                SELECT p.account_id, p.install_id, p.release_id, p.extension_id,
                       p.artifact_kind, p.artifact_id, p.artifact_ref,
                       p.projection_jsonb, p.is_active, p.created_at, p.updated_at
                FROM extension_registry_projections p
                {where_clause}
                ORDER BY p.extension_id, p.artifact_kind, p.artifact_id
                """
                ).format(where_clause=where_clause),
                tuple(params),
            )
            rows = cur.fetchall() or []
            desc = cur.description
        result: list[ExtensionRegistryProjectionRecord] = []
        for row in rows:
            payload = _row_to_dict(row, desc)
            result.append(
                ExtensionRegistryProjectionRecord(
                    account_id=str(payload.get("account_id") or ""),
                    install_id=str(payload.get("install_id") or ""),
                    release_id=str(payload.get("release_id") or ""),
                    extension_id=str(payload.get("extension_id") or ""),
                    artifact_kind=str(payload.get("artifact_kind") or ""),
                    artifact_id=str(payload.get("artifact_id") or ""),
                    artifact_ref=str(payload.get("artifact_ref") or ""),
                    projection=_json_dict(payload.get("projection_jsonb")),
                    is_active=bool(payload.get("is_active")),
                    created_at=_to_timestamp(payload.get("created_at")),
                    updated_at=_to_timestamp(payload.get("updated_at")),
                )
            )
        return result

    def set_projection_active_for_install(self, install_id: str, *, is_active: bool) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE extension_registry_projections SET is_active = %s, updated_at = now() WHERE install_id = %s",
                (bool(is_active), str(install_id or "").strip()),
            )

    def delete_projections_for_install(self, install_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM extension_registry_projections WHERE install_id = %s",
                (str(install_id or "").strip(),),
            )
