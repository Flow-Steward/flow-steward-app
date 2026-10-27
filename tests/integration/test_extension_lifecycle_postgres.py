from __future__ import annotations

import json
import queue
import shutil
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions.command_service import ExtensionCommandService
from core.application.extensions.data_migrations import (
    apply_extension_data_migrations,
    snapshot_extension_data,
)
from core.application.extensions.managed_release_service import ManagedReleaseService
from core.application.extensions.projected_dataset_bridge import projection_bridge_id
from core.application.management.command_services import ExtensionBindingService
from core.application.management.errors import ServiceConflictError
from core.application.management.scope import ScopeContext
from core.application.settings import llm_provider_extension_repository as llm_store
from core.infrastructure.db import psycopg_runtime
from core.infrastructure.db.platform_stores import PlatformIdentityStore, PlatformIntegrationStore
from tests._db_env import resolve_test_db_dsn
from tests.support.platform_test_store import PlatformTestStore as PlatformLogger
from tests.unit.test_extension_oauth_contract import _descriptor as _oauth_descriptor


def _service(logger: PlatformLogger) -> ExtensionBindingService:
    return ExtensionBindingService(
        get_logger=lambda: logger,
        get_operation_store=lambda: logger,
        resolve_extension_runtime_record=lambda _extension_id: (None, None),
        extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 502,
    )


def _seed_lifecycle(pg_conn, tmp_path: Path, suffix: str) -> dict[str, str]:
    extension_id = f"fixture.lifecycle-{suffix}"
    install_id = f"pi_{suffix}"
    release_id = f"pr_{suffix}"
    connection_id = f"conn_{suffix}"
    effect_id = str(uuid.uuid4())
    project_id = str(uuid.uuid4())
    bundle_root = tmp_path / extension_id.replace(".", "_")
    bundle_root.mkdir()
    (bundle_root / "extension.yaml").write_text(
        f"manifest_version: 2\nextension_id: {extension_id}\n",
        encoding="utf-8",
    )
    workflow_id = projection_bridge_id(install_id, "workflow")
    with pg_conn.cursor() as cur:
        cur.execute(
            """INSERT INTO provider_types (key, label, description)
               VALUES ('tool_provider', 'Tool Provider', 'Integration test provider type')
               ON CONFLICT (key) DO NOTHING"""
        )
        cur.execute(
            "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s)",
            (project_id, f"Lifecycle {suffix}", DEFAULT_ACCOUNT_ID),
        )
        cur.execute(
            "INSERT INTO extension_bundles (extension_key, kind, display_name, bundle_root, manifest_path, latest_version, status) VALUES (%s, 'tool_provider', %s, %s, %s, '1.0.0', 'valid') RETURNING id",
            (extension_id, extension_id, str(bundle_root), str(bundle_root / "extension.yaml")),
        )
        bundle_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO extension_versions (bundle_id, version, manifest_jsonb, compiled_manifest_jsonb) VALUES (%s, '1.0.0', %s::jsonb, %s::jsonb) RETURNING id",
            (
                bundle_id,
                '{"manifest_version":2}',
                '{"manifest_version":2,"lifecycle":{"data_schema_version":1,"data_migrations":[]}}',
            ),
        )
        version_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO enabled_extensions (bundle_id, version_id) VALUES (%s, %s)",
            (bundle_id, version_id),
        )
        cur.execute(
            "INSERT INTO extension_releases (release_id, extension_id, release_version, account_id, source_kind, descriptor_jsonb, descriptor_sha256) VALUES (%s, %s, '1.0.0', %s, 'extension_scan', %s::jsonb, %s)",
            (
                release_id,
                extension_id,
                DEFAULT_ACCOUNT_ID,
                '{"extension_id":"' + extension_id + '"}',
                "a" * 64,
            ),
        )
        cur.execute(
            "INSERT INTO extension_installs (extension_id, account_id, install_id, release_id, descriptor_source) VALUES (%s, %s, %s, %s, 'extension_scan')",
            (extension_id, DEFAULT_ACCOUNT_ID, install_id, release_id),
        )
        cur.execute(
            "INSERT INTO extension_entitlements (entitlement_id, install_id, account_id) VALUES (%s, %s, %s)",
            (f"ent_{suffix}", install_id, DEFAULT_ACCOUNT_ID),
        )
        cur.execute(
            "INSERT INTO extension_registry_projections (account_id, install_id, release_id, extension_id, artifact_kind, artifact_id) VALUES (%s, %s, %s, %s, 'workflow', 'workflow')",
            (DEFAULT_ACCOUNT_ID, install_id, release_id, extension_id),
        )
        cur.execute(
            "INSERT INTO registry_workflows (workflow_key, display_name, account_id, scope_kind, source_install_id, source_release_id, projection_mode) VALUES (%s, %s, %s, 'account', %s, %s, 'derived')",
            (workflow_id, workflow_id, DEFAULT_ACCOUNT_ID, install_id, release_id),
        )
        cur.execute(
            "INSERT INTO project_workflows (project_id, workflow_id) VALUES (%s, %s)",
            (project_id, workflow_id),
        )
        cur.execute(
            "INSERT INTO project_connections (connection_id, project_id, account_id, extension_id, connection_kind, extension_install_id, secret_refs) VALUES (%s, %s, %s, %s, 'extension_runtime', %s, %s::jsonb)",
            (
                connection_id,
                project_id,
                DEFAULT_ACCOUNT_ID,
                extension_id,
                install_id,
                '{"refs":{"api_key":"owned"}}',
            ),
        )
        cur.execute(
            "INSERT INTO secret_refs (account_id, ref_key, encrypted_value) VALUES (%s, %s, 'encrypted'), (%s, %s, 'encrypted')",
            (
                DEFAULT_ACCOUNT_ID,
                f"project_connection:{connection_id}:api_key",
                DEFAULT_ACCOUNT_ID,
                f"llmext_cred_{install_id}",
            ),
        )
        cur.execute(
            "INSERT INTO extension_project_state (account_id, project_id, extension_id, extension_install_id, connection_id, namespace, state_key, value_jsonb) VALUES (%s, %s, %s, %s, %s, 'sync', 'cursor', '{\"cursor\":1}'::jsonb)",
            (DEFAULT_ACCOUNT_ID, project_id, extension_id, install_id, connection_id),
        )
        for status in ("succeeded", "claimed", "running"):
            cur.execute(
                "INSERT INTO external_effects (effect_id, account_id, project_id, extension_id, extension_install_id, connection_id, operation_id, effect_kind, channel, source_type, idempotency_key, idempotency_scope_hash, request_fingerprint, status, source_ref_jsonb, artifact_refs_jsonb, response_summary_jsonb, error_text) VALUES (%s, %s, %s, %s, %s, %s, %s, 'write', 'api', 'action', %s, %s, 'fingerprint', %s, '{\"raw\":true}'::jsonb, '[\"artifact\"]'::jsonb, '{\"raw\":true}'::jsonb, 'sensitive')",
                (
                    effect_id if status == "claimed" else str(uuid.uuid4()),
                    DEFAULT_ACCOUNT_ID,
                    project_id,
                    extension_id,
                    install_id,
                    connection_id,
                    f"op_{status}",
                    f"key_{suffix}_{status}",
                    f"scope_{suffix}_{status}",
                    status,
                ),
            )
    return {
        "extension_id": extension_id,
        "install_id": install_id,
        "release_id": release_id,
        "descriptor_sha256": "a" * 64,
        "connection_id": connection_id,
        "effect_id": effect_id,
        "project_id": project_id,
        "workflow_id": workflow_id,
        "bundle_root": str(bundle_root),
    }


