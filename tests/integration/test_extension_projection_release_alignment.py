"""An installed extension's projections stay bound to the release its install names.

The archive install writes two releases of one bundle version: the scan release
(the bundle version's id) and the descriptor release (``pr_…``) that owns the
artifact snapshots and every registry projection. Enabling the new install then
synced the scan record without preserving state and repointed
``extension_installs.release_id`` at the scan release. The projection reader
requires the two release ids to match, so every saved query and dataset an
extension ships answered "unavailable for new runs" (Showcase
``demo_dataset_open_orders``, stand fs-main-r23).

Built on the canonical schema in a disposable PostgreSQL, written the way the
installer writes it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions.extension_runtime import (
    ExtensionArtifactSnapshotRecord,
    ExtensionRegistryProjectionRecord,
    ExtensionReleaseRecord,
)
from core.application.extensions.extension_scan_lifecycle import (
    sync_extension_scan_with_llm_lifecycle,
)
from core.application.extensions.install import DBExtensionInstallStore
from core.application.extensions.runtime_store_db import DBExtensionRuntimeStore
from core.application.registry.db_store import RegistryDBStore
from core.infrastructure.db import psycopg_runtime
from core.infrastructure.db.platform_stores import PlatformIdentityStore
from tests import _disposable_db

pytestmark = pytest.mark.integration

ACCOUNT_ID = DEFAULT_ACCOUNT_ID


@pytest.fixture(scope="module")
def database() -> Iterator[Any]:
    reason = _disposable_db.unavailable_reason()
    if reason:
        pytest.skip(f"disposable canonical PostgreSQL unavailable: {reason}")
    started = _disposable_db.start_disposable_db()
    try:
        _disposable_db.apply_canonical_state(started)
        yield started
    finally:
        _disposable_db.stop_disposable_db(started.container)


@pytest.fixture
def conn(database: Any) -> Iterator[Any]:
    connection = psycopg_runtime.connect(database.dsn)
    connection.autocommit = True
    try:
        yield connection
    finally:
        connection.close()


def _descriptor(extension_id: str, version: str) -> dict[str, Any]:
    return {
        "extension_id": extension_id,
        "kind": "tool_provider",
        "version": version,
        "description": "Projection release alignment fixture",
        "entrypoint": {"mode": "in_process"},
        "runtime": {"contract_version": "extension_host_v2"},
    }


def _scan(logger: PlatformIdentityStore, extension_id: str, version: str) -> None:
    descriptor = _descriptor(extension_id, version)
    sync_extension_scan_with_llm_lifecycle(
        logger,
        [
            {
                "extension_key": extension_id,
                "kind": "tool_provider",
                "status": "valid",
                "display_name": "Alignment fixture",
                "version": version,
                "manifest": descriptor,
                "compiled_manifest": descriptor,
                "account_id": ACCOUNT_ID,
                "config": {},
            }
        ],
    )


def _install_descriptor_release(conn: Any, extension_id: str, version: str) -> tuple[str, str]:
    """What the descriptor install writes: release, snapshots, install, projections."""
    runtime = DBExtensionRuntimeStore(conn)
    release = runtime.create_release(
        ExtensionReleaseRecord(
            release_id=f"pr_{uuid.uuid4().hex[:24]}",
            extension_id=extension_id,
            release_version=version,
            descriptor=_descriptor(extension_id, version),
            source_kind="extension_scan",
            source_ref=extension_id,
            account_id=ACCOUNT_ID,
        )
    )
    runtime.replace_artifact_snapshots(
        release.release_id,
        [
            ExtensionArtifactSnapshotRecord(
                release_id=release.release_id,
                artifact_kind="saved_query",
                artifact_id="open_orders_query",
                snapshot={"query_template_id": "open_orders_query", "dataset_id": "orders"},
            )
        ],
    )
    install = DBExtensionInstallStore(conn).install(
        extension_id=extension_id,
        version=version,
        account_id=ACCOUNT_ID,
        release_id=release.release_id,
        descriptor_source="extension_scan",
    )
    runtime.upsert_projection(
        ExtensionRegistryProjectionRecord(
            account_id=ACCOUNT_ID,
            install_id=install.install_id,
            release_id=release.release_id,
            extension_id=extension_id,
            artifact_kind="saved_query",
            artifact_id="open_orders_query",
            projection={"query_template_id": "open_orders_query", "dataset_id": "orders"},
        )
    )
    return install.install_id, release.release_id


def _enable(logger: PlatformIdentityStore, extension_id: str) -> None:
    """The enable that follows a new archive install (set_enabled_state_locked)."""
    with logger.extension_lifecycle_transaction() as transaction_conn:
        logger.sync_enabled_extension_record(transaction_conn, extension_id, enabled=True)


def _install_release(conn: Any, install_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT release_id FROM extension_installs WHERE install_id = %s", (install_id,)
        )
        return str(cur.fetchone()[0])


def _projection(database: Any, install_id: str) -> dict[str, Any] | None:
    return RegistryDBStore(database.dsn).get_extension_registry_projection(
        account_id=ACCOUNT_ID,
        install_id=install_id,
        artifact_kind="saved_query",
        artifact_id="open_orders_query",
    )


def test_enabling_a_new_install_keeps_its_projections_readable(database: Any, conn: Any) -> None:
    extension_id = f"fixture.alignment-{uuid.uuid4().hex[:12]}"
    logger = PlatformIdentityStore(database.dsn)
    _scan(logger, extension_id, "1.0.0")
    install_id, release_id = _install_descriptor_release(conn, extension_id, "1.0.0")

    _enable(logger, extension_id)

    assert _install_release(conn, install_id) == release_id
    projection = _projection(database, install_id)
    assert projection is not None
    assert projection["release_id"] == release_id


def test_the_dataset_query_step_finds_the_saved_query_projection(database: Any, conn: Any) -> None:
    """The step's own lookup and gate, on the rows the installer wrote."""
    from core.application.orchestration import dataset_query_step_runtime as step

    extension_id = f"fixture.alignment-step-{uuid.uuid4().hex[:12]}"
    logger = PlatformIdentityStore(database.dsn)
    _scan(logger, extension_id, "1.0.0")
    install_id, _release_id = _install_descriptor_release(conn, extension_id, "1.0.0")
    _enable(logger, extension_id)

    row = step._get_projection(
        RegistryDBStore(database.dsn),
        account_id=ACCOUNT_ID,
        install_id=install_id,
        artifact_kind="saved_query",
        artifact_id="open_orders_query",
    )

    assert row is not None
    assert step._projection_is_available(row, active_extension_ids={extension_id}) is True


