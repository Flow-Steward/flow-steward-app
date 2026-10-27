from __future__ import annotations

import contextlib
import hashlib
import json
import queue
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions.command_service import ExtensionCommandService
from core.application.extensions.extension_scan_lifecycle import (
    sync_extension_scan_with_llm_lifecycle,
)
from core.application.extensions.install import DBExtensionInstallStore
from core.application.extensions.oauth_configuration_secrets import (
    configuration_secret_ref_key,
)
from core.application.extensions.oauth_contract import (
    compile_oauth_providers,
    provider_revocation_fingerprint,
    provider_security_fingerprint,
)
from core.application.extensions.oauth_repository import ExtensionOAuthRepository
from core.application.extensions.runtime_store_db import DBExtensionRuntimeStore
from core.application.management.command_models import ExtensionReleaseInstallInput
from core.application.management.errors import ServicePreconditionError
from core.application.management.project_connection_secrets import connection_secret_ref_key
from core.application.management.scope import ScopeContext
from core.application.settings import llm_provider_extension_repository as llm_store
from core.infrastructure.db import psycopg_runtime
from core.infrastructure.db.logger_extensions_accounts_runtime_mixin import (
    PlatformLoggerExtensionsAccountsRuntimeMixin,
)
from core.infrastructure.db.logger_extensions_accounts_sync_mixin import (
    PlatformLoggerExtensionsAccountsSyncMixin,
)
from tests import _disposable_db
from tests._db_env import resolve_test_db_dsn


def _set_search_path(conn: Any, schema_name: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            psycopg_runtime.sql.SQL("SET search_path TO {}").format(
                psycopg_runtime.sql.Identifier(schema_name)
            )
        )