def _cleanup_lifecycle_fixtures(pg_conn) -> None:
    pg_conn.rollback()
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM project_workflows pw
             USING projects p
             WHERE pw.project_id = p.id
               AND p.name LIKE 'Lifecycle %%'
            """
        )
        cur.execute(
            """
            DELETE FROM registry_workflows
             WHERE workflow_id LIKE 'extension.pi\\_%%' ESCAPE '\\'
               AND display_name = workflow_id
               AND description = ''
            """
        )
        cur.execute("DELETE FROM external_effects WHERE extension_id LIKE 'fixture.lifecycle-%%'")
        cur.execute(
            "DELETE FROM command_audit_log WHERE command_type = 'extension_oauth_uninstall_lifecycle' AND target_jsonb->>'extension_id' LIKE 'fixture.lifecycle-%%'"
        )
        cur.execute(
            "DELETE FROM secret_refs sr USING project_connections pc WHERE pc.extension_id LIKE 'fixture.lifecycle-%%' AND sr.account_id = pc.account_id AND sr.ref_key LIKE 'project_connection:' || pc.connection_id || ':%%'"
        )
        cur.execute(
            "DELETE FROM extension_project_state WHERE extension_id LIKE 'fixture.lifecycle-%%'"
        )
        cur.execute(
            "DELETE FROM project_connections WHERE extension_id LIKE 'fixture.lifecycle-%%'"
        )
        cur.execute(
            "DELETE FROM secret_refs WHERE ref_key IN (SELECT 'llmext_cred_' || install_id FROM extension_installs WHERE extension_id LIKE 'fixture.lifecycle-%%')"
        )
        cur.execute(
            "DELETE FROM extension_usage_events WHERE install_id IN (SELECT install_id FROM extension_installs WHERE extension_id LIKE 'fixture.lifecycle-%%')"
        )
        cur.execute(
            "DELETE FROM extension_entitlements WHERE install_id IN (SELECT install_id FROM extension_installs WHERE extension_id LIKE 'fixture.lifecycle-%%')"
        )
        cur.execute(
            "DELETE FROM extension_registry_projections WHERE extension_id LIKE 'fixture.lifecycle-%%'"
        )
        cur.execute("DELETE FROM extension_installs WHERE extension_id LIKE 'fixture.lifecycle-%%'")
        cur.execute("DELETE FROM extension_releases WHERE extension_id LIKE 'fixture.lifecycle-%%'")
        cur.execute(
            "DELETE FROM enabled_extensions WHERE bundle_id IN (SELECT id FROM extension_bundles WHERE extension_key LIKE 'fixture.lifecycle-%%')"
        )
        cur.execute(
            "DELETE FROM extension_versions WHERE bundle_id IN (SELECT id FROM extension_bundles WHERE extension_key LIKE 'fixture.lifecycle-%%')"
        )
        cur.execute("DELETE FROM extension_bundles WHERE extension_key LIKE 'fixture.lifecycle-%%'")
        cur.execute("DELETE FROM projects WHERE name LIKE 'Lifecycle %%'")
        cur.execute("DELETE FROM accounts WHERE account_key LIKE 'lifecycle-consumer-%%'")
    pg_conn.commit()


@pytest.mark.integration
def test_extension_command_toggle_keeps_install_and_runtime_projection_aligned(
    pg_conn, tmp_path
) -> None:
    seeded = _seed_lifecycle(pg_conn, tmp_path, uuid.uuid4().hex[:8])
    pg_conn.commit()
    logger = PlatformLogger(resolve_test_db_dsn())
    service = ExtensionCommandService(get_logger=lambda: logger)
    scope = ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID,
        actor_id="admin",
        principal_type="admin",
    )

    def _persisted_state() -> tuple[str, bool]:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                SELECT install.state, enabled.enabled
                  FROM extension_installs AS install
                  JOIN extension_bundles AS bundle
                    ON bundle.extension_key = install.extension_id
                  JOIN enabled_extensions AS enabled
                    ON enabled.bundle_id = bundle.id
                 WHERE install.extension_id = %s
                   AND install.account_id = %s
                """,
                (seeded["extension_id"], DEFAULT_ACCOUNT_ID),
            )
            row = cur.fetchone()
        assert row is not None
        return str(row[0]), bool(row[1])

    try:
        assert _persisted_state() == ("active", True)

        disabled = service.disable_extension(seeded["extension_id"], scope=scope)
        assert disabled.state == "disabled"
        assert _persisted_state() == ("disabled", False)

        enabled = service.enable_extension(seeded["extension_id"], scope=scope)
        assert enabled.state == "active"
        assert _persisted_state() == ("active", True)

        runtime_store = service._runtime_store()
        with (
            patch.object(
                runtime_store,
                "set_projection_active_for_install",
                side_effect=RuntimeError("injected projection failure"),
            ),
            pytest.raises(RuntimeError, match="injected projection failure"),
        ):
            service.disable_extension(seeded["extension_id"], scope=scope)
        assert _persisted_state() == ("active", True)
    finally:
        _cleanup_lifecycle_fixtures(pg_conn)
        direct_conn = getattr(logger, "_direct_conn", None)
        if direct_conn is not None:
            direct_conn.close()


