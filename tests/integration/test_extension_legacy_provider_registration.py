"""Legacy enablement used by archives and its provider must commit together."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.application.extensions.binding_service import ExtensionBindingService
from core.application.extensions.extension_scan_lifecycle import (
    sync_enabled_extension_with_llm_lifecycle,
)
from core.application.extensions.provider_registration import ProviderRegistrationError
from core.application.management.errors import ServiceValidationError
from tests._db_env import resolve_test_db_dsn
from tests.support.platform_test_store import PlatformTestStore


@pytest.fixture(autouse=True)
def require_audit_database(pg_conn):
    """Guard before any fixture writes; never use a product database."""
    assert pg_conn.info.dbname == "flow_steward_audit"
    assert pg_conn.info.host == "pg-audit"


@pytest.fixture
def scanned_notification(pg_conn, default_account):
    extension_id = f"fixture.notification-{uuid4().hex}"
    bundle_id, version_id = uuid4(), uuid4()
    descriptor = {
        "extension_id": extension_id,
        "kind": "notification_provider",
        "version": "1.0.0",
        "display_name": "Local notification fixture",
        "features": ["notify"],
        "entrypoint": {"mode": "subprocess", "command": ["python3", "main.py"]},
    }
    # Seed only this scan result: a partial whole-catalog scan could disable
    # unrelated fixtures whose filesystem bundles do not exist in this process.
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO extension_bundles "
            "(id, extension_key, kind, display_name, bundle_root, manifest_path, "
            "latest_version, status, account_id) "
            "VALUES (%s, %s, 'notification_provider', %s, '', '', '1.0.0', 'valid', %s)",
            (bundle_id, extension_id, descriptor["display_name"], default_account),
        )
        cur.execute(
            "INSERT INTO extension_versions "
            "(id, bundle_id, version, manifest_jsonb, compiled_manifest_jsonb, features_jsonb) "
            "VALUES (%s, %s, '1.0.0', %s::jsonb, %s::jsonb, '[\"notify\"]'::jsonb)",
            (version_id, bundle_id, json.dumps(descriptor), json.dumps(descriptor)),
        )
    pg_conn.commit()
    store = None
    try:
        store = PlatformTestStore(resolve_test_db_dsn())
        with store.extension_lifecycle_transaction() as conn:
            store.sync_enabled_extension_record(
                conn, extension_id, enabled=False, config={"account_id": str(default_account)}
            )
        yield SimpleNamespace(
            store=store,
            extension_id=extension_id,
            account_id=str(default_account),
            bundle_id=bundle_id,
            version_id=version_id,
            descriptor=descriptor,
        )
    finally:
        pg_conn.rollback()
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM providers WHERE provider_key = %s", (extension_id,))
            cur.execute("DELETE FROM extension_installs WHERE extension_id = %s", (extension_id,))
            cur.execute("DELETE FROM extension_releases WHERE extension_id = %s", (extension_id,))
            cur.execute("DELETE FROM extension_bundles WHERE id = %s", (bundle_id,))
        pg_conn.commit()
        if store is not None and store._direct_conn is not None:
            store._direct_conn.close()


def _set_enabled(fixture, enabled, *, version=None):
    return sync_enabled_extension_with_llm_lifecycle(
        fixture.store,
        fixture.extension_id,
        version=version,
        enabled=enabled,
        config={"account_id": fixture.account_id},
    )


def _provider_rows(pg_conn, extension_id):
    pg_conn.rollback()
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT p.id::text, p.provider_key, pt.key, p.implementation_kind, "
            "p.integration_key, p.config_jsonb, p.features_jsonb, p.enabled "
            "FROM providers p JOIN provider_types pt ON pt.id = p.provider_type_id "
            "WHERE p.provider_key = %s",
            (extension_id,),
        )
        return cur.fetchall()


def _enablement(pg_conn, fixture):
    pg_conn.rollback()
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT ee.enabled, install.state FROM enabled_extensions ee "
            "JOIN extension_installs install ON install.extension_id = %s "
            "AND install.account_id = %s WHERE ee.bundle_id = %s",
            (fixture.extension_id, fixture.account_id, fixture.bundle_id),
        )
        return cur.fetchone()


def _lifecycle_snapshot(pg_conn, fixture):
    pg_conn.rollback()
    queries = (
        ("SELECT to_jsonb(p) FROM providers p WHERE provider_key = %s", fixture.extension_id),
        ("SELECT to_jsonb(e) FROM enabled_extensions e WHERE bundle_id = %s", fixture.bundle_id),
        (
            "SELECT to_jsonb(i) FROM extension_installs i WHERE extension_id = %s",
            fixture.extension_id,
        ),
        (
            "SELECT to_jsonb(r) FROM extension_releases r WHERE extension_id = %s",
            fixture.extension_id,
        ),
    )
    with pg_conn.cursor() as cur:
        rows = []
        for query, identity in queries:
            cur.execute(query, (identity,))
            rows.append(cur.fetchall())
        return rows


def test_legacy_enable_commits_exact_notification_provider(pg_conn, scanned_notification):
    fixture = scanned_notification
    assert _provider_rows(pg_conn, fixture.extension_id) == []

    result = _set_enabled(fixture, True)

    assert result["enabled"] is True
    assert _enablement(pg_conn, fixture) == (True, "active")
    rows = _provider_rows(pg_conn, fixture.extension_id)
    assert len(rows) == 1
    provider_id, *fields = rows[0]
    assert fields == [
        fixture.extension_id,
        "notification_provider",
        "extension",
        fixture.extension_id,
        {"extension_key": fixture.extension_id},
        ["notify"],
        True,
    ]
    with fixture.store.conn.cursor() as cur:
        assert (
            fixture.store._resolve_notification_provider_id_cur(cur, fixture.extension_id)
            == provider_id
        )


def test_legacy_disable_and_reenable_preserve_provider_identity(pg_conn, scanned_notification):
    fixture = scanned_notification
    _set_enabled(fixture, True)
    provider_id = _provider_rows(pg_conn, fixture.extension_id)[0][0]

    _set_enabled(fixture, False)

    assert _enablement(pg_conn, fixture) == (False, "disabled")
    assert _provider_rows(pg_conn, fixture.extension_id)[0][-1] is False
    with fixture.store.conn.cursor() as cur, pytest.raises(ValueError, match="disabled"):
        fixture.store._resolve_notification_provider_id_cur(cur, fixture.extension_id)

    _set_enabled(fixture, True)
    _set_enabled(fixture, True)
    rows = _provider_rows(pg_conn, fixture.extension_id)
    assert len(rows) == 1
    assert rows[0][0] == provider_id
    assert rows[0][-1] is True
    assert _enablement(pg_conn, fixture) == (True, "active")


def test_explicit_version_registers_the_selected_release_features(pg_conn, scanned_notification):
    fixture = scanned_notification
    latest = {**fixture.descriptor, "version": "2.0.0", "features": ["notify", "audit"]}
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO extension_versions "
            "(bundle_id, version, manifest_jsonb, compiled_manifest_jsonb, features_jsonb) "
            "VALUES (%s, '2.0.0', %s::jsonb, %s::jsonb, '[\"notify\",\"audit\"]'::jsonb)",
            (fixture.bundle_id, json.dumps(latest), json.dumps(latest)),
        )
        cur.execute(
            "UPDATE extension_bundles SET latest_version = '2.0.0' WHERE id = %s",
            (fixture.bundle_id,),
        )
    pg_conn.commit()

    _set_enabled(fixture, True, version="1.0.0")

    rows = _provider_rows(pg_conn, fixture.extension_id)
    assert len(rows) == 1
    assert rows[0][-2] == ["notify"]
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT version, release_id FROM extension_installs WHERE extension_id = %s",
            (fixture.extension_id,),
        )
        assert cur.fetchone() == ("1.0.0", str(fixture.version_id))


@pytest.mark.parametrize("initially_enabled", [False, True])
def test_provider_write_failure_rolls_back_entire_transition(
    pg_conn, scanned_notification, monkeypatch, initially_enabled
):
    fixture = scanned_notification
    if initially_enabled:
        _set_enabled(fixture, True)
    before = _lifecycle_snapshot(pg_conn, fixture)
    save_provider = fixture.store.save_provider

    def fail_after_real_provider_write(data):
        save_provider(data)
        with fixture.store.conn.cursor() as cur:
            cur.execute("SELECT 1 / 0")

    monkeypatch.setattr(fixture.store, "save_provider", fail_after_real_provider_write)

    with pytest.raises(ProviderRegistrationError) as raised:
        _set_enabled(fixture, not initially_enabled)

    assert raised.value.__cause__.sqlstate == "22012"
    assert _lifecycle_snapshot(pg_conn, fixture) == before
    assert _enablement(pg_conn, fixture) == (
        initially_enabled,
        "active" if initially_enabled else "disabled",
    )


def test_failed_health_gate_does_not_register_provider(pg_conn, scanned_notification, monkeypatch):
    fixture = scanned_notification
    descriptor = {**fixture.descriptor, "healthcheck": {"command": ["python3", "health.py"]}}
    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE extension_versions SET compiled_manifest_jsonb = %s::jsonb WHERE id = %s",
            (json.dumps(descriptor), fixture.version_id),
        )
    pg_conn.commit()
    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime.run_extension_health",
        lambda _descriptor: {"status": "failed", "detail": "local fixture unhealthy"},
    )

    def forbid_provider_write(_data):
        pytest.fail("Provider registration must not precede the health gate")

    monkeypatch.setattr(fixture.store, "save_provider", forbid_provider_write)
    service = ExtensionBindingService(
        get_logger=lambda: fixture.store,
        get_operation_store=lambda: fixture.store,
        resolve_extension_runtime_record=lambda _extension_id: (None, None),
        extension_identifier=lambda row: str((row or {}).get("extension_key") or ""),
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 502,
    )

    with pytest.raises(ServiceValidationError, match="health check failed"):
        service.set_enabled_state(
            fixture.extension_id,
            version=None,
            enabled=True,
            config={"account_id": fixture.account_id},
            _skip_rescan=True,
        )

    assert _provider_rows(pg_conn, fixture.extension_id) == []
    assert _enablement(pg_conn, fixture) == (False, "disabled")
