"""
Scoped install model for extensions.

Extension artifacts are portable — they contain no account identity.
Installation state is account-scoped: the same extension can be installed
independently into multiple accounts with different visibility.

Two store implementations:
- ``ExtensionInstallStore`` — in-memory, used for tests and offline CLI
- ``DBExtensionInstallStore`` — PostgreSQL-backed, used when a DB connection is available
"""

from __future__ import annotations

import json
import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.application.extensions.release_membership_lock import (
    EXTENSION_RELEASE_MEMBERSHIP_LOCK_NAMESPACE,
    extension_release_membership_lock_key,
)
from core.infrastructure.db.psycopg_runtime import sql as psycopg_sql


def _install_table_identifier() -> psycopg_sql.Identifier:
    return psycopg_sql.Identifier(_INSTALL_TABLE)


def _generate_install_id() -> str:
    return f"pi_{uuid.uuid4().hex[:24]}"


class InstallVisibility(StrEnum):
    """Who can see artifacts from an installed extension."""

    account = "account"  # visible only to the installing account
    global_ = "global"  # visible to all accounts


class InstallState(StrEnum):
    """Lifecycle state of a extension installation."""

    active = "active"
    disabled = "disabled"
    pending = "pending"


class ExtensionInstallRecord(BaseModel):
    """Per-account installation record for a extension.

    The extension artifact itself carries no account identity.
    Scope and visibility belong here, not in the extension bundle.
    """

    model_config = ConfigDict(extra="forbid")

    extension_id: str
    version: str
    account_id: str
    visibility: InstallVisibility = InstallVisibility.account
    state: InstallState = InstallState.active
    installed_by: str = ""
    installed_at: float = Field(default_factory=time.time)
    install_id: str = Field(default_factory=_generate_install_id)
    release_id: str | None = None
    descriptor_schema_version: str = "extension_descriptor_v1"
    descriptor_origin: str = "imported"
    descriptor_source: str = "file"
    provenance: dict[str, Any] = Field(default_factory=dict)
    runtime_mode: str = ""
    remote_base_url: str = ""
    handshake_status: str = "unknown"
    health_status: str = "unknown"
    runtime_status: str = "unknown"
    last_error: str = ""
    applied_data_schema_version: int = Field(default=1, ge=1)
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("extension_id", "account_id", "install_id", mode="before")
    @classmethod
    def _require_nonempty(cls, v: Any) -> str:
        value = str(v or "").strip()
        if not value:
            raise ValueError("Value is required")
        return value