@pytest.mark.integration
def test_extension_update_advisory_lock_is_released_with_pooled_logger(pg_conn) -> None:
    logger = PlatformLogger(resolve_test_db_dsn())
    service = _service(logger)
    extension_id = f"fixture.lock-{uuid.uuid4().hex[:8]}"

    try:
        with (
            service._context.update_lock(bl=logger, extension_id=extension_id),
            pg_conn.cursor() as cur,
        ):
            cur.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s))",
                ("platform.extension_lifecycle",),
            )
            assert cur.fetchone()[0] is False

        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s))",
                ("platform.extension_lifecycle",),
            )
            assert cur.fetchone()[0] is True
            cur.execute(
                "SELECT pg_advisory_unlock(hashtext(%s))",
                ("platform.extension_lifecycle",),
            )
            assert cur.fetchone()[0] is True
        pg_conn.commit()
    finally:
        direct_conn = getattr(logger, "_direct_conn", None)
        if direct_conn is not None:
            direct_conn.close()


@pytest.fixture(autouse=True)
def _isolate_lifecycle_rows(pg_conn, default_account):
    _cleanup_lifecycle_fixtures(pg_conn)
    yield
    _cleanup_lifecycle_fixtures(pg_conn)


@pytest.mark.integration
def test_confirmed_uninstall_cleans_owned_rows_and_rolls_back_on_failure(
    pg_conn, tmp_path, monkeypatch
) -> None:
    logger = PlatformLogger(resolve_test_db_dsn())
    service = _service(logger)
    scope = ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID,
        actor_id="integration-admin",
        principal_type="admin",
    )
    seeded = _seed_lifecycle(pg_conn, tmp_path, uuid.uuid4().hex[:8])
    managed_roots = {seeded["extension_id"]: Path(seeded["bundle_root"])}
    monkeypatch.setattr(
        ManagedReleaseService,
        "_managed_simple_extension_root",
        classmethod(lambda _cls, extension_id: managed_roots[extension_id]),
    )
    workspace_connection_id = f"workspace_{uuid.uuid4().hex[:8]}"
    with pg_conn.cursor() as cur:
        cur.execute("SELECT tenant_id::text FROM accounts WHERE id = %s", (DEFAULT_ACCOUNT_ID,))
        tenant_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO workspace_data_connections (
                connection_id, tenant_id, account_id, workspace_id, display_name,
                connector_ref_jsonb, connection_config_jsonb
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, '{"host":"owned"}'::jsonb)
            """,
            (
                workspace_connection_id,
                tenant_id,
                DEFAULT_ACCOUNT_ID,
                str(uuid.uuid4()),
                "Owned workspace connection",
                json.dumps({"install_id": seeded["install_id"], "connector_id": "fixture"}),
            ),
        )
    pg_conn.commit()
    plan = service.preview_extension_uninstall(seeded["extension_id"], scope=scope)
    assert {row["id"] for row in plan["remove"] if row["kind"] == "workspace_data_connection"} == {
        workspace_connection_id
    }
    service.uninstall_extension(seeded["extension_id"], scope=scope, plan_revision=plan["revision"])

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM extension_installs WHERE install_id = %s",
            (seeded["install_id"],),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT count(*) FROM extension_registry_projections WHERE install_id = %s",
            (seeded["install_id"],),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT count(*) FROM registry_workflows WHERE workflow_id = %s",
            (seeded["workflow_id"],),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT count(*) FROM project_workflows WHERE project_id = %s AND workflow_id = %s",
            (seeded["project_id"], seeded["workflow_id"]),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT count(*) FROM project_connections WHERE connection_id = %s",
            (seeded["connection_id"],),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT count(*) FROM workspace_data_connections WHERE connection_id = %s",
            (workspace_connection_id,),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT extension_install_id, connection_id, status, status_reason, source_ref_jsonb, artifact_refs_jsonb, response_summary_jsonb, error_text, claim_token FROM external_effects WHERE effect_id = %s",
            (seeded["effect_id"],),
        )
        effect = cur.fetchone()
        assert effect[:4] == (None, None, "expired", "extension_uninstalled")
        assert effect[4:8] == ({}, [], {}, None)
        old_token = effect[8]
        cur.execute(
            "UPDATE external_effects SET status = 'succeeded' WHERE effect_id = %s AND claim_token = %s",
            (seeded["effect_id"], "stale-token"),
        )
        assert cur.rowcount == 0
    assert old_token != "stale-token"

    rollback = _seed_lifecycle(pg_conn, tmp_path, uuid.uuid4().hex[:8])
    managed_roots[rollback["extension_id"]] = Path(rollback["bundle_root"])
    pg_conn.commit()
    rollback_plan = service.preview_extension_uninstall(rollback["extension_id"], scope=scope)
    with (
        patch(
            "core.application.management.command_services_project_connection.cleanup_project_connection_owned_data",
            side_effect=RuntimeError("injected cleanup failure"),
        ),
        pytest.raises(RuntimeError, match="injected cleanup failure"),
    ):
        service.uninstall_extension(
            rollback["extension_id"],
            scope=scope,
            plan_revision=rollback_plan["revision"],
        )
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM extension_installs WHERE install_id = %s",
            (rollback["install_id"],),
        )
        assert cur.fetchone()[0] == 1
    assert Path(rollback["bundle_root"]).is_dir()


@pytest.mark.integration
def test_uninstall_and_discovery_completion_use_install_then_provider_lock_order(
    pg_conn, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FS_DB_POOL_ENABLED", "0")
    dsn = str(resolve_test_db_dsn() or "").strip()
    seeded = _seed_lifecycle(pg_conn, tmp_path, uuid.uuid4().hex[:8])
    monkeypatch.setattr(
        ManagedReleaseService,
        "_managed_simple_extension_root",
        classmethod(lambda _cls, _extension_id: Path(seeded["bundle_root"])),
    )
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            UPDATE extension_installs
               SET remote_base_url = 'https://runtime.example.com',
                   config_jsonb = '{
                       "llm_provider_state_revision":"revision-1",
                       "llm_provider_discovery_generation":"generation-1"
                   }'::jsonb
             WHERE account_id = %s AND install_id = %s
            """,
            (DEFAULT_ACCOUNT_ID, seeded["install_id"]),
        )
    llm_store.upsert_source(
        pg_conn,
        account_id=DEFAULT_ACCOUNT_ID,
        extension_install_id=seeded["install_id"],
        extension_id=seeded["extension_id"],
        provider_key=f"llmext_{seeded['install_id']}",
        display_label="Concurrent lifecycle provider",
        supplier_key="fixture",
        supplier_label="Fixture",
        upstream_owner_key=None,
        upstream_owner_label=None,
    )
    pg_conn.commit()

    plan_logger = PlatformLogger(dsn)
    try:
        plan = _service(plan_logger).preview_extension_uninstall(
            seeded["extension_id"],
            scope=ScopeContext(
                account_id=DEFAULT_ACCOUNT_ID,
                actor_id="integration-admin",
                principal_type="admin",
            ),
        )
    finally:
        direct_conn = getattr(plan_logger, "_direct_conn", None)
        if direct_conn is not None:
            direct_conn.close()

    completion_conn = psycopg_runtime.connect(dsn)
    completion_conn.autocommit = False
    lifecycle_events: queue.Queue[tuple[str, object]] = queue.Queue()
    uninstall_thread: threading.Thread | None = None
    try:
        with completion_conn.cursor() as cur:
            cur.execute("SET LOCAL deadlock_timeout = '100ms'")
            cur.execute("SET LOCAL lock_timeout = '5s'")
        assert llm_store.lock_install_discovery_generation_current(
            completion_conn,
            account_id=DEFAULT_ACCOUNT_ID,
            extension_install_id=seeded["install_id"],
            generation="generation-1",
            state_revision="revision-1",
            release_id=seeded["release_id"],
            descriptor_sha256=seeded["descriptor_sha256"],
        )

        def uninstall() -> None:
            logger = PlatformLogger(dsn)
            try:
                with logger.conn.cursor() as cur:
                    cur.execute("SELECT pg_backend_pid()")
                    lifecycle_events.put(("pid", int(cur.fetchone()[0])))
                result = _service(logger).uninstall_extension(
                    seeded["extension_id"],
                    scope=ScopeContext(
                        account_id=DEFAULT_ACCOUNT_ID,
                        actor_id="integration-admin",
                        principal_type="admin",
                    ),
                    plan_revision=plan["revision"],
                )
                lifecycle_events.put(("result", result))
            except BaseException as exc:  # pragma: no cover - relayed to main thread
                lifecycle_events.put(("error", exc))
            finally:
                direct_conn = getattr(logger, "_direct_conn", None)
                if direct_conn is not None:
                    direct_conn.close()

        uninstall_thread = threading.Thread(target=uninstall, daemon=True)
        uninstall_thread.start()
        kind, uninstall_pid = lifecycle_events.get(timeout=5)
        assert kind == "pid"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not lifecycle_events.empty():
                early_kind, early_payload = lifecycle_events.get_nowait()
                pytest.fail(
                    "uninstall exited before waiting for discovery completion: "
                    f"{early_kind}={early_payload!r}"
                )
            with pg_conn.cursor() as cur:
                cur.execute(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                    (uninstall_pid,),
                )
                row = cur.fetchone()
            if row and row[0] == "Lock":
                break
            time.sleep(0.02)
        else:
            pytest.fail("uninstall did not block behind discovery completion")

        llm_store.upsert_source(
            completion_conn,
            account_id=DEFAULT_ACCOUNT_ID,
            extension_install_id=seeded["install_id"],
            extension_id=seeded["extension_id"],
            provider_key=f"llmext_{seeded['install_id']}",
            display_label="Completed lifecycle provider",
            supplier_key="fixture",
            supplier_label="Fixture",
            upstream_owner_key=None,
            upstream_owner_label=None,
        )
        completion_conn.commit()
        uninstall_thread.join(timeout=5)
        assert not uninstall_thread.is_alive()
        kind, value = lifecycle_events.get(timeout=1)
        assert kind == "error"
        assert isinstance(value, ServiceConflictError)
        assert value.payload.get("stale_plan") is True

        assert llm_store.lock_install_discovery_generation_current(
            completion_conn,
            account_id=DEFAULT_ACCOUNT_ID,
            extension_install_id=seeded["install_id"],
            generation="generation-1",
            state_revision="revision-1",
            release_id=seeded["release_id"],
            descriptor_sha256=seeded["descriptor_sha256"],
        )
        completion_conn.rollback()
    finally:
        completion_conn.rollback()
        completion_conn.close()
        if uninstall_thread is not None:
            uninstall_thread.join(timeout=5)
        pg_conn.rollback()
        with pg_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM llm_provider_sources WHERE extension_id = %s",
                (seeded["extension_id"],),
            )
        pg_conn.commit()


