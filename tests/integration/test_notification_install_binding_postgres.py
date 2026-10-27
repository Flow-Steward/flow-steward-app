"""PostgreSQL coverage for notification runtime install identity binding."""

from __future__ import annotations

import json
from uuid import uuid4

from core.application.notifications.delivery import _resolve_notification_extension
from tests.integration import test_extension_legacy_provider_registration as legacy_fixtures

require_audit_database = legacy_fixtures.require_audit_database
scanned_notification = legacy_fixtures.scanned_notification


def test_notification_resolution_binds_selected_active_install(pg_conn, scanned_notification):
    fixture = scanned_notification
    latest_descriptor = {
        **fixture.descriptor,
        "version": "2.0.0",
        "features": ["notify", "audit"],
    }
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO extension_versions "
            "(id, bundle_id, version, manifest_jsonb, compiled_manifest_jsonb, features_jsonb) "
            "VALUES (%s, %s, '2.0.0', %s::jsonb, %s::jsonb, '[\"notify\",\"audit\"]'::jsonb)",
            (
                uuid4(),
                fixture.bundle_id,
                json.dumps(latest_descriptor),
                json.dumps(latest_descriptor),
            ),
        )
        cur.execute(
            "UPDATE extension_bundles SET latest_version = '2.0.0' WHERE id = %s",
            (fixture.bundle_id,),
        )
    pg_conn.commit()

    legacy_fixtures._set_enabled(fixture, True, version="1.0.0")
    provider = fixture.store.get_provider(fixture.extension_id)
    assert provider is not None

    pg_conn.rollback()
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT install_id, release_id, extension_id, version, state, account_id "
            "FROM extension_installs WHERE extension_id = %s AND account_id = %s",
            (fixture.extension_id, fixture.account_id),
        )
        install_row = cur.fetchone()
    assert install_row[:5] == (
        str(fixture.bundle_id),
        str(fixture.version_id),
        fixture.extension_id,
        "1.0.0",
        "active",
    )
    assert str(install_row[5]) == str(fixture.account_id)

    resolved = _resolve_notification_extension(
        fixture.store,
        provider,
        account_id=fixture.account_id,
    )

    assert resolved.get("extension_install_id") == install_row[0]
    assert resolved.get("install_id") == install_row[0]
    assert resolved.get("release_id") == install_row[1]
    assert resolved.get("extension_key") == install_row[2]
    assert resolved.get("compiled_manifest", {}).get("version") == install_row[3]