class ExtensionInstallStore:
    """In-memory install record store.

    Production deployments replace this with a DB-backed implementation.
    The interface is deliberately minimal to keep the model clear. In-memory
    mutations are outside the lifecycle transaction and require compensation
    when a later install step fails.
    """

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], ExtensionInstallRecord] = {}

    @staticmethod
    def participates_in_transaction(_transaction_connection: Any) -> bool:
        return False

    def install(
        self,
        *,
        extension_id: str,
        version: str,
        account_id: str,
        visibility: str = "account",
        installed_by: str = "",
        config: dict[str, Any] | None = None,
        install_id: str = "",
        release_id: str | None = None,
        descriptor_schema_version: str = "extension_descriptor_v1",
        descriptor_origin: str = "imported",
        descriptor_source: str = "file",
        provenance: dict[str, Any] | None = None,
        runtime_mode: str = "",
        remote_base_url: str = "",
        handshake_status: str = "unknown",
        health_status: str = "unknown",
        runtime_status: str = "unknown",
        last_error: str = "",
        applied_data_schema_version: int = 1,
    ) -> ExtensionInstallRecord:
        """Create or update an install record for a extension in an account."""
        existing = self._records.get((extension_id, account_id))
        resolved_install_id = (
            str(install_id or (existing.install_id if existing else "")).strip()
            or _generate_install_id()
        )
        record = ExtensionInstallRecord(
            extension_id=extension_id,
            version=version,
            account_id=account_id,
            visibility=InstallVisibility(visibility),
            state=InstallState.active,
            installed_by=installed_by,
            install_id=resolved_install_id,
            release_id=str(release_id or "").strip() or None,
            descriptor_schema_version=str(
                descriptor_schema_version or "extension_descriptor_v1"
            ).strip()
            or "extension_descriptor_v1",
            descriptor_origin=str(descriptor_origin or "imported").strip() or "imported",
            descriptor_source=str(descriptor_source or "file").strip() or "file",
            provenance=dict(provenance or {}),
            runtime_mode=str(runtime_mode or "").strip(),
            remote_base_url=str(remote_base_url or "").strip(),
            handshake_status=str(handshake_status or "unknown").strip() or "unknown",
            health_status=str(health_status or "unknown").strip() or "unknown",
            runtime_status=str(runtime_status or "unknown").strip() or "unknown",
            last_error=str(last_error or "").strip(),
            applied_data_schema_version=applied_data_schema_version,
            config=config or {},
        )
        self._records[(extension_id, account_id)] = record
        return record

    def uninstall(self, *, extension_id: str, account_id: str) -> bool:
        """Remove an install record. Returns True if it existed."""
        return self._records.pop((extension_id, account_id), None) is not None

    def get(self, *, extension_id: str, account_id: str) -> ExtensionInstallRecord | None:
        """Get install record for a specific extension+account."""
        return self._records.get((extension_id, account_id))

    def get_by_install_id(self, install_id: str) -> ExtensionInstallRecord | None:
        normalized_install_id = str(install_id or "").strip()
        if not normalized_install_id:
            return None
        for record in self._records.values():
            if record.install_id == normalized_install_id:
                return record
        return None

    def list_for_account(
        self,
        account_id: str,
        *,
        include_global: bool = True,
        include_inactive_owned: bool = False,
    ) -> list[ExtensionInstallRecord]:
        """List extensions visible to an account.

        Returns extensions installed directly into this account, plus
        global-visibility extensions from any account if ``include_global``.
        """
        results: list[ExtensionInstallRecord] = []
        for record in self._records.values():
            if record.account_id == account_id:
                if record.state == InstallState.active or include_inactive_owned:
                    results.append(record)
                continue
            if record.state != InstallState.active:
                continue
            if include_global and record.visibility == InstallVisibility.global_:
                results.append(record)
        return results

    def list_all_installs(self, extension_id: str) -> list[ExtensionInstallRecord]:
        """List all install records for a extension across accounts."""
        return [r for r in self._records.values() if r.extension_id == extension_id]

    def list_active_installs(self) -> list[ExtensionInstallRecord]:
        """List every active install record across accounts."""
        return [r for r in self._records.values() if r.state == InstallState.active]

    def set_visibility(
        self,
        *,
        extension_id: str,
        account_id: str,
        visibility: str,
    ) -> ExtensionInstallRecord | None:
        """Update visibility of an existing install."""
        record = self._records.get((extension_id, account_id))
        if record is None:
            return None
        record.visibility = InstallVisibility(visibility)
        return record

    def set_provenance(
        self,
        *,
        extension_id: str,
        account_id: str,
        provenance: dict[str, Any],
    ) -> ExtensionInstallRecord | None:
        record = self._records.get((extension_id, account_id))
        if record is None:
            return None
        record.provenance = dict(provenance)
        return record

    def disable(self, *, extension_id: str, account_id: str) -> ExtensionInstallRecord | None:
        """Disable a extension install without removing it."""
        record = self._records.get((extension_id, account_id))
        if record is None:
            return None
        record.state = InstallState.disabled
        return record

    def set_state(
        self, *, extension_id: str, account_id: str, state: str
    ) -> ExtensionInstallRecord | None:
        record = self._records.get((extension_id, account_id))
        if record is None:
            return None
        record.state = InstallState(str(state or "active"))
        return record

    def set_runtime_state(
        self,
        *,
        install_id: str,
        runtime_mode: str | None = None,
        remote_base_url: str | None = None,
        handshake_status: str | None = None,
        health_status: str | None = None,
        runtime_status: str | None = None,
        last_error: str | None = None,
    ) -> ExtensionInstallRecord | None:
        record = self.get_by_install_id(install_id)
        if record is None:
            return None
        if runtime_mode is not None:
            record.runtime_mode = str(runtime_mode or "").strip()
        if remote_base_url is not None:
            record.remote_base_url = str(remote_base_url or "").strip()
        if handshake_status is not None:
            record.handshake_status = str(handshake_status or "unknown").strip() or "unknown"
        if health_status is not None:
            record.health_status = str(health_status or "unknown").strip() or "unknown"
        if runtime_status is not None:
            record.runtime_status = str(runtime_status or "unknown").strip() or "unknown"
        if last_error is not None:
            record.last_error = str(last_error or "").strip()
        return record