@pytest.mark.integration
def test_uninstall_reports_only_optional_host_records_that_were_removed(
    pg_conn, tmp_path, monkeypatch
) -> None:
    logger = PlatformLogger(resolve_test_db_dsn())
    service = _service(logger)
    scope = ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID,
        actor_id="integration-admin",
        principal_type="admin",
    )
    seeded = _seed_lifecycle(pg_conn, tmp_path, uuid.uuid4().hex[:8])
    monkeypatch.setattr(
        ManagedReleaseService,
        "_managed_simple_extension_root",
        classmethod(lambda _cls, _extension_id: Path(seeded["bundle_root"])),
    )
    with pg_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM extension_project_state WHERE extension_id = %s",
            (seeded["extension_id"],),
        )
        cur.execute(
            "DELETE FROM external_effects WHERE extension_id = %s",
            (seeded["extension_id"],),
        )
        cur.execute(
            "DELETE FROM secret_refs WHERE ref_key = %s",
            (f"llmext_cred_{seeded['install_id']}",),
        )
    pg_conn.commit()

    plan = service.preview_extension_uninstall(seeded["extension_id"], scope=scope)
    result = service.uninstall_extension(
        seeded["extension_id"], scope=scope, plan_revision=plan["revision"]
    )

    assert "project_state" not in result.removed_data
    assert "external_effect_audit_detached" not in result.removed_data
    assert "llm_credential_ref" not in result.removed_data