def _create_isolated_schema(conn: Any, schema_name: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            psycopg_runtime.sql.SQL("CREATE SCHEMA {}").format(
                psycopg_runtime.sql.Identifier(schema_name)
            )
        )
    _set_search_path(conn, schema_name)
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE extension_bundles (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                extension_key text NOT NULL UNIQUE,
                kind text NOT NULL,
                display_name text NOT NULL DEFAULT '',
                description text NOT NULL DEFAULT '',
                bundle_root text NOT NULL DEFAULT '',
                manifest_path text NOT NULL DEFAULT '',
                latest_version text,
                status text NOT NULL DEFAULT 'valid',
                last_scan_error text
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE extension_versions (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                bundle_id uuid NOT NULL,
                version text NOT NULL,
                manifest_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                compiled_manifest_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                features_jsonb jsonb NOT NULL DEFAULT '[]'::jsonb,
                schemas_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                healthcheck_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                entrypoint_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                checksum text,
                status text NOT NULL DEFAULT 'valid',
                scan_error text,
                scanned_at timestamptz NOT NULL DEFAULT now(),
                UNIQUE (bundle_id, version)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE extension_releases (
                release_id text PRIMARY KEY,
                extension_id text NOT NULL,
                release_version text NOT NULL DEFAULT '1.0.0',
                descriptor_schema_version text NOT NULL DEFAULT 'extension_descriptor_v1',
                descriptor_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                descriptor_sha256 text NOT NULL DEFAULT '',
                channel text NOT NULL DEFAULT 'private_byo',
                execution_modes_jsonb jsonb NOT NULL DEFAULT '[]'::jsonb,
                contract_version text NOT NULL DEFAULT '',
                provenance_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                source_kind text NOT NULL DEFAULT 'file',
                source_ref text NOT NULL DEFAULT '',
                account_id uuid,
                created_by text NOT NULL DEFAULT '',
                created_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE extension_artifact_snapshots (
                release_id text NOT NULL,
                artifact_kind text NOT NULL,
                artifact_id text NOT NULL,
                artifact_ref text NOT NULL DEFAULT '',
                publish boolean NOT NULL DEFAULT true,
                snapshot_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                created_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (release_id, artifact_kind, artifact_id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE extension_installs (
                extension_id text NOT NULL,
                version text NOT NULL DEFAULT '1.0.0',
                account_id uuid NOT NULL,
                visibility text NOT NULL DEFAULT 'account',
                state text NOT NULL DEFAULT 'active',
                state_reason text,
                disabled_at timestamptz,
                installed_by text NOT NULL DEFAULT '',
                installed_at timestamptz NOT NULL DEFAULT now(),
                config_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                install_id text NOT NULL UNIQUE,
                release_id text,
                descriptor_schema_version text DEFAULT 'extension_descriptor_v1',
                descriptor_origin text DEFAULT 'imported',
                descriptor_source text DEFAULT 'file',
                provenance_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                runtime_mode text,
                remote_base_url text,
                handshake_status text NOT NULL DEFAULT 'unknown',
                health_status text NOT NULL DEFAULT 'unknown',
                runtime_status text NOT NULL DEFAULT 'unknown',
                last_error text,
                applied_data_schema_version integer NOT NULL DEFAULT 1,
                PRIMARY KEY (extension_id, account_id),
                CONSTRAINT extension_installs_release_id_fkey
                    FOREIGN KEY (release_id)
                    REFERENCES extension_releases(release_id)
                    ON DELETE RESTRICT,
                CONSTRAINT extension_installs_applied_data_schema_version_check
                    CHECK (applied_data_schema_version >= 1),
                CONSTRAINT extension_installs_runtime_status_check
                    CHECK (runtime_status IN ('unknown', 'compatible', 'incompatible')),
                CONSTRAINT extension_installs_handshake_status_check
                    CHECK (handshake_status IN ('unknown', 'ok', 'failed')),
                CONSTRAINT extension_installs_health_status_check
                    CHECK (health_status IN ('unknown', 'ok', 'failed')),
                CONSTRAINT extension_installs_remote_activation_url_check
                    CHECK (
                        state <> 'active'
                        OR runtime_mode IS DISTINCT FROM 'remote_https'
                        OR COALESCE(btrim(remote_base_url), '') <> ''
                    ),
                CONSTRAINT extension_installs_state_check
                    CHECK (
                        state IN (
                            'active', 'disabled', 'pending',
                            'uninstall_pending_drain', 'uninstalled'
                        )
                    )
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE llm_provider_sources (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                account_id uuid NOT NULL,
                extension_install_id text NOT NULL,
                extension_id text NOT NULL,
                provider_key text NOT NULL,
                UNIQUE (account_id, provider_key)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE llm_provider_models (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                account_id uuid NOT NULL,
                provider_source_id uuid NOT NULL,
                model_id text NOT NULL,
                enabled boolean NOT NULL DEFAULT false,
                verification_status text NOT NULL DEFAULT 'unverified',
                verification_generation text,
                verified_capabilities_jsonb jsonb NOT NULL DEFAULT '[]'::jsonb,
                verification_error_safe text,
                verified_at timestamptz,
                lifecycle text NOT NULL DEFAULT 'active',
                updated_at timestamptz NOT NULL DEFAULT now(),
                UNIQUE (account_id, provider_source_id, model_id),
                CONSTRAINT llm_provider_models_source_fkey
                    FOREIGN KEY (provider_source_id)
                    REFERENCES llm_provider_sources(id)
                    ON DELETE CASCADE,
                CONSTRAINT llm_provider_models_verification_status_check
                    CHECK (
                        verification_status IN (
                            'unverified', 'pending', 'passed', 'failed', 'stale'
                        )
                    ),
                CONSTRAINT llm_provider_models_lifecycle_check
                    CHECK (lifecycle IN ('active', 'deprecated', 'shutdown'))
            )
            """
        )
    conn.commit()


class _IsolatedScannerLogger(
    PlatformLoggerExtensionsAccountsSyncMixin,
    PlatformLoggerExtensionsAccountsRuntimeMixin,
):
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def _table_exists(self, table_name: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s) IS NOT NULL", (str(table_name),))
            return bool(cur.fetchone()[0])

    def _column_exists(self, table_name: str, column_name: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                      FROM information_schema.columns
                     WHERE table_schema = current_schema()
                       AND table_name = %s
                       AND column_name = %s
                )
                """,
                (str(table_name), str(column_name)),
            )
            return bool(cur.fetchone()[0])


class _ReleaseReader:
    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._store = DBExtensionRuntimeStore(conn)

    def participates_in_transaction(self, transaction_connection: Any) -> bool:
        return self._store.participates_in_transaction(transaction_connection)

    def get_release(self, release_id: str):
        return self._store.get_release(release_id)


class _ReleaseReaderWithDelete(_ReleaseReader):
    """Expose the old eager-GC surface without enabling other runtime writes."""

    def __init__(self, conn: Any) -> None:
        super().__init__(conn)
        self._conn = conn

    def delete_release(self, release_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM extension_releases WHERE release_id = %s",
                (str(release_id),),
            )


class _TrackingInstallStore(DBExtensionInstallStore):
    def __init__(
        self,
        conn: Any,
        *,
        scan_can_finish: threading.Event,
        early_install_read: threading.Event,
        membership_lock_attempted: threading.Event,
        install_upsert_started: threading.Event,
    ) -> None:
        super().__init__(conn)
        self._scan_can_finish = scan_can_finish
        self._early_install_read = early_install_read
        self._membership_lock_attempted = membership_lock_attempted
        self._install_upsert_started = install_upsert_started

    def lock_release_membership(self, release_id: str) -> None:
        self._membership_lock_attempted.set()
        parent_lock = getattr(super(), "lock_release_membership", None)
        if callable(parent_lock):
            parent_lock(release_id)

    def get(self, *, extension_id: str, account_id: str):
        if not self._scan_can_finish.is_set():
            self._early_install_read.set()
        return super().get(extension_id=extension_id, account_id=account_id)

    def install(self, **kwargs):
        self._install_upsert_started.set()
        return super().install(**kwargs)


def _descriptor(*, extension_id: str, remote_base_url: str, revision: str) -> dict[str, Any]:
    return {
        "extension_id": extension_id,
        "kind": "tool_provider",
        "version": "1.0.0",
        "description": f"descriptor-{revision}",
        "distribution": {"execution_modes": ["remote_https"]},
        "entrypoint": {"mode": "remote_https"},
        "runtime": {
            "contract_version": "extension_host_v2",
            "remote": {
                "base_url": remote_base_url,
                "endpoints": {
                    "handshake": "/handshake",
                    "health": "/health",
                },
            },
        },
    }


def _scan_payload(
    *, extension_id: str, descriptor: dict[str, Any], account_id: str
) -> list[dict[str, Any]]:
    return [
        {
            "extension_key": extension_id,
            "kind": "tool_provider",
            "status": "valid",
            "display_name": "Repoint ordering fixture",
            "version": "1.0.0",
            "manifest": descriptor,
            "compiled_manifest": descriptor,
            "account_id": account_id,
            "config": {},
        }
    ]


@pytest.mark.integration
def test_legacy_enable_preserves_same_version_enriched_release() -> None:
    dsn = str(resolve_test_db_dsn() or "").strip()
    if not dsn:
        pytest.skip("FS_DB_DSN is required for enriched release preservation verification")

    account_id = DEFAULT_ACCOUNT_ID
    extension_id = f"fixture.enriched-enable-{uuid.uuid4().hex}"
    enriched_release_id = f"release-enriched-{uuid.uuid4().hex}"
    schema_name = f"extension_enriched_enable_{uuid.uuid4().hex}"
    setup = psycopg_runtime.connect(dsn)
    try:
        _create_isolated_schema(setup, schema_name)
        with setup.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE enabled_extensions (
                    bundle_id uuid PRIMARY KEY,
                    version_id uuid NOT NULL,
                    enabled boolean NOT NULL DEFAULT false,
                    config_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                    enabled_at timestamptz NOT NULL DEFAULT now(),
                    disabled_at timestamptz
                )
                """
            )
        setup.commit()
        setup.autocommit = True

        descriptor = _descriptor(
            extension_id=extension_id,
            remote_base_url="https://enriched-enable.example.com",
            revision="stable",
        )
        logger = _IsolatedScannerLogger(setup)
        sync_extension_scan_with_llm_lifecycle(
            logger,
            _scan_payload(
                extension_id=extension_id,
                descriptor=descriptor,
                account_id=account_id,
            ),
        )
        with setup.cursor() as cur:
            cur.execute(
                """
                SELECT bundle.id::text, version.id::text, install.install_id
                  FROM extension_bundles AS bundle
                  JOIN extension_versions AS version ON version.bundle_id = bundle.id
                  JOIN extension_installs AS install
                    ON install.extension_id = bundle.extension_key
                   AND install.account_id = %s
                 WHERE bundle.extension_key = %s
                """,
                (account_id, extension_id),
            )
            bundle_id, _version_id, install_id = cur.fetchone()
            descriptor_json = json.dumps(descriptor, sort_keys=True, separators=(",", ":"))
            cur.execute(
                """
                INSERT INTO extension_releases (
                    release_id, extension_id, release_version, descriptor_jsonb,
                    descriptor_sha256, source_kind, source_ref, account_id
                ) VALUES (%s, %s, '1.0.0', %s::jsonb, %s, 'extension_scan', %s, %s)
                """,
                (
                    enriched_release_id,
                    extension_id,
                    descriptor_json,
                    hashlib.sha256(descriptor_json.encode()).hexdigest(),
                    extension_id,
                    account_id,
                ),
            )
            cur.execute(
                """
                INSERT INTO extension_artifact_snapshots (
                    release_id, artifact_kind, artifact_id, snapshot_jsonb
                ) VALUES (%s, 'workflow', 'demo', '{"workflow_id":"demo"}'::jsonb)
                """,
                (enriched_release_id,),
            )
            cur.execute(
                """
                UPDATE extension_installs
                   SET release_id = %s, descriptor_source = 'extension_scan'
                 WHERE install_id = %s
                """,
                (enriched_release_id, install_id),
            )
            cur.execute(
                """
                UPDATE enabled_extensions
                   SET enabled = false
                 WHERE bundle_id = %s::uuid
                """,
                (bundle_id,),
            )

        with logger.extension_lifecycle_transaction() as transaction_conn:
            logger.sync_enabled_extension_record(
                transaction_conn,
                extension_id,
                enabled=True,
                preserve_existing_install=True,
            )

        with setup.cursor() as cur:
            cur.execute(
                "SELECT release_id FROM extension_installs WHERE install_id = %s",
                (install_id,),
            )
            assert cur.fetchone()[0] == enriched_release_id
            cur.execute(
                "SELECT count(*) FROM extension_artifact_snapshots WHERE release_id = %s",
                (enriched_release_id,),
            )
            assert cur.fetchone()[0] == 1
    finally:
        setup.rollback()
        setup.autocommit = True
        with setup.cursor() as cur:
            cur.execute(
                psycopg_runtime.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    psycopg_runtime.sql.Identifier(schema_name)
                )
            )
        setup.close()


@pytest.mark.integration
def test_production_repoint_waits_for_scan_before_descriptor_decisions() -> None:
    dsn = str(resolve_test_db_dsn() or "").strip()
    if not dsn:
        pytest.skip("FS_DB_DSN is required for PostgreSQL repoint ordering verification")

    account_id = DEFAULT_ACCOUNT_ID
    extension_id = f"fixture.repoint-order-{uuid.uuid4().hex}"
    install_id = f"repoint-order-{uuid.uuid4().hex}"
    old_release_id = f"release-old-{uuid.uuid4().hex}"
    old_remote_base_url = "https://old-runtime.example.com"
    new_remote_base_url = "https://new-runtime.example.com"
    schema_name = f"extension_repoint_{uuid.uuid4().hex}"
    setup = psycopg_runtime.connect(dsn)
    scanner_conn = psycopg_runtime.connect(dsn)
    service_conn = psycopg_runtime.connect(dsn)
    scanner_thread: threading.Thread | None = None
    installer_thread: threading.Thread | None = None
    scan_can_finish = threading.Event()
    scan_reached_invalidation = threading.Event()
    early_install_read = threading.Event()
    membership_lock_attempted = threading.Event()
    install_upsert_started = threading.Event()
    scanner_results: queue.Queue[tuple[str, Any]] = queue.Queue()
    installer_results: queue.Queue[tuple[str, Any]] = queue.Queue()
    provider_invalidation_phases: list[str] = []
    try:
        _create_isolated_schema(setup, schema_name)
        for conn in (scanner_conn, service_conn):
            _set_search_path(conn, schema_name)
            conn.commit()
            conn.autocommit = True

        old_descriptor = _descriptor(
            extension_id=extension_id,
            remote_base_url=old_remote_base_url,
            revision="old",
        )
        old_descriptor_json = json.dumps(old_descriptor, sort_keys=True, separators=(",", ":"))
        old_descriptor_sha = hashlib.sha256(old_descriptor_json.encode("utf-8")).hexdigest()
        with setup.cursor() as cur:
            cur.execute(
                """
                INSERT INTO extension_releases (
                    release_id, extension_id, release_version, descriptor_jsonb,
                    descriptor_sha256, execution_modes_jsonb, contract_version,
                    source_kind, account_id
                ) VALUES (
                    %s, %s, '0.9.0', %s::jsonb,
                    %s, '["remote_https"]'::jsonb, 'extension_host_v2',
                    'extension_scan', %s
                )
                """,
                (
                    old_release_id,
                    extension_id,
                    old_descriptor_json,
                    old_descriptor_sha,
                    account_id,
                ),
            )
            cur.execute(
                """
                INSERT INTO extension_installs (
                    extension_id, version, account_id, install_id, release_id,
                    descriptor_source, runtime_mode, remote_base_url
                ) VALUES (%s, '0.9.0', %s, %s, %s, 'extension_scan', 'remote_https', %s)
                """,
                (
                    extension_id,
                    account_id,
                    install_id,
                    old_release_id,
                    old_remote_base_url,
                ),
            )
        setup.commit()

        scanner_logger = _IsolatedScannerLogger(scanner_conn)
        initial_descriptor = _descriptor(
            extension_id=extension_id,
            remote_base_url=old_remote_base_url,
            revision="target-old",
        )
        sync_extension_scan_with_llm_lifecycle(
            scanner_logger,
            _scan_payload(
                extension_id=extension_id,
                descriptor=initial_descriptor,
                account_id=account_id,
            ),
        )
        with setup.cursor() as cur:
            cur.execute(
                """
                SELECT version.id::text
                  FROM extension_versions AS version
                  JOIN extension_bundles AS bundle ON bundle.id = version.bundle_id
                 WHERE bundle.extension_key = %s AND version.version = '1.0.0'
                """,
                (extension_id,),
            )
            target_release_id = str(cur.fetchone()[0])
            cur.execute(
                """
                UPDATE extension_installs
                   SET version = '0.9.0',
                       release_id = %s,
                       remote_base_url = %s,
                       config_jsonb = '{}'::jsonb
                 WHERE extension_id = %s AND account_id = %s
                """,
                (old_release_id, old_remote_base_url, extension_id, account_id),
            )
            cur.execute(
                """
                INSERT INTO llm_provider_sources (
                    account_id, extension_install_id, extension_id, provider_key
                ) VALUES (%s, %s, %s, 'fixture-provider')
                RETURNING id
                """,
                (account_id, install_id, extension_id),
            )
            provider_source_id = str(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO llm_provider_models (
                    account_id, provider_source_id, model_id, enabled,
                    verification_status, verified_capabilities_jsonb, verified_at
                ) VALUES (
                    %s, %s, 'fixture/model', true,
                    'passed', '["chat"]'::jsonb, now()
                )
                """,
                (account_id, provider_source_id),
            )
        setup.commit()

        install_store = _TrackingInstallStore(
            service_conn,
            scan_can_finish=scan_can_finish,
            early_install_read=early_install_read,
            membership_lock_attempted=membership_lock_attempted,
            install_upsert_started=install_upsert_started,
        )
        service_logger = SimpleNamespace(conn=service_conn, db_store=None, dsn="")
        service = ExtensionCommandService(
            get_logger=lambda: service_logger,
            get_install_store=lambda: install_store,
            get_runtime_store=lambda: _ReleaseReader(service_conn),
        )
        changed_descriptor = _descriptor(
            extension_id=extension_id,
            remote_base_url=new_remote_base_url,
            revision="target-new",
        )
        changed_scan = _scan_payload(
            extension_id=extension_id,
            descriptor=changed_descriptor,
            account_id=account_id,
        )
        real_scan_invalidation = llm_store.invalidate_extension_models_for_release_change
        real_install_invalidation = llm_store.mark_install_models_stale

        def pause_scan_invalidation(conn: Any, **kwargs: Any) -> int:
            scan_reached_invalidation.set()
            if not scan_can_finish.wait(timeout=8):
                raise RuntimeError("timed out waiting to release descriptor scan")
            return real_scan_invalidation(conn, **kwargs)

        def track_install_invalidation(conn: Any, **kwargs: Any) -> int:
            provider_invalidation_phases.append(
                "after_scan_release" if scan_can_finish.is_set() else "before_scan_release"
            )
            return real_install_invalidation(conn, **kwargs)

        def run_scan() -> None:
            try:
                sync_extension_scan_with_llm_lifecycle(scanner_logger, changed_scan)
                scanner_results.put(("done", None))
            except BaseException as exc:  # pragma: no cover - relayed to main thread
                scanner_results.put(("error", exc))

        def run_install() -> None:
            try:
                result = service.install_release(
                    ExtensionReleaseInstallInput(
                        release_id=target_release_id,
                        runtime_mode="remote_https",
                    ),
                    scope=ScopeContext(
                        account_id=account_id,
                        actor_id="integration-test",
                        principal_type="admin",
                    ),
                    allow_same_version_enrichment=True,
                )
                installer_results.put(("done", result))
            except BaseException as exc:  # pragma: no cover - relayed to main thread
                installer_results.put(("error", exc))

        with (
            patch.object(
                llm_store,
                "invalidate_extension_models_for_release_change",
                side_effect=pause_scan_invalidation,
            ),
            patch.object(
                llm_store,
                "mark_install_models_stale",
                side_effect=track_install_invalidation,
            ),
            patch(
                "core.application.extensions.command_install._safe_remote_http_url",
                side_effect=lambda value, **_kwargs: value,
            ),
            patch.object(service._install, "run_install_handshake", return_value=None),
        ):
            scanner_thread = threading.Thread(target=run_scan, daemon=True)
            scanner_thread.start()
            assert scan_reached_invalidation.wait(timeout=5)

            installer_thread = threading.Thread(target=run_install, daemon=True)
            installer_thread.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if membership_lock_attempted.is_set() or early_install_read.is_set():
                    break
                time.sleep(0.01)
            else:
                installer_outcome = (
                    installer_results.get_nowait() if not installer_results.empty() else None
                )
                pytest.fail(
                    "repoint did not reach membership serialization or an early read; "
                    f"installer_outcome={installer_outcome!r}"
                )

            if early_install_read.is_set():
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not provider_invalidation_phases:
                    time.sleep(0.01)
            read_before_scan_release = early_install_read.is_set()
            invalidation_before_scan_release = "before_scan_release" in provider_invalidation_phases
            scan_can_finish.set()

            scanner_thread.join(timeout=8)
            installer_thread.join(timeout=8)

        assert not scanner_thread.is_alive(), "descriptor scan deadlocked with production repoint"
        assert not installer_thread.is_alive(), "production repoint deadlocked with descriptor scan"
        scanner_kind, scanner_value = scanner_results.get(timeout=1)
        installer_kind, installer_value = installer_results.get(timeout=1)
        assert scanner_kind == "done", scanner_value
        assert installer_kind == "done", installer_value
        assert membership_lock_attempted.is_set()
        assert not read_before_scan_release
        assert not invalidation_before_scan_release
        assert install_upsert_started.is_set()
        assert provider_invalidation_phases == ["after_scan_release"]
        assert installer_value.release_id == target_release_id

        with setup.cursor() as cur:
            cur.execute(
                """
                SELECT release_id, remote_base_url
                  FROM extension_installs
                 WHERE extension_id = %s AND account_id = %s
                """,
                (extension_id, account_id),
            )
            assert cur.fetchone() == (target_release_id, new_remote_base_url)
            cur.execute(
                """
                SELECT descriptor_jsonb -> 'runtime' -> 'remote' ->> 'base_url'
                  FROM extension_releases
                 WHERE release_id = %s
                """,
                (target_release_id,),
            )
            assert cur.fetchone()[0] == new_remote_base_url
            cur.execute(
                """
                SELECT enabled, verification_status
                  FROM llm_provider_models
                 WHERE provider_source_id = %s
                """,
                (provider_source_id,),
            )
            assert cur.fetchone() == (False, "stale")
    finally:
        scan_can_finish.set()
        if scanner_thread is not None:
            scanner_thread.join(timeout=8)
        if installer_thread is not None:
            installer_thread.join(timeout=8)
        scanner_conn.close()
        service_conn.close()
        setup.rollback()
        setup.autocommit = True
        with setup.cursor() as cur:
            cur.execute(
                psycopg_runtime.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    psycopg_runtime.sql.Identifier(schema_name)
                )
            )
        setup.close()


@pytest.mark.integration
def test_failed_repoint_rolls_back_without_inverting_old_release_scan_lock() -> None:
    dsn = str(resolve_test_db_dsn() or "").strip()
    if not dsn:
        pytest.skip("FS_DB_DSN is required for PostgreSQL repoint rollback verification")

    account_id = DEFAULT_ACCOUNT_ID
    extension_id = f"fixture.repoint-rollback-{uuid.uuid4().hex}"
    target_release_id = f"release-target-{uuid.uuid4().hex}"
    old_remote_base_url = "https://stable-runtime.example.com"
    target_remote_base_url = "https://failed-runtime.example.com"
    schema_name = f"extension_repoint_rollback_{uuid.uuid4().hex}"
    failure_message = "forced handshake failure after repoint"
    setup = psycopg_runtime.connect(dsn)
    scanner_conn = psycopg_runtime.connect(dsn)
    service_conn = psycopg_runtime.connect(dsn)
    scanner_thread: threading.Thread | None = None
    installer_thread: threading.Thread | None = None
    handshake_started = threading.Event()
    allow_handshake_failure = threading.Event()
    scanner_results: queue.Queue[tuple[str, Any]] = queue.Queue()
    installer_results: queue.Queue[tuple[str, Any]] = queue.Queue()
    try:
        _create_isolated_schema(setup, schema_name)
        for conn in (scanner_conn, service_conn):
            _set_search_path(conn, schema_name)
            conn.commit()
            conn.autocommit = True

        stable_descriptor = _descriptor(
            extension_id=extension_id,
            remote_base_url=old_remote_base_url,
            revision="stable",
        )
        stable_scan = _scan_payload(
            extension_id=extension_id,
            descriptor=stable_descriptor,
            account_id=account_id,
        )
        scanner_logger = _IsolatedScannerLogger(scanner_conn)
        sync_extension_scan_with_llm_lifecycle(scanner_logger, stable_scan)

        target_descriptor = _descriptor(
            extension_id=extension_id,
            remote_base_url=target_remote_base_url,
            revision="failed-target",
        )
        target_descriptor["version"] = "2.0.0"
        target_descriptor_json = json.dumps(
            target_descriptor,
            sort_keys=True,
            separators=(",", ":"),
        )
        target_descriptor_sha = hashlib.sha256(target_descriptor_json.encode("utf-8")).hexdigest()
        with setup.cursor() as cur:
            cur.execute(
                """
                SELECT version.id::text, install.install_id
                  FROM extension_versions AS version
                  JOIN extension_bundles AS bundle ON bundle.id = version.bundle_id
                  JOIN extension_installs AS install
                    ON install.extension_id = bundle.extension_key
                   AND install.account_id = %s
                 WHERE bundle.extension_key = %s AND version.version = '1.0.0'
                """,
                (account_id, extension_id),
            )
            old_release_id, install_id = cur.fetchone()
            cur.execute(
                """
                UPDATE extension_installs
                   SET state = 'active',
                       remote_base_url = %s,
                       handshake_status = 'ok',
                       health_status = 'ok',
                       runtime_status = 'compatible',
                       config_jsonb = %s::jsonb
                 WHERE extension_id = %s AND account_id = %s
                """,
                (
                    old_remote_base_url,
                    json.dumps({"account_id": account_id, "marker": "original"}),
                    extension_id,
                    account_id,
                ),
            )
            cur.execute(
                """
                INSERT INTO extension_releases (
                    release_id, extension_id, release_version, descriptor_jsonb,
                    descriptor_sha256, execution_modes_jsonb, contract_version,
                    provenance_jsonb, source_kind, account_id
                ) VALUES (
                    %s, %s, '2.0.0', %s::jsonb,
                    %s, '["remote_https"]'::jsonb, 'extension_host_v2',
                    '{"origin":"imported"}'::jsonb, 'file', %s
                )
                """,
                (
                    target_release_id,
                    extension_id,
                    target_descriptor_json,
                    target_descriptor_sha,
                    account_id,
                ),
            )
            cur.execute(
                """
                INSERT INTO llm_provider_sources (
                    account_id, extension_install_id, extension_id, provider_key
                ) VALUES (%s, %s, %s, 'fixture-provider')
                RETURNING id
                """,
                (account_id, install_id, extension_id),
            )
            provider_source_id = str(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO llm_provider_models (
                    account_id, provider_source_id, model_id, enabled,
                    verification_status, verification_generation,
                    verified_capabilities_jsonb, verified_at
                ) VALUES (
                    %s, %s, 'fixture/model', true,
                    'passed', 'original-generation', '["chat"]'::jsonb, now()
                )
                """,
                (account_id, provider_source_id),
            )
        setup.commit()

        with scanner_conn.cursor() as cur:
            cur.execute("SELECT pg_backend_pid()")
            scanner_pid = int(cur.fetchone()[0])
        with service_conn.cursor() as cur:
            cur.execute("SELECT pg_backend_pid()")
            service_pid = int(cur.fetchone()[0])

        install_store = DBExtensionInstallStore(service_conn)
        service_logger = SimpleNamespace(conn=service_conn, db_store=None, dsn="")
        service = ExtensionCommandService(
            get_logger=lambda: service_logger,
            get_install_store=lambda: install_store,
            get_runtime_store=lambda: _ReleaseReader(service_conn),
        )

        def fail_handshake_after_scanner_waits(**_kwargs: Any) -> None:
            handshake_started.set()
            if not allow_handshake_failure.wait(timeout=8):
                raise RuntimeError("timed out waiting for old-release scan")
            raise RuntimeError(failure_message)

        def run_scan() -> None:
            try:
                sync_extension_scan_with_llm_lifecycle(scanner_logger, stable_scan)
                scanner_results.put(("done", None))
            except BaseException as exc:  # pragma: no cover - relayed to main thread
                scanner_results.put(("error", exc))

        def run_install() -> None:
            try:
                result = service.install_release(
                    ExtensionReleaseInstallInput(
                        release_id=target_release_id,
                        runtime_mode="remote_https",
                    ),
                    scope=ScopeContext(
                        account_id=account_id,
                        actor_id="integration-test",
                        principal_type="admin",
                    ),
                )
                installer_results.put(("done", result))
            except BaseException as exc:  # pragma: no cover - relayed to main thread
                installer_results.put(("error", exc))

        with (
            patch(
                "core.application.extensions.command_install._safe_remote_http_url",
                side_effect=lambda value, **_kwargs: value,
            ),
            patch.object(
                service._install,
                "run_install_handshake",
                side_effect=fail_handshake_after_scanner_waits,
            ),
        ):
            installer_thread = threading.Thread(target=run_install, daemon=True)
            installer_thread.start()
            assert handshake_started.wait(timeout=5)

            scanner_thread = threading.Thread(target=run_scan, daemon=True)
            scanner_thread.start()
            scanner_wait: tuple[str, str, list[int], bool] | None = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with setup.cursor() as cur:
                    cur.execute(
                        """
                        SELECT activity.wait_event_type,
                               activity.wait_event,
                               pg_blocking_pids(activity.pid),
                               EXISTS (
                                   SELECT 1
                                     FROM pg_locks AS held
                                    WHERE held.pid = activity.pid
                                      AND held.locktype = 'advisory'
                                      AND held.granted
                               )
                          FROM pg_stat_activity AS activity
                         WHERE activity.pid = %s
                        """,
                        (scanner_pid,),
                    )
                    scanner_wait = cur.fetchone()
                if (
                    scanner_wait is not None
                    and scanner_wait[0] == "Lock"
                    and service_pid in scanner_wait[2]
                    and scanner_wait[3] is True
                ):
                    break
                time.sleep(0.01)
            else:
                pytest.fail(
                    "old-release scan did not hold its advisory lock while waiting "
                    f"on the repoint install row: {scanner_wait!r}"
                )

            allow_handshake_failure.set()
            installer_thread.join(timeout=8)
            scanner_thread.join(timeout=8)

        assert not installer_thread.is_alive(), "failed repoint deadlocked with descriptor scan"
        assert not scanner_thread.is_alive(), "descriptor scan deadlocked with failed repoint"
        installer_kind, installer_value = installer_results.get(timeout=1)
        scanner_kind, scanner_value = scanner_results.get(timeout=1)
        assert installer_kind == "error", installer_value
        assert isinstance(installer_value, RuntimeError), installer_value
        assert str(installer_value) == failure_message
        assert scanner_kind == "done", scanner_value

        with setup.cursor() as cur:
            cur.execute(
                """
                SELECT install_id, release_id, version, state, runtime_mode,
                       remote_base_url, handshake_status, health_status,
                       runtime_status, config_jsonb
                  FROM extension_installs
                 WHERE extension_id = %s AND account_id = %s
                """,
                (extension_id, account_id),
            )
            assert cur.fetchone() == (
                install_id,
                old_release_id,
                "1.0.0",
                "active",
                "remote_https",
                old_remote_base_url,
                "ok",
                "ok",
                "compatible",
                {"account_id": account_id, "marker": "original"},
            )
            cur.execute(
                """
                SELECT enabled, verification_status, verification_generation,
                       verified_capabilities_jsonb,
                       verification_error_safe,
                       verified_at IS NOT NULL
                  FROM llm_provider_models
                 WHERE provider_source_id = %s
                """,
                (provider_source_id,),
            )
            assert cur.fetchone() == (
                True,
                "passed",
                "original-generation",
                ["chat"],
                None,
                True,
            )
    finally:
        allow_handshake_failure.set()
        if installer_thread is not None:
            installer_thread.join(timeout=8)
        if scanner_thread is not None:
            scanner_thread.join(timeout=8)
        scanner_conn.close()
        service_conn.close()
        setup.rollback()
        setup.autocommit = True
        with setup.cursor() as cur:
            cur.execute(
                psycopg_runtime.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    psycopg_runtime.sql.Identifier(schema_name)
                )
            )
        setup.close()


@pytest.mark.integration
def test_repoint_away_does_not_race_late_cross_account_release_join() -> None:
    dsn = str(resolve_test_db_dsn() or "").strip()
    if not dsn:
        pytest.skip("FS_DB_DSN is required for PostgreSQL late-join verification")

    account_a = DEFAULT_ACCOUNT_ID
    account_b = str(uuid.uuid4())
    extension_id = f"fixture.repoint-late-join-{uuid.uuid4().hex}"
    old_release_id = f"release-old-{uuid.uuid4().hex}"
    new_release_id = f"release-new-{uuid.uuid4().hex}"
    install_a_id = f"late-join-a-{uuid.uuid4().hex}"
    install_b_id = f"late-join-b-{uuid.uuid4().hex}"
    schema_name = f"extension_repoint_late_join_{uuid.uuid4().hex}"
    setup = psycopg_runtime.connect(dsn)
    service_conn = psycopg_runtime.connect(dsn)
    join_conn = psycopg_runtime.connect(dsn)
    installer_thread: threading.Thread | None = None
    handshake_started = threading.Event()
    allow_handshake = threading.Event()
    installer_results: queue.Queue[tuple[str, Any]] = queue.Queue()
    try:
        _create_isolated_schema(setup, schema_name)
        for conn in (service_conn, join_conn):
            _set_search_path(conn, schema_name)
            conn.commit()
        service_conn.autocommit = True
        join_conn.autocommit = False

        old_descriptor = _descriptor(
            extension_id=extension_id,
            remote_base_url="https://old-late-join.example.com",
            revision="old",
        )
        new_descriptor = _descriptor(
            extension_id=extension_id,
            remote_base_url="https://new-late-join.example.com",
            revision="new",
        )
        new_descriptor["version"] = "2.0.0"
        with setup.cursor() as cur:
            for release_id, version, descriptor in (
                (old_release_id, "1.0.0", old_descriptor),
                (new_release_id, "2.0.0", new_descriptor),
            ):
                descriptor_json = json.dumps(
                    descriptor,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                cur.execute(
                    """
                    INSERT INTO extension_releases (
                        release_id, extension_id, release_version,
                        descriptor_jsonb, descriptor_sha256,
                        execution_modes_jsonb, contract_version,
                        source_kind, account_id
                    ) VALUES (
                        %s, %s, %s,
                        %s::jsonb, %s,
                        '["remote_https"]'::jsonb, 'extension_host_v2',
                        'file', NULL
                    )
                    """,
                    (
                        release_id,
                        extension_id,
                        version,
                        descriptor_json,
                        hashlib.sha256(descriptor_json.encode("utf-8")).hexdigest(),
                    ),
                )
            cur.execute(
                """
                INSERT INTO extension_installs (
                    extension_id, version, account_id, install_id, release_id,
                    descriptor_source, runtime_mode, remote_base_url
                ) VALUES (
                    %s, '1.0.0', %s, %s, %s,
                    'file', 'remote_https', 'https://old-late-join.example.com'
                )
                """,
                (extension_id, account_a, install_a_id, old_release_id),
            )
        setup.commit()

        service_logger = SimpleNamespace(conn=service_conn, db_store=None, dsn="")
        service = ExtensionCommandService(
            get_logger=lambda: service_logger,
            get_install_store=lambda: DBExtensionInstallStore(service_conn),
            get_runtime_store=lambda: _ReleaseReaderWithDelete(service_conn),
        )

        def pause_after_repoint(**_kwargs: Any) -> None:
            handshake_started.set()
            if not allow_handshake.wait(timeout=8):
                raise RuntimeError("timed out waiting for late release join")

        def run_install() -> None:
            try:
                result = service.install_release(
                    ExtensionReleaseInstallInput(
                        release_id=new_release_id,
                        runtime_mode="remote_https",
                    ),
                    scope=ScopeContext(
                        account_id=account_a,
                        actor_id="integration-test",
                        principal_type="admin",
                    ),
                )
                installer_results.put(("done", result))
            except BaseException as exc:  # pragma: no cover - relayed to main thread
                installer_results.put(("error", exc))

        with (
            patch(
                "core.application.extensions.command_install._safe_remote_http_url",
                side_effect=lambda value, **_kwargs: value,
            ),
            patch.object(
                service._install,
                "run_install_handshake",
                side_effect=pause_after_repoint,
            ),
        ):
            installer_thread = threading.Thread(target=run_install, daemon=True)
            installer_thread.start()
            assert handshake_started.wait(timeout=5)

            joining_store = DBExtensionInstallStore(join_conn)
            joining_store.lock_release_membership(old_release_id)
            joining_store.install(
                extension_id=extension_id,
                version="1.0.0",
                account_id=account_b,
                install_id=install_b_id,
                release_id=old_release_id,
                descriptor_source="file",
                runtime_mode="remote_https",
                remote_base_url="https://old-late-join.example.com",
            )

            allow_handshake.set()
            installer_thread.join(timeout=3)
            repoint_finished_before_join_commit = not installer_thread.is_alive()
            join_conn.commit()
            installer_thread.join(timeout=8)

        assert repoint_finished_before_join_commit, (
            "repoint waited on the late joiner's old-release reference; "
            "old-release GC must be deferred"
        )
        assert not installer_thread.is_alive()
        installer_kind, installer_value = installer_results.get(timeout=1)
        assert installer_kind == "done", installer_value
        assert installer_value.release_id == new_release_id

        with setup.cursor() as cur:
            cur.execute(
                """
                SELECT account_id::text, install_id, release_id
                  FROM extension_installs
                 WHERE extension_id = %s
                 ORDER BY account_id::text
                """,
                (extension_id,),
            )
            installs = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
            assert installs == {
                account_a: (install_a_id, new_release_id),
                account_b: (install_b_id, old_release_id),
            }
            cur.execute(
                "SELECT COUNT(*) FROM extension_releases WHERE release_id = %s",
                (old_release_id,),
            )
            assert cur.fetchone()[0] == 1
    finally:
        allow_handshake.set()
        with contextlib.suppress(Exception):
            join_conn.rollback()
        if installer_thread is not None:
            installer_thread.join(timeout=8)
        service_conn.close()
        join_conn.close()
        setup.rollback()
        setup.autocommit = True
        with setup.cursor() as cur:
            cur.execute(
                psycopg_runtime.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    psycopg_runtime.sql.Identifier(schema_name)
                )
            )
        setup.close()


@pytest.mark.integration
def test_managed_update_inner_install_sees_uncommitted_candidate_and_commits_atomically() -> None:
    from core.infrastructure.db.db_pool import PooledConnectionProxy, transaction_connection

    dsn = str(resolve_test_db_dsn() or "").strip()
    if not dsn:
        pytest.skip("FS_DB_DSN is required for PostgreSQL managed-update verification")

    account_id = DEFAULT_ACCOUNT_ID
    extension_id = f"fixture.managed-update-{uuid.uuid4().hex}"
    release_id = f"release-managed-update-{uuid.uuid4().hex}"
    schema_name = f"extension_managed_update_{uuid.uuid4().hex}"
    setup = psycopg_runtime.connect(dsn)
    transaction_conn = psycopg_runtime.connect(dsn)
    observer_conn = psycopg_runtime.connect(dsn)

    class _SingleConnectionPool:
        def __init__(self, conn: Any) -> None:
            self.conn = conn
            self.borrowed = False
            self.get_calls = 0
            self.put_calls = 0

        def getconn(self):
            self.get_calls += 1
            if self.borrowed:
                raise AssertionError("nested lifecycle borrowed a second PostgreSQL session")
            self.borrowed = True
            return self.conn

        def putconn(self, conn: Any, close: bool = False) -> None:
            assert conn is self.conn
            assert close is False
            self.put_calls += 1
            self.borrowed = False

    try:
        _create_isolated_schema(setup, schema_name)
        for conn in (transaction_conn, observer_conn):
            _set_search_path(conn, schema_name)
            conn.commit()
            conn.autocommit = True

        pool = _SingleConnectionPool(transaction_conn)
        proxy = PooledConnectionProxy(pool)
        logger = SimpleNamespace(conn=proxy, db_store=None, dsn="")
        service = ExtensionCommandService(
            get_logger=lambda: logger,
            get_install_store=lambda: DBExtensionInstallStore(proxy),
            get_runtime_store=lambda: _ReleaseReader(proxy),
        )
        descriptor = {
            "extension_id": extension_id,
            "version": "2.0.0",
            "distribution": {"execution_modes": ["local"]},
        }
        descriptor_json = json.dumps(descriptor, sort_keys=True, separators=(",", ":"))

        with (
            patch.object(service._install, "run_install_handshake", return_value=None),
            transaction_connection(proxy) as outer_conn,
        ):
            with outer_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO extension_releases (
                        release_id, extension_id, release_version,
                        descriptor_jsonb, descriptor_sha256,
                        execution_modes_jsonb, source_kind, account_id
                    ) VALUES (
                        %s, %s, '2.0.0', %s::jsonb, %s,
                        '[\"local\"]'::jsonb, 'extension_scan', %s
                    )
                    """,
                    (
                        release_id,
                        extension_id,
                        descriptor_json,
                        hashlib.sha256(descriptor_json.encode("utf-8")).hexdigest(),
                        account_id,
                    ),
                )

            installed = service.install_release(
                ExtensionReleaseInstallInput(release_id=release_id, runtime_mode="local"),
                scope=ScopeContext(
                    account_id=account_id,
                    actor_id="integration-test",
                    principal_type="admin",
                ),
            )
            assert installed.release_id == release_id

            with observer_conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM extension_releases WHERE release_id = %s",
                    (release_id,),
                )
                assert cur.fetchone()[0] == 0
                cur.execute(
                    "SELECT COUNT(*) FROM extension_installs WHERE release_id = %s",
                    (release_id,),
                )
                assert cur.fetchone()[0] == 0

        assert pool.get_calls == 1
        assert pool.put_calls == 1
        with observer_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM extension_releases WHERE release_id = %s",
                (release_id,),
            )
            assert cur.fetchone()[0] == 1
            cur.execute(
                "SELECT account_id::text, release_id FROM extension_installs WHERE release_id = %s",
                (release_id,),
            )
            assert cur.fetchone() == (account_id, release_id)
    finally:
        transaction_conn.close()
        observer_conn.close()
        setup.rollback()
        setup.autocommit = True
        with setup.cursor() as cur:
            cur.execute(
                psycopg_runtime.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    psycopg_runtime.sql.Identifier(schema_name)
                )
            )
        setup.close()


@pytest.mark.integration
def test_direct_nested_failure_rolls_back_owner_before_deferred_compensation() -> None:
    from core.infrastructure.db.db_pool import (
        active_transaction_connection,
        defer_transaction_rollback,
        transaction_connection,
    )

    dsn = str(resolve_test_db_dsn() or "").strip()
    if not dsn:
        pytest.skip("FS_DB_DSN is required for PostgreSQL nested-transaction verification")

    extension_id = f"fixture.direct-nested-{uuid.uuid4().hex}"
    release_id = f"release-direct-nested-{uuid.uuid4().hex}"
    schema_name = f"extension_direct_nested_{uuid.uuid4().hex}"
    setup = psycopg_runtime.connect(dsn)
    conn = psycopg_runtime.connect(dsn)
    compensation_events: list[str] = []
    try:
        _create_isolated_schema(setup, schema_name)
        _set_search_path(conn, schema_name)
        conn.commit()
        conn.autocommit = True

        def compensate() -> None:
            assert active_transaction_connection(conn) is None
            assert conn.get_transaction_status() == 0
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM extension_releases WHERE release_id = %s",
                    (release_id,),
                )
                assert cur.fetchone()[0] == 0
            compensation_events.append("after_owner_rollback")

        def _raises_operation_1474():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO extension_releases (
                        release_id, extension_id, descriptor_sha256
                    ) VALUES (%s, %s, %s)
                    """,
                    (release_id, extension_id, "f" * 64),
                )
            try:
                with transaction_connection(conn):
                    raise RuntimeError("nested lifecycle failed")
            except RuntimeError:
                assert defer_transaction_rollback(conn, compensate)
                raise

        with (
            pytest.raises(RuntimeError, match="nested lifecycle failed"),
            transaction_connection(conn),
        ):
            _raises_operation_1474()

        assert compensation_events == ["after_owner_rollback"]
    finally:
        conn.close()
        setup.rollback()
        setup.autocommit = True
        with setup.cursor() as cur:
            cur.execute(
                psycopg_runtime.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    psycopg_runtime.sql.Identifier(schema_name)
                )
            )
        setup.close()


@pytest.mark.integration
def test_oauth_release_install_waits_for_disconnect_then_rolls_back_atomically() -> None:
    reason = _disposable_db.unavailable_reason()
    if reason:
        pytest.skip(f"disposable PostgreSQL unavailable: {reason}")
    database = _disposable_db.start_disposable_db()
    setup = psycopg_runtime.connect(database.dsn)
    blocker = psycopg_runtime.connect(database.dsn)
    upgrader = psycopg_runtime.connect(database.dsn)
    observer = psycopg_runtime.connect(database.dsn)
    try:
        tenant_id = uuid.uuid4()
        account_id = uuid.UUID(DEFAULT_ACCOUNT_ID)
        workspace_id = uuid.uuid4()
        project_id = uuid.uuid4()
        extension_id = f"fixture.oauth-install-race-{uuid.uuid4().hex}"
        install_id = f"install-oauth-race-{uuid.uuid4().hex}"
        old_release_id = f"release-oauth-race-old-{uuid.uuid4().hex}"
        new_release_id = f"release-oauth-race-new-{uuid.uuid4().hex}"
        configuration_id = uuid.uuid4()
        connection_id = f"connection-oauth-race-{uuid.uuid4().hex}"
        manifest_path = (
            Path(__file__).resolve().parents[1]
            / "fixtures/extensions/fixture_extension_oauth/extension.yaml"
        )
        old_descriptor = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        old_descriptor["extension_id"] = extension_id
        old_descriptor["version"] = "1.0.0"
        old_descriptor["distribution"] = {"execution_modes": ["remote_https"]}
        old_descriptor["entrypoint"] = {"mode": "remote_https"}
        old_descriptor["runtime"]["remote"] = {
            "base_url": "https://oauth-race.example.test",
            "endpoints": {"handshake": "/handshake", "health": "/health"},
        }
        old_descriptor["action_definitions"] = [
            {"action_id": "verify_identity", "handler": {"mode": "extension"}}
        ]
        new_descriptor = deepcopy(old_descriptor)
        new_descriptor["version"] = "2.0.0"
        new_descriptor["description"] = "Cosmetic release transition under disconnect race."
        provider = compile_oauth_providers(old_descriptor)[0]
        security_fingerprint = provider_security_fingerprint(provider)
        revocation_fingerprint = provider_revocation_fingerprint(provider)
        normalized_oauth_contract = {
            "oauth_providers": [
                {
                    "provider_id": provider.provider_id,
                    "security_fingerprint": security_fingerprint,
                    "revocation_fingerprint": revocation_fingerprint,
                }
            ],
            "connection_types": deepcopy(
                old_descriptor["runtime"]["extension_contract_v2"]["connection_types"]
            ),
        }
        old_descriptor["runtime"]["extension_contract_v2_normalized"] = deepcopy(
            normalized_oauth_contract
        )
        new_descriptor["runtime"]["extension_contract_v2_normalized"] = deepcopy(
            normalized_oauth_contract
        )
        configuration_ref = configuration_secret_ref_key(str(configuration_id), "client_secret")
        token_ref = connection_secret_ref_key(connection_id, "_oauth_refresh_token")

        with setup.cursor() as cur:
            cur.execute(
                "INSERT INTO accounts (id, tenant_id, account_key, display_name) VALUES (%s, %s, %s, %s)",
                (account_id, tenant_id, f"oauth-race-{account_id.hex}", "OAuth race"),
            )
            cur.execute(
                """INSERT INTO workspaces
                       (id, tenant_id, account_id, workspace_key, display_name)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    workspace_id,
                    tenant_id,
                    account_id,
                    f"oauth-race-{workspace_id.hex}",
                    "OAuth race",
                ),
            )
            cur.execute(
                """INSERT INTO projects (id, tenant_id, account_id, workspace_id, name)
                   VALUES (%s, %s, %s, %s, %s)""",
                (project_id, tenant_id, account_id, workspace_id, "OAuth race"),
            )
            for release_id, version, descriptor in (
                (old_release_id, "1.0.0", old_descriptor),
                (new_release_id, "2.0.0", new_descriptor),
            ):
                descriptor_json = json.dumps(descriptor, sort_keys=True, separators=(",", ":"))
                cur.execute(
                    """INSERT INTO extension_releases (
                           release_id, extension_id, release_version,
                           descriptor_jsonb, descriptor_sha256,
                           execution_modes_jsonb, contract_version, source_kind
                       ) VALUES (%s, %s, %s, %s::jsonb, %s,
                                 '["remote_https"]'::jsonb, 'extension_host_v2', 'file')""",
                    (
                        release_id,
                        extension_id,
                        version,
                        descriptor_json,
                        hashlib.sha256(descriptor_json.encode()).hexdigest(),
                    ),
                )
            cur.execute(
                """INSERT INTO extension_installs (
                       extension_id, version, account_id, install_id, release_id,
                       descriptor_source, runtime_mode, remote_base_url
                   ) VALUES (%s, '1.0.0', %s, %s, %s,
                             'file', 'remote_https', 'https://oauth-race.example.test')""",
                (extension_id, account_id, install_id, old_release_id),
            )
        oauth = ExtensionOAuthRepository(setup)
        configuration = oauth.create_configuration(
            configuration_id=configuration_id,
            tenant_id=tenant_id,
            account_id=account_id,
            project_id=project_id,
            extension_install_id=install_id,
            extension_id=extension_id,
            extension_release_id=old_release_id,
            provider_id=provider.provider_id,
            display_name="Primary",
            configuration_revision=1,
            authorization_revision=1,
            status="verified",
            non_secret_config={"client_id": "client-a"},
            secret_refs={"refs": {"client_secret": configuration_ref}},
            provider_security_fingerprint=security_fingerprint,
            provider_revocation_fingerprint=revocation_fingerprint,
            last_error={},
        )
        assert configuration is not None
        with setup.cursor() as cur:
            cur.execute(
                """INSERT INTO project_connections (
                       connection_id, project_id, extension_id, account_id,
                       extension_install_id, connection_type, oauth_configuration_id,
                       oauth_provider_id, oauth_extension_release_id,
                       oauth_authorization_revision, oauth_requested_scopes,
                       oauth_granted_scopes, oauth_provider_security_fingerprint,
                       oauth_provider_revocation_fingerprint, credential_revision,
                       enabled, connection_config, secret_refs
                   ) VALUES (
                       %s, %s, %s, %s, %s, 'example_mail', %s, %s, %s, 1,
                       '["mail.read"]'::jsonb, '["mail.read"]'::jsonb,
                       %s, %s, 1, true,
                       '{"oauth":{"lifecycle_status":"connected"}}'::jsonb,
                       %s::jsonb
                   )""",
                (
                    connection_id,
                    project_id,
                    extension_id,
                    account_id,
                    install_id,
                    configuration_id,
                    provider.provider_id,
                    old_release_id,
                    security_fingerprint,
                    revocation_fingerprint,
                    json.dumps({"refs": {"_oauth_refresh_token": token_ref}}),
                ),
            )
        setup.commit()

        with upgrader.cursor() as cur:
            cur.execute("SET statement_timeout = '10s'")
            cur.execute("SET lock_timeout = '8s'")
            cur.execute("SELECT pg_backend_pid()")
            upgrader_pid = int(cur.fetchone()[0])
        upgrader.commit()
        outcome: queue.Queue[tuple[str, Any]] = queue.Queue()
        started = threading.Event()

        def run_upgrade() -> None:
            logger = SimpleNamespace(conn=upgrader, db_store=None, dsn="")
            service = ExtensionCommandService(
                get_logger=lambda: logger,
                get_install_store=lambda: DBExtensionInstallStore(upgrader),
                get_runtime_store=lambda: _ReleaseReader(upgrader),
            )
            started.set()
            try:
                with patch(
                    "core.application.extensions.command_install._safe_remote_http_url",
                    side_effect=lambda value, **_kwargs: value,
                ):
                    service.install_release(
                        ExtensionReleaseInstallInput(
                            release_id=new_release_id,
                            runtime_mode="remote_https",
                        ),
                        scope=ScopeContext(
                            account_id=str(account_id),
                            actor_id="integration-test",
                            principal_type="admin",
                        ),
                    )
            except BaseException as exc:  # pragma: no cover - relayed to owner thread
                outcome.put(("error", exc))
            else:
                outcome.put(("done", None))

        exact_scope = {
            "tenant_id": tenant_id,
            "account_id": account_id,
            "project_id": project_id,
            "extension_install_id": install_id,
            "extension_id": extension_id,
            "extension_release_id": old_release_id,
            "provider_id": provider.provider_id,
            "configuration_id": configuration_id,
            "connection_id": connection_id,
        }
        with blocker.transaction():
            assert ExtensionOAuthRepository(blocker).block_connection_disconnect(
                **exact_scope,
                expected_credential_revision=1,
                disconnect_operation_id="B" * 43,
                lease_seconds=300,
            )
            with blocker.cursor() as cur:
                cur.execute(
                    """SELECT credential_revision, enabled, connection_config,
                              secret_refs, oauth_granted_scopes,
                              oauth_extension_release_id,
                              oauth_provider_security_fingerprint,
                              oauth_provider_revocation_fingerprint
                         FROM project_connections WHERE connection_id = %s""",
                    (connection_id,),
                )
                blocked_connection = deepcopy(cur.fetchone())
            upgrade_thread = threading.Thread(target=run_upgrade, daemon=True)
            upgrade_thread.start()
            assert started.wait(timeout=2)
            deadline = time.monotonic() + 5
            waiting_on_lock = False
            while time.monotonic() < deadline and not waiting_on_lock:
                with observer.cursor() as cur:
                    cur.execute(
                        """SELECT wait_event_type
                             FROM pg_stat_activity
                            WHERE pid = %s""",
                        (upgrader_pid,),
                    )
                    row = cur.fetchone()
                waiting_on_lock = bool(row and row[0] == "Lock")
                if not waiting_on_lock:
                    started.wait(timeout=0.02)
            early_outcome = outcome.get_nowait() if not outcome.empty() else None
            assert waiting_on_lock, (
                "production install did not wait on the disconnect row lock; "
                f"early outcome={early_outcome!r}"
            )

        upgrade_thread.join(timeout=8)
        assert not upgrade_thread.is_alive()
        outcome_kind, outcome_value = outcome.get(timeout=1)
        assert outcome_kind == "error"
        assert isinstance(outcome_value, ServicePreconditionError)
        assert outcome_value.payload == {
            "error": "oauth_release_disconnect_in_progress",
            "connection_ids": [connection_id],
        }
        with observer.cursor() as cur:
            cur.execute(
                "SELECT release_id FROM extension_installs WHERE install_id = %s",
                (install_id,),
            )
            assert cur.fetchone()[0] == old_release_id
            cur.execute(
                """SELECT extension_release_id, provider_security_fingerprint,
                          provider_revocation_fingerprint
                     FROM extension_oauth_configurations WHERE configuration_id = %s""",
                (configuration_id,),
            )
            assert cur.fetchone() == (
                old_release_id,
                security_fingerprint,
                revocation_fingerprint,
            )
            cur.execute(
                """SELECT credential_revision, enabled, connection_config,
                          secret_refs, oauth_granted_scopes,
                          oauth_extension_release_id,
                          oauth_provider_security_fingerprint,
                          oauth_provider_revocation_fingerprint
                     FROM project_connections WHERE connection_id = %s""",
                (connection_id,),
            )
            assert cur.fetchone() == blocked_connection
    finally:
        for connection in (observer, upgrader, blocker, setup):
            connection.close()
        _disposable_db.stop_disposable_db(database.container)
