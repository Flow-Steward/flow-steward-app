from __future__ import annotations

import json
import uuid
from unittest.mock import patch

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions.command_service import ExtensionCommandService
from core.application.extensions.extension_scan_lifecycle import (
    sync_extension_scan_with_llm_lifecycle,
)
from core.application.management.scope import ScopeContext
from core.application.settings import llm_provider_extension_repository as extension_repository
from core.application.settings import llm_provider_extension_service as extension_service
from core.application.settings.llm_provider_extension_service import credential_ref_key
from core.application.settings.llm_provider_extension_use_cases import (
    LlmProviderExtensionError,
    LlmProviderExtensionUseCases,
)
from core.infrastructure.db import psycopg_runtime
from core.infrastructure.extension_runtime.extension_runtime_helpers import (
    resolve_extension_runtime_record,
)
from tests._db_env import resolve_test_db_dsn
from tests.support.platform_test_store import PlatformTestStore as PlatformLogger
from tests.support.synthetic_llm_provider import (
    hosted_llm_provider_manifest,
    llm_provider_manifest,
    write_llm_provider_bundle,
)


def _lifecycle_service(logger) -> ExtensionCommandService:
    return ExtensionCommandService(
        get_logger=lambda: logger,
    )


@pytest.fixture
def llm_pg_scope():
    dsn = resolve_test_db_dsn()
    if not dsn:
        pytest.skip("FS_DB_DSN is required for PostgreSQL onboarding verification")
    account_id = DEFAULT_ACCOUNT_ID
    extension_id = f"fixture.llm-aggregator-{uuid.uuid4().hex}"
    install_id = f"pi_{uuid.uuid4().hex}"
    release_id = f"release-{uuid.uuid4().hex}"
    base_secret_ref = credential_ref_key(install_id)
    conn = psycopg_runtime.connect(dsn)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute("SELECT EXISTS (SELECT 1 FROM accounts WHERE id = %s)", (account_id,))
        if not bool(cur.fetchone()[0]):
            pytest.fail("canonical default account fixture is required")
    conn.rollback()
    try:
        yield conn, account_id, extension_id, install_id, release_id
    finally:
        conn.rollback()
        conn.close()
        cleanup = psycopg_runtime.connect(dsn)
        cleanup.autocommit = True
        try:
            with cleanup.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM llm_provider_sources
                     WHERE account_id = %s AND extension_install_id = %s
                    """,
                    (account_id, install_id),
                )
                cur.execute(
                    """
                    DELETE FROM secret_refs
                     WHERE account_id = %s
                       AND (
                            ref_key = %s
                            OR left(ref_key, length(%s) + 1) = %s || '_'
                       )
                    """,
                    (account_id, base_secret_ref, base_secret_ref, base_secret_ref),
                )
                cur.execute(
                    "DELETE FROM extension_installs WHERE account_id = %s AND install_id = %s",
                    (account_id, install_id),
                )
                cur.execute(
                    "DELETE FROM extension_releases WHERE extension_id = %s", (extension_id,)
                )
                cur.execute(
                    """
                    DELETE FROM enabled_extensions
                     WHERE bundle_id IN (
                         SELECT id FROM extension_bundles WHERE extension_key = %s
                     )
                    """,
                    (extension_id,),
                )
                cur.execute(
                    """
                    DELETE FROM extension_versions
                     WHERE bundle_id IN (
                         SELECT id FROM extension_bundles WHERE extension_key = %s
                     )
                    """,
                    (extension_id,),
                )
                cur.execute(
                    "DELETE FROM extension_bundles WHERE extension_key = %s", (extension_id,)
                )
                cur.execute(
                    """
                    SELECT
                        (SELECT count(*) FROM llm_provider_models AS model
                          JOIN llm_provider_sources AS source
                            ON source.id = model.provider_source_id
                         WHERE source.extension_install_id = %s),
                        (SELECT count(*) FROM llm_provider_sources
                         WHERE extension_install_id = %s),
                        (SELECT count(*) FROM secret_refs
                         WHERE account_id = %s
                           AND (ref_key = %s OR left(ref_key, length(%s) + 1) = %s || '_')),
                        (SELECT count(*) FROM extension_installs WHERE install_id = %s),
                        (SELECT count(*) FROM extension_releases WHERE extension_id = %s),
                        (SELECT count(*) FROM extension_bundles WHERE extension_key = %s)
                    """,
                    (
                        install_id,
                        install_id,
                        account_id,
                        base_secret_ref,
                        base_secret_ref,
                        base_secret_ref,
                        install_id,
                        extension_id,
                        extension_id,
                    ),
                )
                assert cur.fetchone() == (0, 0, 0, 0, 0, 0)
        finally:
            cleanup.close()


@pytest.mark.integration
def test_endpoint_connection_save_requires_upstream_base_url(llm_pg_scope, monkeypatch) -> None:
    pg_conn, account_id, extension_id, install_id, release_id = llm_pg_scope
    monkeypatch.setenv("ENCRYPTION_KEY", "integration-test-only-encryption-key")
    descriptor = llm_provider_manifest()
    descriptor["extension_id"] = extension_id
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO extension_releases (
                release_id, extension_id, release_version, descriptor_jsonb,
                source_kind, account_id
            ) VALUES (%s, %s, '1.0.0', %s::jsonb, 'integration_test', %s)
            """,
            (release_id, extension_id, json.dumps(descriptor), account_id),
        )
        cur.execute(
            """
            INSERT INTO extension_installs (
                extension_id, version, account_id, state, install_id, release_id,
                runtime_mode, descriptor_source
            ) VALUES (%s, '1.0.0', %s, 'disabled', %s, %s, 'remote_https', 'integration_test')
            """,
            (extension_id, account_id, install_id, release_id),
        )
    pg_conn.commit()

    logger = PlatformLogger(resolve_test_db_dsn())
    use_cases = LlmProviderExtensionUseCases(pg_conn, get_logger_only=lambda: logger)
    with (
        patch(
            "core.application.extensions.url_safety.assert_safe_remote_https_url",
            return_value="https://runtime.example.com",
        ),
        pytest.raises(LlmProviderExtensionError, match="Fill in the required setting: Base URL"),
    ):
        use_cases.save_credentials(
            install_id,
            account_id,
            secrets={"runtime_bearer_token": "test-runtime-token"},
            connection_config={},
            remote_base_url="https://runtime.example.com",
        )

    pg_conn.rollback()
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT config_jsonb FROM extension_installs WHERE account_id = %s AND install_id = %s",
            (account_id, install_id),
        )
        assert cur.fetchone()[0] == {}
        cur.execute(
            "SELECT count(*) FROM secret_refs WHERE account_id = %s AND ref_key = %s",
            (account_id, credential_ref_key(install_id)),
        )
        assert cur.fetchone()[0] == 0