@pytest.mark.integration
def test_uninstall_of_an_oauth_extension_writes_its_audit_through_the_production_stores(
    pg_conn, tmp_path, monkeypatch
) -> None:
    """An extension that declares an OAuth provider can be uninstalled.

    Seen on a stand with File Transfer (dropbox_oauth): the uninstall plan
    answered 200 and the confirmed uninstall 500, because the lifecycle runs on
    the identity store and that store had no command audit writer. Every other
    test here uses the monolithic test store, which has every method, so none
    of them could see it. Google Workspace (workspace_oauth) takes the same path.
    """
    dsn = resolve_test_db_dsn()
    lifecycle_store = PlatformIdentityStore(dsn)
    service = ExtensionBindingService(
        get_logger=lambda: lifecycle_store,
        get_operation_store=lambda: PlatformIntegrationStore(dsn),
        resolve_extension_runtime_record=lambda _extension_id: (None, None),
        extension_identifier=lambda payload: str((payload or {}).get("extension_key") or ""),
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 502,
    )
    scope = ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID,
        actor_id="integration-admin",
        principal_type="admin",
    )
    seeded = _seed_lifecycle(pg_conn, tmp_path, uuid.uuid4().hex[:8])
    monkeypatch.setattr(
        ManagedReleaseService,
        "_managed_simple_extension_root",
        classmethod(lambda _cls, _extension_id: Path(seeded["bundle_root"])),
    )
    descriptor = {**_oauth_descriptor(), "extension_id": seeded["extension_id"]}
    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE extension_releases SET descriptor_jsonb = %s::jsonb WHERE release_id = %s",
            (json.dumps(descriptor), seeded["release_id"]),
        )
    pg_conn.commit()

    plan = service.preview_extension_uninstall(seeded["extension_id"], scope=scope)
    result = service.uninstall_extension(
        seeded["extension_id"], scope=scope, plan_revision=plan["revision"]
    )

    assert result.state == "uninstalled"
    assert "oauth_remote_revocation_not_available" in result.removed_data
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM extension_installs WHERE install_id = %s",
            (seeded["install_id"],),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            """
            SELECT actor_id, status, target_jsonb, result_jsonb
              FROM command_audit_log
             WHERE command_type = 'extension_oauth_uninstall_lifecycle'
               AND target_jsonb->>'extension_id' = %s
            """,
            (seeded["extension_id"],),
        )
        rows = cur.fetchall()
    assert rows == [
        (
            "integration-admin",
            "executed",
            {"extension_id": seeded["extension_id"], "install_id": seeded["install_id"]},
            {"remote_revocation": "not_available"},
        )
    ]