# ---------------------------------------------------------------------------
# DB-backed install store
# ---------------------------------------------------------------------------

_INSTALL_TABLE = "extension_installs"


def _row_to_record(row: dict) -> ExtensionInstallRecord:
    config_raw = row.get("config_jsonb") or "{}"
    if isinstance(config_raw, str):
        config = json.loads(config_raw)
    elif isinstance(config_raw, dict):
        config = config_raw
    else:
        config = {}
    installed_at_val = row.get("installed_at")
    timestamp_fn = getattr(installed_at_val, "timestamp", None)
    if callable(timestamp_fn):
        installed_at_float = float(timestamp_fn())
    elif isinstance(installed_at_val, (int, float)):
        installed_at_float = float(installed_at_val)
    else:
        installed_at_float = time.time()
    provenance_raw = row.get("provenance_jsonb")
    if isinstance(provenance_raw, dict):
        provenance = dict(provenance_raw)
    else:
        try:
            parsed_provenance = json.loads(str(provenance_raw or "{}"))
            provenance = dict(parsed_provenance) if isinstance(parsed_provenance, dict) else {}
        except Exception:
            provenance = {}

    vis_raw = str(row.get("visibility") or "account").strip()
    visibility = (
        InstallVisibility(vis_raw)
        if vis_raw in ("account", "global")
        else InstallVisibility.account
    )

    return ExtensionInstallRecord(
        extension_id=str(row.get("extension_id") or ""),
        version=str(row.get("version") or "1.0.0"),
        account_id=str(row.get("account_id") or ""),
        install_id=str(row.get("install_id") or "").strip() or _generate_install_id(),
        release_id=str(row.get("release_id") or "").strip() or None,
        descriptor_schema_version=str(
            row.get("descriptor_schema_version") or "extension_descriptor_v1"
        ).strip()
        or "extension_descriptor_v1",
        descriptor_origin=str(row.get("descriptor_origin") or "imported").strip() or "imported",
        descriptor_source=str(row.get("descriptor_source") or "file").strip() or "file",
        provenance=provenance,
        runtime_mode=str(row.get("runtime_mode") or "").strip(),
        remote_base_url=str(row.get("remote_base_url") or "").strip(),
        handshake_status=str(row.get("handshake_status") or "unknown").strip() or "unknown",
        health_status=str(row.get("health_status") or "unknown").strip() or "unknown",
        runtime_status=str(row.get("runtime_status") or "unknown").strip() or "unknown",
        last_error=str(row.get("last_error") or "").strip(),
        applied_data_schema_version=int(row.get("applied_data_schema_version") or 1),
        visibility=visibility,
        state=InstallState(str(row.get("state") or "active")),
        installed_by=str(row.get("installed_by") or ""),
        installed_at=installed_at_float,
        config=config,
    )


def _cursor_row_to_dict(row: tuple, description: Any) -> dict:
    """Convert a psycopg_runtime tuple row to a dict using cursor.description."""
    if isinstance(row, dict):
        return row
    columns = [col[0] if hasattr(col, "__getitem__") else col.name for col in description]
    return dict(zip(columns, row, strict=False))