@pytest.mark.integration
def test_disabled_llm_provider_can_save_enable_discover_and_verify(
    llm_pg_scope, monkeypatch, request, tmp_path
) -> None:
    pg_conn, account_id, extension_id, install_id, release_id = llm_pg_scope
    monkeypatch.setenv("ENCRYPTION_KEY", "integration-test-only-encryption-key")
    descriptor = hosted_llm_provider_manifest()
    descriptor["extension_id"] = extension_id
    # Enablement runs the declared health command, so the descriptor needs a real
    # bundle behind it, and the runtime row takes its bundle root from the stored
    # descriptor rather than from the scan payload.
    bundle_root = write_llm_provider_bundle(tmp_path, descriptor)
    descriptor["bundle_root"] = str(bundle_root)
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO extension_releases (
                release_id, extension_id, release_version, descriptor_jsonb,
                source_kind, account_id
            ) VALUES (%s, %s, '1.0.0', %s::jsonb, 'integration_test', %s)
            """,
            (release_id, extension_id, json.dumps(descriptor), account_id),
        )
        cur.execute(
            """
            INSERT INTO extension_installs (
                extension_id, version, account_id, state, install_id, release_id,
                runtime_mode, descriptor_source
            ) VALUES (%s, '1.0.0', %s, 'disabled', %s, %s, 'remote_https', 'integration_test')
            """,
            (extension_id, account_id, install_id, release_id),
        )
    pg_conn.commit()
    bl = PlatformLogger(resolve_test_db_dsn())

    use_cases = LlmProviderExtensionUseCases(pg_conn, get_logger_only=lambda: bl)
    runtime_record, _runtime = resolve_extension_runtime_record(
        install_id,
        account_id=account_id,
        get_logger_only=lambda: bl,
    )
    # A disabled install resolves to a record that is not enabled, rather than to
    # nothing. The runtime gate is fail-closed and has to tell "disabled" apart
    # from "absent" to say so; collapsing the two is what this used to assert,
    # before 93ea4f710 put the gate lookup in front of the executable one. The
    # mirror of this pair runs again after enable_extension below.
    assert runtime_record is not None
    assert runtime_record["enabled"] is False

    with patch(
        "core.application.extensions.url_safety.assert_safe_remote_https_url",
        return_value="https://runtime.example.com",
    ):
        saved = use_cases.save_credentials(
            install_id,
            account_id,
            secrets={
                "api_key": "test-aggregator-key",
                "runtime_bearer_token": "test-runtime-token",
            },
            connection_config={},
            remote_base_url="https://runtime.example.com",
        )
        assert saved == {
            "status": "saved",
            "install_id": install_id,
            "changed": True,
            "verification_invalidated": True,
        }
        pg_conn.commit()

        # Startup performs this manifest rescan before the operator can run the
        # first authenticated discovery. Operator-owned connection state must
        # survive that reconciliation.
        sync_extension_scan_with_llm_lifecycle(
            bl,
            [
                {
                    "extension_key": extension_id,
                    "kind": descriptor["kind"],
                    "status": "valid",
                    "display_name": "Acme LLM integration fixture",
                    "version": "1.0.0",
                    "bundle_root": str(bundle_root),
                    "manifest_path": str(bundle_root / "extension.yaml"),
                    "manifest": descriptor,
                    "compiled_manifest": descriptor,
                    "account_id": account_id,
                    "config": {"manifest_default": "preserved"},
                }
            ],
        )
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT config_jsonb FROM extension_installs WHERE account_id = %s AND install_id = %s",
                (account_id, install_id),
            )
            rescanned_config = cur.fetchone()[0]
        assert rescanned_config["llm_provider_credential_secret_ref"] == credential_ref_key(
            install_id
        )
        assert rescanned_config["llm_provider_state_revision"]
        # The panel's "API key saved": names and times only, never the values.
        assert set(rescanned_config["llm_provider_saved_secrets"]) == {
            "api_key",
            "runtime_bearer_token",
        }
        assert "test-aggregator-key" not in json.dumps(rescanned_config)
        assert "test-runtime-token" not in json.dumps(rescanned_config)
        assert rescanned_config["manifest_default"] == "preserved"

        _lifecycle_service(bl).enable_extension(
            extension_id,
            scope=ScopeContext(
                account_id=account_id,
                actor_id="integration-test",
                principal_type="admin",
            ),
        )

    runtime_record, _runtime = resolve_extension_runtime_record(
        install_id,
        account_id=account_id,
        get_logger_only=lambda: bl,
    )
    assert runtime_record is not None
    assert runtime_record["enabled"] is True

    discovered_model = {
        "model_id": "deepseek/deepseek-chat",
        "label": "DeepSeek Chat",
        "upstream_owner_key": "deepseek",
        "upstream_owner_label": "DeepSeek",
        "capabilities": ["chat", "tools", "structured_output"],
        "supports_structured_output": True,
        "metadata": {"supported_parameters": ["tools", "response_format"]},
    }
    discovery_inputs: dict[str, object] = {}

    def _capture_first_discovery(
        _record,
        _contract,
        *,
        account_id,
        credentials,
        connection_config,
        get_logger_only,
    ):
        assert callable(get_logger_only)
        discovery_inputs.update(
            {
                "account_id": account_id,
                "credentials": credentials,
                "connection_config": connection_config,
            }
        )
        return [discovered_model]

    with patch.object(
        extension_service,
        "fetch_models_via_remote_runtime",
        side_effect=_capture_first_discovery,
    ):
        refreshed = use_cases.refresh_models(
            install_id,
            account_id,
            connection_config={},
        )
    assert discovery_inputs["account_id"] == account_id
    assert discovery_inputs["credentials"] == {
        "api_key": "test-aggregator-key",
        "runtime_bearer_token": "test-runtime-token",
    }
    assert refreshed["discovered_total"] == 1
    pg_conn.commit()
    # The panel's status line: names of saved secrets and a passed check.
    connection = use_cases.connection_status(install_id, account_id)
    assert [secret["name"] for secret in connection["saved_secrets"]] == [
        "api_key",
        "runtime_bearer_token",
    ]
    assert connection["last_check"]["status"] == "ok"
    assert connection["last_check"]["checked_at"]
    assert "test-aggregator-key" not in json.dumps(connection)
    source_id = refreshed["sources"][0]["source_id"]

    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT id
              FROM llm_provider_models
             WHERE account_id = %s
               AND provider_source_id = %s
               AND model_id = 'deepseek/deepseek-chat'
            """,
            (account_id, source_id),
        )
        model_row = cur.fetchone()
    assert model_row is not None
    model_row_id = str(model_row[0])

    with patch(
        "core.application.settings.llm_capability_verifier.run_capability_suite",
        return_value={"chat", "tools", "structured_output"},
    ):
        enabled = use_cases.set_model_state(
            model_row_id,
            account_id,
            enabled=True,
        )
    assert enabled == {"status": "ok", "model_id": model_row_id, "enabled": True}

    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT enabled, verification_status, verified_capabilities_jsonb
              FROM llm_provider_models
             WHERE account_id = %s AND id = %s
            """,
            (account_id, model_row_id),
        )
        published = cur.fetchone()
    assert published == (
        True,
        "passed",
        ["chat", "structured_output", "tools"],
    )

    second_account_id = str(uuid.uuid4())
    second_install_id = f"install-{uuid.uuid4().hex}"
    second_provider_key = f"llmext_{uuid.uuid4().hex}"

    def _cleanup_second_account() -> None:
        cleanup = psycopg_runtime.connect(resolve_test_db_dsn())
        cleanup.autocommit = True
        try:
            with cleanup.cursor() as cur:
                cur.execute(
                    "DELETE FROM llm_provider_sources WHERE account_id = %s",
                    (second_account_id,),
                )
                cur.execute(
                    "DELETE FROM extension_installs WHERE account_id = %s",
                    (second_account_id,),
                )
                cur.execute("DELETE FROM accounts WHERE id = %s", (second_account_id,))
        finally:
            cleanup.close()

    request.addfinalizer(_cleanup_second_account)
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT release_id, descriptor_sha256
              FROM extension_releases
             WHERE extension_id = %s
            """,
            (extension_id,),
        )
        current_release_id, current_descriptor_sha = cur.fetchone()
        cur.execute(
            """
            INSERT INTO accounts (id, account_key, display_name)
            VALUES (%s, %s, 'Secondary LLM integration account')
            """,
            (second_account_id, f"llm-integration-{uuid.uuid4().hex}"),
        )
        cur.execute(
            """
            INSERT INTO extension_installs (
                extension_id, version, account_id, state, install_id, release_id,
                runtime_mode, remote_base_url, descriptor_source
            ) VALUES (
                %s, '1.0.0', %s, 'active', %s, %s,
                'remote_https', 'https://runtime.example.com', 'integration_test'
            )
            """,
            (extension_id, second_account_id, second_install_id, current_release_id),
        )
    second_source_id = extension_repository.upsert_source(
        pg_conn,
        account_id=second_account_id,
        extension_install_id=second_install_id,
        extension_id=extension_id,
        provider_key=second_provider_key,
        display_label="Secondary Acme Router LLM",
        supplier_key="acme_router_llm",
        supplier_label="Acme Router LLM",
        upstream_owner_key=None,
        upstream_owner_label=None,
        capabilities={"required": ["chat", "tools", "structured_output"]},
    )
    assert second_source_id
    assert (
        extension_repository.upsert_discovered_models(
            pg_conn,
            account_id=second_account_id,
            provider_source_id=second_source_id,
            models=[discovered_model],
        )
        == 1
    )
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            UPDATE llm_provider_models
               SET enabled = true,
                   verification_status = 'passed',
                   verified_capabilities_jsonb = '["chat","structured_output","tools"]'::jsonb,
                   verified_at = now()
             WHERE account_id = %s AND provider_source_id = %s
         RETURNING id
            """,
            (second_account_id, second_source_id),
        )
        second_model_row_id = str(cur.fetchone()[0])
    discovery_generation = f"discovery-{uuid.uuid4().hex}"
    discovery_snapshot = extension_repository.begin_install_discovery(
        pg_conn,
        account_id=second_account_id,
        extension_install_id=second_install_id,
        generation=discovery_generation,
        expected_release_id=str(current_release_id),
        expected_descriptor_sha256=str(current_descriptor_sha),
    )
    assert discovery_snapshot is not None
    discovery_state_revision = str(discovery_snapshot.get("llm_provider_state_revision") or "")
    pg_conn.commit()

    changed_descriptor = {
        **descriptor,
        "description": "Descriptor changed after capability verification",
    }
    changed_scan = [
        {
            "extension_key": extension_id,
            "kind": changed_descriptor["kind"],
            "status": "valid",
            "display_name": "Acme LLM integration fixture",
            "version": "1.0.0",
            "bundle_root": str(bundle_root),
            "manifest_path": str(bundle_root / "extension.yaml"),
            "manifest": changed_descriptor,
            "compiled_manifest": changed_descriptor,
            "account_id": account_id,
            "config": {"manifest_default": "preserved"},
        }
    ]

    # Release publication and route invalidation are one transaction. A failure
    # after the release upsert must expose neither the new descriptor nor a
    # partially updated catalog to another connection.
    with (
        patch.object(
            extension_repository,
            "invalidate_extension_models_for_release_change",
            side_effect=RuntimeError("forced transition failure"),
        ),
        pytest.raises(RuntimeError, match="forced transition failure"),
    ):
        sync_extension_scan_with_llm_lifecycle(bl, changed_scan)
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT descriptor_sha256
              FROM extension_releases
             WHERE release_id = %s
            """,
            (current_release_id,),
        )
        assert cur.fetchone()[0] == current_descriptor_sha
        cur.execute(
            """
            SELECT count(*)
              FROM llm_provider_models
             WHERE id = ANY(%s::uuid[])
               AND enabled = true
               AND verification_status = 'passed'
            """,
            ([model_row_id, second_model_row_id],),
        )
        assert cur.fetchone()[0] == 2

    sync_extension_scan_with_llm_lifecycle(bl, changed_scan)
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text,
                   enabled,
                   verification_status,
                   verified_capabilities_jsonb,
                   verification_error_safe,
                   verified_at,
                   verification_generation
              FROM llm_provider_models
             WHERE id = ANY(%s::uuid[])
             ORDER BY id
            """,
            ([model_row_id, second_model_row_id],),
        )
        invalidated = cur.fetchall()
    assert len(invalidated) == 2
    assert {row[0] for row in invalidated} == {model_row_id, second_model_row_id}
    assert all(
        row[1:]
        == (
            False,
            "stale",
            [],
            "extension_release_or_descriptor_changed",
            None,
            None,
        )
        for row in invalidated
    )
    assert not extension_repository.lock_install_discovery_generation_current(
        pg_conn,
        account_id=second_account_id,
        extension_install_id=second_install_id,
        generation=discovery_generation,
        state_revision=discovery_state_revision,
        release_id=str(current_release_id),
        descriptor_sha256=str(current_descriptor_sha),
    )
    pg_conn.rollback()

    with pg_conn.cursor() as cur:
        cur.execute(
            """
            UPDATE extension_installs
               SET state = 'active', disabled_at = NULL
             WHERE extension_id = %s
            """,
            (extension_id,),
        )
        cur.execute(
            """
            UPDATE llm_provider_models AS model
               SET enabled = true,
                   verification_status = 'passed',
                   verified_capabilities_jsonb = '["chat","structured_output","tools"]'::jsonb,
                   verified_at = now(),
                   verification_error_safe = NULL
              FROM llm_provider_sources AS source
             WHERE model.provider_source_id = source.id
               AND source.extension_id = %s
            """,
            (extension_id,),
        )
    pg_conn.commit()
    invalid_scan = [{**changed_scan[0], "status": "invalid", "scan_error": "invalid manifest"}]
    sync_extension_scan_with_llm_lifecycle(bl, invalid_scan)
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT array_agg(DISTINCT state) FROM extension_installs WHERE extension_id = %s",
            (extension_id,),
        )
        assert cur.fetchone()[0] == ["disabled"]
        cur.execute(
            """
            SELECT count(*)
              FROM llm_provider_models AS model
              JOIN llm_provider_sources AS source ON source.id = model.provider_source_id
             WHERE source.extension_id = %s
               AND model.enabled = false
               AND model.verification_status = 'stale'
               AND model.verification_error_safe = 'extension_invalid_or_missing'
            """,
            (extension_id,),
        )
        assert cur.fetchone()[0] == 2