@pytest.mark.integration
@pytest.mark.parametrize("files", ["missing", "plain_folder"])
def test_uninstall_does_not_depend_on_the_extension_files(
    pg_conn, tmp_path, monkeypatch, files
) -> None:
    """Uninstall is the only way out of an install, whatever is left on disk.

    Seen on a stand: an install whose folder was gone answered every uninstall
    with "Installed extension active release path is invalid"; and a plain
    folder left behind by an uninstall was registered again by the next scan.
    """
    logger = PlatformLogger(resolve_test_db_dsn())
    service = _service(logger)
    scope = ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID,
        actor_id="integration-admin",
        principal_type="admin",
    )
    seeded = _seed_lifecycle(pg_conn, tmp_path, uuid.uuid4().hex[:8])
    bundle_root = Path(seeded["bundle_root"])
    monkeypatch.setattr(
        ManagedReleaseService,
        "_managed_simple_extension_root",
        classmethod(lambda _cls, _extension_id: bundle_root),
    )
    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
        lambda: str(tmp_path),
    )
    if files == "missing":
        shutil.rmtree(bundle_root)
    else:
        (bundle_root / ".fs-python" / "3.12").mkdir(parents=True)
    pg_conn.commit()

    plan = service.preview_extension_uninstall(seeded["extension_id"], scope=scope)
    result = service.uninstall_extension(
        seeded["extension_id"], scope=scope, plan_revision=plan["revision"]
    )

    assert result.state == "uninstalled"
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM extension_installs WHERE install_id = %s",
            (seeded["install_id"],),
        )
        assert cur.fetchone()[0] == 0
    assert not bundle_root.exists()
    if files == "missing":
        assert "extension_files_already_missing" in result.removed_data
    else:
        assert "bundle_files" in result.removed_data
    # Nothing outside the managed root is touched; the cleanup journal is done.
    assert (
        sorted(path.name for path in tmp_path.iterdir() if path.name != ".fs-uninstall-cleanup")
        == []
    )