class DBExtensionInstallStore:
    """PostgreSQL-backed extension install store.

    Uses the same connection pattern as RegistryDBStore/Flow Steward logger:
    autocommit with explicit transactions where needed.

    Public lifecycle commands bind this store to their enclosing transaction,
    so an escaping failure rolls its mutations back without compensating SQL.

    The ``extension_installs`` table is owned by ``db/schema/*.sql``.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def participates_in_transaction(self, transaction_connection: Any) -> bool:
        from core.infrastructure.db.db_pool import connection_participates_in_transaction

        return connection_participates_in_transaction(self._conn, transaction_connection)

    def lock_release_membership(self, release_id: str) -> None:
        """Serialize the active command transaction before its target release read."""
        membership_lock_key = extension_release_membership_lock_key(release_id)
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                (EXTENSION_RELEASE_MEMBERSHIP_LOCK_NAMESPACE, membership_lock_key),
            )

    def install(
        self,
        *,
        extension_id: str,
        version: str,
        account_id: str,
        visibility: str = "account",
        installed_by: str = "",
        config: dict[str, Any] | None = None,
        install_id: str = "",
        release_id: str | None = None,
        descriptor_schema_version: str = "extension_descriptor_v1",
        descriptor_origin: str = "imported",
        descriptor_source: str = "file",
        provenance: dict[str, Any] | None = None,
        runtime_mode: str = "",
        remote_base_url: str = "",
        handshake_status: str = "unknown",
        health_status: str = "unknown",
        runtime_status: str = "unknown",
        last_error: str = "",
        applied_data_schema_version: int = 1,
    ) -> ExtensionInstallRecord:
        resolved_install_id = str(install_id or "").strip()
        if not resolved_install_id:
            existing = self.get(extension_id=extension_id, account_id=account_id)
            if existing is not None:
                resolved_install_id = existing.install_id
        if not resolved_install_id:
            resolved_install_id = _generate_install_id()
        config_json = json.dumps(config or {})
        provenance_json = json.dumps(provenance or {})
        normalized_release_id = str(release_id or "").strip() or None
        membership_lock_key = extension_release_membership_lock_key(
            normalized_release_id,
            extension_id=extension_id,
        )
        with self._conn.cursor() as cur:
            cur.execute(
                psycopg_sql.SQL(
                    """
                WITH extension_release_membership_lock AS MATERIALIZED (
                    SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))
                )
                INSERT INTO {_INSTALL_TABLE}
                    (
                        extension_id, version, account_id, install_id, release_id,
                        descriptor_schema_version, descriptor_origin, descriptor_source,
                        provenance_jsonb, runtime_mode, remote_base_url,
                        handshake_status, health_status, runtime_status, last_error,
                        visibility, state, installed_by, applied_data_schema_version, config_jsonb
                    )
                SELECT
                    %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s::jsonb, %s, %s,
                    %s, %s, %s, %s,
                    %s, 'active', %s, %s, %s
                FROM extension_release_membership_lock
                ON CONFLICT (extension_id, account_id)
                DO UPDATE SET
                    install_id = EXCLUDED.install_id,
                    release_id = EXCLUDED.release_id,
                    descriptor_schema_version = EXCLUDED.descriptor_schema_version,
                    descriptor_origin = EXCLUDED.descriptor_origin,
                    descriptor_source = EXCLUDED.descriptor_source,
                    provenance_jsonb = EXCLUDED.provenance_jsonb,
                    runtime_mode = EXCLUDED.runtime_mode,
                    remote_base_url = EXCLUDED.remote_base_url,
                    handshake_status = EXCLUDED.handshake_status,
                    health_status = EXCLUDED.health_status,
                    runtime_status = EXCLUDED.runtime_status,
                    last_error = EXCLUDED.last_error,
                    version = EXCLUDED.version,
                    visibility = EXCLUDED.visibility,
                    state = 'active',
                    installed_by = EXCLUDED.installed_by,
                    installed_at = now(),
                    applied_data_schema_version = EXCLUDED.applied_data_schema_version,
                    config_jsonb = EXCLUDED.config_jsonb
                """
                ).format(_INSTALL_TABLE=_install_table_identifier()),
                (
                    EXTENSION_RELEASE_MEMBERSHIP_LOCK_NAMESPACE,
                    membership_lock_key,
                    extension_id,
                    version,
                    account_id,
                    resolved_install_id,
                    normalized_release_id,
                    str(descriptor_schema_version or "extension_descriptor_v1").strip()
                    or "extension_descriptor_v1",
                    str(descriptor_origin or "imported").strip() or "imported",
                    str(descriptor_source or "file").strip() or "file",
                    provenance_json,
                    str(runtime_mode or "").strip() or None,
                    str(remote_base_url or "").strip() or None,
                    str(handshake_status or "unknown").strip() or "unknown",
                    str(health_status or "unknown").strip() or "unknown",
                    str(runtime_status or "unknown").strip() or "unknown",
                    str(last_error or "").strip(),
                    visibility,
                    installed_by,
                    applied_data_schema_version,
                    config_json,
                ),
            )
        return ExtensionInstallRecord(
            extension_id=extension_id,
            version=version,
            account_id=account_id,
            install_id=resolved_install_id,
            release_id=str(release_id or "").strip() or None,
            descriptor_schema_version=str(
                descriptor_schema_version or "extension_descriptor_v1"
            ).strip()
            or "extension_descriptor_v1",
            descriptor_origin=str(descriptor_origin or "imported").strip() or "imported",
            descriptor_source=str(descriptor_source or "file").strip() or "file",
            provenance=dict(provenance or {}),
            runtime_mode=str(runtime_mode or "").strip(),
            remote_base_url=str(remote_base_url or "").strip(),
            handshake_status=str(handshake_status or "unknown").strip() or "unknown",
            health_status=str(health_status or "unknown").strip() or "unknown",
            runtime_status=str(runtime_status or "unknown").strip() or "unknown",
            last_error=str(last_error or "").strip(),
            applied_data_schema_version=applied_data_schema_version,
            visibility=InstallVisibility(visibility),
            state=InstallState.active,
            installed_by=installed_by,
            config=config or {},
        )

    def uninstall(self, *, extension_id: str, account_id: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                psycopg_sql.SQL(
                    "DELETE FROM {table} WHERE extension_id = %s AND account_id = %s"
                ).format(
                    table=_install_table_identifier(),
                ),
                (extension_id, account_id),
            )
            return cur.rowcount > 0

    def get(self, *, extension_id: str, account_id: str) -> ExtensionInstallRecord | None:
        with self._conn.cursor() as cur:
            cur.execute(
                psycopg_sql.SQL(
                    "SELECT * FROM {table} WHERE extension_id = %s AND account_id = %s"
                ).format(
                    table=_install_table_identifier(),
                ),
                (extension_id, account_id),
            )
            row = cur.fetchone()
            if not row:
                return None
            return _row_to_record(_cursor_row_to_dict(row, cur.description))

    def get_by_install_id(self, install_id: str) -> ExtensionInstallRecord | None:
        with self._conn.cursor() as cur:
            cur.execute(
                psycopg_sql.SQL("SELECT * FROM {table} WHERE install_id = %s").format(
                    table=_install_table_identifier(),
                ),
                (str(install_id or "").strip(),),
            )
            row = cur.fetchone()
            if not row:
                return None
            return _row_to_record(_cursor_row_to_dict(row, cur.description))

    def list_for_account(
        self,
        account_id: str,
        *,
        include_global: bool = True,
        include_inactive_owned: bool = False,
    ) -> list[ExtensionInstallRecord]:
        with self._conn.cursor() as cur:
            if include_global:
                if include_inactive_owned:
                    cur.execute(
                        psycopg_sql.SQL(
                            """
                        SELECT * FROM {table}
                        WHERE account_id = %s
                           OR (visibility = 'global' AND state = 'active')
                        ORDER BY extension_id
                        """
                        ).format(table=_install_table_identifier()),
                        (account_id,),
                    )
                else:
                    cur.execute(
                        psycopg_sql.SQL(
                            """
                        SELECT * FROM {table}
                        WHERE state = 'active'
                          AND (account_id = %s OR visibility = 'global')
                        ORDER BY extension_id
                        """
                        ).format(table=_install_table_identifier()),
                        (account_id,),
                    )
            else:
                if include_inactive_owned:
                    cur.execute(
                        psycopg_sql.SQL(
                            """
                        SELECT * FROM {table}
                        WHERE account_id = %s
                        ORDER BY extension_id
                        """
                        ).format(table=_install_table_identifier()),
                        (account_id,),
                    )
                else:
                    cur.execute(
                        psycopg_sql.SQL(
                            """
                        SELECT * FROM {table}
                        WHERE state = 'active' AND account_id = %s
                        ORDER BY extension_id
                        """
                        ).format(table=_install_table_identifier()),
                        (account_id,),
                    )
            rows = cur.fetchall()
            return [_row_to_record(_cursor_row_to_dict(r, cur.description)) for r in rows]

    def list_all_installs(self, extension_id: str) -> list[ExtensionInstallRecord]:
        with self._conn.cursor() as cur:
            cur.execute(
                psycopg_sql.SQL(
                    "SELECT * FROM {table} WHERE extension_id = %s ORDER BY account_id"
                ).format(
                    table=_install_table_identifier(),
                ),
                (extension_id,),
            )
            rows = cur.fetchall()
            return [_row_to_record(_cursor_row_to_dict(r, cur.description)) for r in rows]

    def list_active_installs(self) -> list[ExtensionInstallRecord]:
        with self._conn.cursor() as cur:
            cur.execute(
                psycopg_sql.SQL(
                    "SELECT * FROM {table} WHERE state = 'active' ORDER BY account_id, extension_id"
                ).format(
                    table=_install_table_identifier(),
                )
            )
            rows = cur.fetchall()
            return [_row_to_record(_cursor_row_to_dict(r, cur.description)) for r in rows]

    def set_visibility(
        self,
        *,
        extension_id: str,
        account_id: str,
        visibility: str,
    ) -> ExtensionInstallRecord | None:
        with self._conn.cursor() as cur:
            cur.execute(
                psycopg_sql.SQL(
                    """
                UPDATE {table}
                SET visibility = %s
                WHERE extension_id = %s AND account_id = %s
                """
                ).format(table=_install_table_identifier()),
                (visibility, extension_id, account_id),
            )
            if cur.rowcount == 0:
                return None
            return self.get(extension_id=extension_id, account_id=account_id)

    def set_provenance(
        self,
        *,
        extension_id: str,
        account_id: str,
        provenance: dict[str, Any],
    ) -> ExtensionInstallRecord | None:
        with self._conn.cursor() as cur:
            cur.execute(
                psycopg_sql.SQL(
                    """
                    UPDATE {table}
                    SET provenance_jsonb = %s::jsonb
                    WHERE extension_id = %s AND account_id = %s
                    """
                ).format(table=_install_table_identifier()),
                (json.dumps(provenance), extension_id, account_id),
            )
            if cur.rowcount <= 0:
                return None
        return self.get(extension_id=extension_id, account_id=account_id)

    def disable(self, *, extension_id: str, account_id: str) -> ExtensionInstallRecord | None:
        with self._conn.cursor() as cur:
            cur.execute(
                psycopg_sql.SQL(
                    """
                UPDATE {table}
                SET state = 'disabled'
                WHERE extension_id = %s AND account_id = %s
                """
                ).format(table=_install_table_identifier()),
                (extension_id, account_id),
            )
            if cur.rowcount == 0:
                return None
        return self.get(extension_id=extension_id, account_id=account_id)

    def set_state(
        self, *, extension_id: str, account_id: str, state: str
    ) -> ExtensionInstallRecord | None:
        normalized_state = str(state or "active").strip() or "active"
        with self._conn.cursor() as cur:
            cur.execute(
                psycopg_sql.SQL(
                    """
                UPDATE {table}
                SET state = %s
                WHERE extension_id = %s AND account_id = %s
                """
                ).format(table=_install_table_identifier()),
                (normalized_state, extension_id, account_id),
            )
            if cur.rowcount == 0:
                return None
        return self.get(extension_id=extension_id, account_id=account_id)

    def set_runtime_state(
        self,
        *,
        install_id: str,
        runtime_mode: str | None = None,
        remote_base_url: str | None = None,
        handshake_status: str | None = None,
        health_status: str | None = None,
        runtime_status: str | None = None,
        last_error: str | None = None,
    ) -> ExtensionInstallRecord | None:
        updates: list[str] = []
        values: list[Any] = []
        if runtime_mode is not None:
            updates.append("runtime_mode = %s")
            values.append(str(runtime_mode or "").strip() or None)
        if remote_base_url is not None:
            updates.append("remote_base_url = %s")
            values.append(str(remote_base_url or "").strip() or None)
        if handshake_status is not None:
            updates.append("handshake_status = %s")
            values.append(str(handshake_status or "unknown").strip() or "unknown")
            updates.append("last_handshake_at = now()")
        if health_status is not None:
            updates.append("health_status = %s")
            values.append(str(health_status or "unknown").strip() or "unknown")
            updates.append("last_health_at = now()")
        if runtime_status is not None:
            updates.append("runtime_status = %s")
            values.append(str(runtime_status or "unknown").strip() or "unknown")
        if last_error is not None:
            updates.append("last_error = %s")
            values.append(str(last_error or "").strip())
        if not updates:
            return self.get_by_install_id(install_id)
        values.append(str(install_id or "").strip())
        set_clause = psycopg_sql.SQL(", ").join(psycopg_sql.SQL(update) for update in updates)
        with self._conn.cursor() as cur:
            cur.execute(
                psycopg_sql.SQL("UPDATE {table} SET {set_clause} WHERE install_id = %s").format(
                    table=_install_table_identifier(),
                    set_clause=set_clause,
                ),
                tuple(values),
            )
            if cur.rowcount == 0:
                return None
        return self.get_by_install_id(install_id)