def test_a_rescan_returns_an_already_repointed_install_to_its_projections(
    database: Any, conn: Any
) -> None:
    """Installs repointed before the fix heal on their next sync, without a reinstall."""
    extension_id = f"fixture.alignment-heal-{uuid.uuid4().hex[:12]}"
    logger = PlatformIdentityStore(database.dsn)
    _scan(logger, extension_id, "1.0.0")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT release_id FROM extension_installs WHERE extension_id = %s",
            (extension_id,),
        )
        scan_release_id = str(cur.fetchone()[0])
    install_id, release_id = _install_descriptor_release(conn, extension_id, "1.0.0")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE extension_installs SET release_id = %s WHERE install_id = %s",
            (scan_release_id, install_id),
        )
    assert _projection(database, install_id) is None

    _enable(logger, extension_id)

    assert _install_release(conn, install_id) == release_id
    assert _projection(database, install_id) is not None


def test_a_projection_from_an_older_release_stays_unavailable(database: Any, conn: Any) -> None:
    extension_id = f"fixture.alignment-old-{uuid.uuid4().hex[:12]}"
    logger = PlatformIdentityStore(database.dsn)
    _scan(logger, extension_id, "1.0.0")
    install_id, old_release_id = _install_descriptor_release(conn, extension_id, "1.0.0")
    _enable(logger, extension_id)

    # 1.1.0 arrives through a scan alone: the install moves on, the 1.0.0
    # projection is left behind and must not serve the new release.
    _scan(logger, extension_id, "1.1.0")

    assert _install_release(conn, install_id) != old_release_id
    assert _projection(database, install_id) is None