@pytest.mark.integration
def test_global_uninstall_previews_and_cleans_consumer_account_dependencies(
    pg_conn, tmp_path
) -> None:
    logger = PlatformLogger(resolve_test_db_dsn())
    service = _service(logger)
    scope = ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID,
        actor_id="integration-admin",
        principal_type="admin",
    )
    suffix = uuid.uuid4().hex[:8]
    seeded = _seed_lifecycle(pg_conn, tmp_path, suffix)
    consumer_account_id = str(uuid.uuid4())
    consumer_project_id = str(uuid.uuid4())
    consumer_connection_id = f"consumer_conn_{suffix}"
    consumer_effect_id = str(uuid.uuid4())
    consumer_local_install_id = f"pi_consumer_{suffix}"
    consumer_projection_id = f"consumer_workflow_{suffix}"
    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE extension_installs SET visibility = 'global' WHERE install_id = %s",
            (seeded["install_id"],),
        )
        cur.execute(
            "INSERT INTO accounts (id, account_key, display_name) VALUES (%s, %s, %s)",
            (
                consumer_account_id,
                f"lifecycle-consumer-{suffix}",
                f"Lifecycle consumer {suffix}",
            ),
        )
        cur.execute(
            "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s)",
            (consumer_project_id, f"Lifecycle consumer {suffix}", consumer_account_id),
        )
        cur.execute(
            "INSERT INTO extension_installs (extension_id, account_id, install_id, release_id, descriptor_source) VALUES (%s, %s, %s, %s, 'extension_scan')",
            (
                seeded["extension_id"],
                consumer_account_id,
                consumer_local_install_id,
                seeded["release_id"],
            ),
        )
        cur.execute(
            """
            INSERT INTO extension_registry_projections (
                account_id, install_id, release_id, extension_id,
                artifact_kind, artifact_id, projection_jsonb
            ) VALUES (%s, %s, %s, %s, 'workflow', %s, %s::jsonb)
            """,
            (
                consumer_account_id,
                consumer_local_install_id,
                seeded["release_id"],
                "fixture.consumer-workflow",
                consumer_projection_id,
                json.dumps(
                    {"dependencies": {"extensions": [{"extension_id": seeded["extension_id"]}]}}
                ),
            ),
        )
        cur.execute(
            "INSERT INTO project_connections (connection_id, project_id, account_id, extension_id, connection_kind, extension_install_id, secret_refs) VALUES (%s, %s, %s, %s, 'extension_runtime', %s, %s::jsonb)",
            (
                consumer_connection_id,
                consumer_project_id,
                consumer_account_id,
                seeded["extension_id"],
                seeded["install_id"],
                '{"refs":{"api_key":"consumer-owned"}}',
            ),
        )
        cur.execute(
            "INSERT INTO secret_refs (account_id, ref_key, encrypted_value) VALUES (%s, %s, 'encrypted')",
            (
                consumer_account_id,
                f"project_connection:{consumer_connection_id}:api_key",
            ),
        )
        cur.execute(
            "INSERT INTO extension_project_state (account_id, project_id, extension_id, extension_install_id, connection_id, namespace, state_key, value_jsonb) VALUES (%s, %s, %s, %s, %s, 'global-install', 'cursor', '{\"cursor\":1}'::jsonb)",
            (
                consumer_account_id,
                consumer_project_id,
                seeded["extension_id"],
                seeded["install_id"],
                consumer_connection_id,
            ),
        )
        cur.execute(
            "INSERT INTO extension_project_state (account_id, project_id, extension_id, extension_install_id, namespace, state_key, value_jsonb) VALUES (%s, %s, %s, %s, 'consumer-install', 'cursor', '{\"cursor\":2}'::jsonb)",
            (
                consumer_account_id,
                consumer_project_id,
                seeded["extension_id"],
                consumer_local_install_id,
            ),
        )
        cur.execute(
            "INSERT INTO external_effects (effect_id, account_id, project_id, extension_id, extension_install_id, connection_id, operation_id, effect_kind, channel, source_type, idempotency_key, idempotency_scope_hash, request_fingerprint, status) VALUES (%s, %s, %s, %s, %s, %s, 'publish', 'write', 'api', 'action', %s, %s, 'fingerprint', 'succeeded')",
            (
                consumer_effect_id,
                consumer_account_id,
                consumer_project_id,
                seeded["extension_id"],
                seeded["install_id"],
                consumer_connection_id,
                f"consumer-key-{suffix}",
                f"consumer-scope-{suffix}",
            ),
        )
        cur.execute(
            "INSERT INTO extension_usage_events (account_id, install_id, release_id, project_id, connection_id, event_kind) VALUES (%s, %s, %s, %s, %s, 'consumer_query')",
            (
                consumer_account_id,
                seeded["install_id"],
                seeded["release_id"],
                consumer_project_id,
                consumer_connection_id,
            ),
        )
    pg_conn.commit()

    plan = service.preview_extension_uninstall(seeded["extension_id"], scope=scope)
    assert any(
        item.get("kind") == "consumer_runtime_connections" and item.get("count") == 2
        for item in plan["remove"]
    )
    # Consumer usage history is host bookkeeping: it is counted in the one
    # "Extension registration and history" row, not listed on its own.
    records = [item for item in plan["remove"] if item.get("kind") == "extension_records"]
    assert len(records) == 1
    assert records[0]["count"] >= 1
    assert records[0]["label"] == "Extension registration and history"
    assert not any(item.get("kind") == "consumer_usage_records" for item in plan["remove"])
    preview_text = str(plan["remove"])
    assert consumer_account_id not in preview_text
    assert consumer_project_id not in preview_text
    assert consumer_connection_id not in preview_text
    assert not any(item.get("kind") == "runtime_package" for item in plan["remove"])
    assert not any(item.get("kind") == "release" for item in plan["remove"])
    # The shared files and release show as one retained "Extension files" line.
    retained_files = [item for item in plan["retained"] if item.get("kind") == "runtime_package"]
    assert len(retained_files) == 1
    assert retained_files[0]["label"] == "Extension files (another install still uses them)"
    assert not any(item.get("kind") == "release" for item in plan["retained"])
    assert not any(
        item.get("owner_account_id") == consumer_account_id
        and item.get("owner_install_id") == consumer_local_install_id
        and item.get("id") == consumer_projection_id
        for item in plan["affected"]
    )

    service.uninstall_extension(
        seeded["extension_id"],
        scope=scope,
        plan_revision=plan["revision"],
    )

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM project_connections WHERE connection_id = %s",
            (consumer_connection_id,),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT count(*) FROM secret_refs WHERE account_id = %s AND ref_key = %s",
            (
                consumer_account_id,
                f"project_connection:{consumer_connection_id}:api_key",
            ),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT namespace FROM extension_project_state WHERE account_id = %s AND extension_id = %s ORDER BY namespace",
            (consumer_account_id, seeded["extension_id"]),
        )
        assert cur.fetchall() == [("consumer-install",)]
        cur.execute(
            "SELECT count(*) FROM extension_installs WHERE install_id = %s",
            (consumer_local_install_id,),
        )
        assert cur.fetchone()[0] == 1
        cur.execute(
            "SELECT count(*) FROM extension_bundles WHERE extension_key = %s",
            (seeded["extension_id"],),
        )
        assert cur.fetchone()[0] == 1
        cur.execute(
            "SELECT extension_install_id, connection_id FROM external_effects WHERE effect_id = %s",
            (consumer_effect_id,),
        )
        assert cur.fetchone() == (None, None)
        cur.execute(
            """
            SELECT projection_jsonb->'extension_dependency_availability'->>'availability_status'
              FROM extension_registry_projections
             WHERE account_id = %s
               AND install_id = %s
               AND artifact_id = %s
            """,
            (consumer_account_id, consumer_local_install_id, consumer_projection_id),
        )
        assert cur.fetchone()[0] is None
    assert Path(seeded["bundle_root"]).is_dir()


