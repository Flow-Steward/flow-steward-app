# mypy: ignore-errors
from __future__ import annotations

from typing import Self

from core.infrastructure.db import (
    job_attachment_store,
    project_registration_store,
    schema_store,
    secret_storage_store,
    system_settings_store,
)
from core.infrastructure.db import psycopg_runtime as psycopg_runtime
from core.infrastructure.db.db_pool import (
    PooledConnectionProxy,
    get_threaded_pool,
    pool_bounds,
    pooling_enabled,
    transaction_connection,
)
from core.infrastructure.db.platform_logger_common import (
    AESGCM,
    _attachment_master_key_bytes,
    _coerce_bytes,
    _core_config,
    _default_account_id,
    _default_tenant_id,
    _encrypt_attachment_value,
    _encrypt_secret_value,
    _secret_master_key_bytes,
    logger,
    resolve_observability_dsn,
)


class PlatformLoggerCoreMixin:
    def __init__(self, dsn):
        self.dsn = resolve_observability_dsn(dsn)
        self._direct_conn = None
        self._pool = None
        self._column_exists_cache: dict[tuple[str, str], bool] = {}
        if pooling_enabled() and self.dsn:
            min_conn, max_conn = pool_bounds()
            if max_conn > 1:
                self._pool = get_threaded_pool(self.dsn, minconn=min_conn, maxconn=max_conn)
                self.conn = PooledConnectionProxy(self._pool, dsn=self.dsn)
            else:
                self._direct_conn = psycopg_runtime.connect(self.dsn)
                self._direct_conn.autocommit = True
                self.conn = self._direct_conn
        else:
            self._direct_conn = psycopg_runtime.connect(self.dsn)
            self._direct_conn.autocommit = True
            self.conn = self._direct_conn
        self._lifecycle_starts = {}
        try:
            self._backfill_plaintext_secret_storage()
        except Exception as exc:
            logger.debug("secure storage backfill skipped (non-fatal): %s", exc)
        try:
            self._backfill_plaintext_attachment_storage()
        except Exception as exc:
            logger.debug("attachment storage backfill skipped (non-fatal): %s", exc)
        self._validate_attachment_encryption_schema()

    @classmethod
    def get_shared_instance(cls, dsn: object | None = None) -> Self:
        resolved = resolve_observability_dsn(dsn)
        if not resolved:
            resolved = resolve_observability_dsn(
                _core_config.CONFIG.get("fs_observability_dsn")
                or _core_config.CONFIG.get("fs_db_dsn")
                or ""
            )
        if not resolved:
            raise RuntimeError("Flow Steward observability DSN is not configured")
        existing = cls._shared_instances.get(resolved)
        if existing is not None:
            return existing
        instance = cls(resolved)
        cls._shared_instances[resolved] = instance
        return instance

    def cursor(self, **kwargs):
        """Expose a raw DB cursor for services that need direct table access."""
        return self.conn.cursor(**kwargs)

    def get_retention_settings(
        self,
        *,
        defaults: dict | None = None,
        account_id: str | None = None,
    ) -> dict:
        return system_settings_store.get_retention_settings(
            conn=self.conn,
            table_exists=self._table_exists,
            defaults=defaults,
            account_id=account_id,
        )

    def save_retention_settings(
        self,
        settings: dict | None,
        *,
        actor: str = "system",
        account_id: str | None = None,
    ) -> dict:
        return system_settings_store.save_retention_settings(
            conn=self.conn,
            settings=settings,
            actor=actor,
            account_id=account_id,
        )

    def get_security_posture_loop_status(self) -> dict:
        return system_settings_store.get_security_posture_loop_status(conn=self.conn)

    def record_security_posture_loop_attempt(self) -> dict:
        return system_settings_store.record_security_posture_loop_attempt(conn=self.conn)

    def record_security_posture_loop_success(self) -> dict:
        return system_settings_store.record_security_posture_loop_success(conn=self.conn)

    def record_security_posture_loop_failure(self, reason: str) -> dict:
        return system_settings_store.record_security_posture_loop_failure(
            conn=self.conn,
            reason=reason,
        )

    def get_workflow_knowledge_sync_loop_status(self) -> dict:
        return system_settings_store.get_workflow_knowledge_sync_loop_status(conn=self.conn)

    def record_workflow_knowledge_sync_loop_attempt(
        self,
        *,
        outbox_id: str = "",
        claim_owner: str = "",
        claim_version: int = 0,
    ) -> dict:
        return system_settings_store.record_workflow_knowledge_sync_loop_attempt(
            conn=self.conn,
            outbox_id=outbox_id,
            claim_owner=claim_owner,
            claim_version=claim_version,
        )

    def record_workflow_knowledge_sync_loop_success(self) -> dict:
        return system_settings_store.record_workflow_knowledge_sync_loop_success(conn=self.conn)

    def record_workflow_knowledge_sync_loop_failure(self, reason: str) -> dict:
        return system_settings_store.record_workflow_knowledge_sync_loop_failure(
            conn=self.conn,
            reason=reason,
        )

    def _validate_attachment_encryption_schema(self) -> None:
        return job_attachment_store.validate_attachment_encryption_schema(
            table_exists=self._table_exists,
            column_exists=self._column_exists,
        )

    def _backfill_plaintext_secret_storage(self) -> None:
        if AESGCM is None or not _secret_master_key_bytes():
            return None
        return secret_storage_store.backfill_plaintext_secret_storage(
            conn=self.conn,
            table_exists=self._table_exists,
            column_exists=self._column_exists,
            encrypt_secret_value=_encrypt_secret_value,
        )

    def _backfill_plaintext_attachment_storage(self) -> None:
        return job_attachment_store.backfill_plaintext_attachment_storage(
            conn=self.conn,
            attachment_encryption_enabled=bool(
                AESGCM is not None and _attachment_master_key_bytes()
            ),
            table_exists=self._table_exists,
            column_exists=self._column_exists,
            encrypt_attachment_value=lambda value: _encrypt_attachment_value(_coerce_bytes(value)),
        )

    def add_project(
        self,
        name,
        description=None,
        agents_config=None,
        project_rules=None,
        operator_default_workflow_id=None,
        project_workflow_ids=None,
        project_policy=None,
        config=None,
        extra=None,
        account_id=None,
        tenant_id=None,
        workspace_id=None,
        create_only=False,
    ):
        with transaction_connection(self.conn) as conn:
            return project_registration_store.add_project(
                conn=conn,
                table_exists=self._table_exists,
                column_exists=self._column_exists,
                sync_project_workflows_cur=self._sync_project_workflows_cur,
                save_project_policy_cur=self._save_project_policy_cur,
                default_account_id=_default_account_id,
                default_tenant_id=_default_tenant_id,
                name=name,
                description=description,
                agents_config=agents_config,
                project_rules=project_rules,
                operator_default_workflow_id=operator_default_workflow_id,
                project_workflow_ids=project_workflow_ids,
                project_policy=project_policy,
                config=config,
                extra=extra,
                account_id=account_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                create_only=create_only,
            )

    def _table_exists(self, table_name: str) -> bool:
        """Return True if table exists in public schema."""
        return schema_store.table_exists(
            conn=self.conn,
            table_name=table_name,
        )

    def _column_exists(self, table_name: str, column_name: str) -> bool:
        """Return True when a table column exists in public schema (cached per process)."""
        return schema_store.column_exists(
            conn=self.conn,
            cache=self._column_exists_cache,
            table_name=table_name,
            column_name=column_name,
        )