@pytest.mark.integration
def test_extension_data_migration_rechecks_revisions_and_applies_atomically(pg_conn) -> None:
    suffix = uuid.uuid4().hex[:8]
    extension_id = f"fixture.migrating-{suffix}"
    install_id = f"pi_migrating_{suffix}"
    release_id = f"pr_migrating_{suffix}"
    project_id = str(uuid.uuid4())
    workspace_connection_id = f"workspace_migrating_{suffix}"
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO extension_releases (release_id, extension_id, release_version, account_id) VALUES (%s, %s, '2.0.0', %s)",
            (release_id, extension_id, DEFAULT_ACCOUNT_ID),
        )
        cur.execute(
            "INSERT INTO extension_installs (extension_id, account_id, install_id, release_id, config_jsonb, applied_data_schema_version) VALUES (%s, %s, %s, %s, '{\"version\":1}'::jsonb, 1)",
            (extension_id, DEFAULT_ACCOUNT_ID, install_id, release_id),
        )
        cur.execute(
            "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s)",
            (project_id, f"Migrating {suffix}", DEFAULT_ACCOUNT_ID),
        )
    pg_conn.commit()
    migrated = [
        {
            "target_type": "install_config",
            "record_id": install_id,
            "value": {"version": 2},
        }
    ]

    workspace_snapshot = snapshot_extension_data(
        pg_conn,
        account_id=DEFAULT_ACCOUNT_ID,
        extension_id=extension_id,
        install_id=install_id,
    )
    with pg_conn.cursor() as cur:
        cur.execute("SELECT tenant_id::text FROM accounts WHERE id = %s", (DEFAULT_ACCOUNT_ID,))
        tenant_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO workspace_data_connections (
                connection_id, tenant_id, account_id, workspace_id, display_name,
                connector_ref_jsonb, connection_config_jsonb
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, '{}'::jsonb)
            """,
            (
                workspace_connection_id,
                tenant_id,
                DEFAULT_ACCOUNT_ID,
                str(uuid.uuid4()),
                "Migrating workspace connection",
                json.dumps({"install_id": install_id, "connector_id": "fixture"}),
            ),
        )
    pg_conn.commit()
    with (
        pytest.raises(ValueError, match="Workspace data connections changed during migration"),
        pg_conn.transaction(),
        pg_conn.cursor() as cur,
    ):
        apply_extension_data_migrations(
            cur,
            account_id=DEFAULT_ACCOUNT_ID,
            install_id=install_id,
            snapshot=workspace_snapshot,
            migrated=migrated,
            target_version=3,
        )

    state_snapshot = snapshot_extension_data(
        pg_conn,
        account_id=DEFAULT_ACCOUNT_ID,
        extension_id=extension_id,
        install_id=install_id,
    )
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO extension_project_state (
                account_id, project_id, extension_id, extension_install_id,
                namespace, state_key, value_jsonb
            ) VALUES (%s, %s, %s, %s, 'migration', 'late-state', '{}'::jsonb)
            """,
            (DEFAULT_ACCOUNT_ID, project_id, extension_id, install_id),
        )
    pg_conn.commit()
    with (
        pytest.raises(ValueError, match="Extension project state changed during migration"),
        pg_conn.transaction(),
        pg_conn.cursor() as cur,
    ):
        apply_extension_data_migrations(
            cur,
            account_id=DEFAULT_ACCOUNT_ID,
            install_id=install_id,
            snapshot=state_snapshot,
            migrated=migrated,
            target_version=3,
        )

    snapshot = snapshot_extension_data(
        pg_conn,
        account_id=DEFAULT_ACCOUNT_ID,
        extension_id=extension_id,
        install_id=install_id,
    )
    with pg_conn.transaction(), pg_conn.cursor() as cur:
        apply_extension_data_migrations(
            cur,
            account_id=DEFAULT_ACCOUNT_ID,
            install_id=install_id,
            snapshot=snapshot,
            migrated=migrated,
            target_version=2,
        )
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT config_jsonb, applied_data_schema_version FROM extension_installs WHERE install_id = %s",
            (install_id,),
        )
        assert cur.fetchone() == ({"version": 2}, 2)
    pg_conn.commit()

    stale_snapshot = snapshot_extension_data(
        pg_conn,
        account_id=DEFAULT_ACCOUNT_ID,
        extension_id=extension_id,
        install_id=install_id,
    )
    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE extension_installs SET config_jsonb = '{\"version\":99}'::jsonb WHERE install_id = %s",
            (install_id,),
        )
    pg_conn.commit()
    with (
        pytest.raises(ValueError, match="changed during migration"),
        pg_conn.transaction(),
        pg_conn.cursor() as cur,
    ):
        apply_extension_data_migrations(
            cur,
            account_id=DEFAULT_ACCOUNT_ID,
            install_id=install_id,
            snapshot=stale_snapshot,
            migrated=migrated,
            target_version=3,
        )

    with pg_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM workspace_data_connections WHERE connection_id = %s",
            (workspace_connection_id,),
        )
        cur.execute(
            "DELETE FROM extension_project_state WHERE extension_id = %s",
            (extension_id,),
        )
        cur.execute("DELETE FROM projects WHERE id = %s", (project_id,))
        cur.execute("DELETE FROM extension_installs WHERE install_id = %s", (install_id,))
        cur.execute("DELETE FROM extension_releases WHERE release_id = %s", (release_id,))
    pg_conn.commit()
