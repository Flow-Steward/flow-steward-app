from __future__ import annotations

import queue
import threading
import time
import uuid
from typing import Any

import pytest

from core.application.extensions.install import DBExtensionInstallStore
from core.application.settings import llm_provider_extension_repository as store
from core.infrastructure.db import psycopg_runtime
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
            CREATE TABLE extension_installs (
                extension_id text NOT NULL,
                version text NOT NULL DEFAULT '1.0.0',
                account_id uuid NOT NULL,
                install_id text NOT NULL,
                release_id text,
                descriptor_schema_version text DEFAULT 'extension_descriptor_v1',
                descriptor_origin text DEFAULT 'imported',
                descriptor_source text DEFAULT 'file',
                provenance_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                runtime_mode text,
                config_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                remote_base_url text,
                handshake_status text NOT NULL DEFAULT 'unknown',
                health_status text NOT NULL DEFAULT 'unknown',
                runtime_status text NOT NULL DEFAULT 'unknown',
                last_error text,
                visibility text NOT NULL DEFAULT 'account',
                state text NOT NULL DEFAULT 'active',
                installed_by text NOT NULL DEFAULT '',
                installed_at timestamptz NOT NULL DEFAULT now(),
                applied_data_schema_version integer NOT NULL DEFAULT 1,
                PRIMARY KEY (extension_id, account_id),
                UNIQUE (install_id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE extension_releases (
                release_id text PRIMARY KEY,
                extension_id text NOT NULL,
                descriptor_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                descriptor_sha256 text
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE secret_refs (
                account_id uuid NOT NULL,
                ref_key text NOT NULL,
                encrypted_value text NOT NULL,
                PRIMARY KEY (account_id, ref_key)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE llm_provider_sources (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                account_id uuid NOT NULL,
                extension_install_id text NOT NULL,
                extension_id text NOT NULL,
                provider_key text NOT NULL,
                credential_secret_ref text,
                connection_config_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                updated_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
    conn.commit()


@pytest.mark.integration
def test_discovery_waits_for_atomic_endpoint_and_credential_revision() -> None:
    dsn = str(resolve_test_db_dsn() or "").strip()
    if not dsn:
        pytest.skip("FS_DB_DSN is required for PostgreSQL concurrency verification")

    account_id = str(uuid.uuid4())
    install_id = f"snapshot-{uuid.uuid4().hex}"
    extension_id = f"fixture.snapshot-{uuid.uuid4().hex}"
    provider_key = f"llmext_snapshot_{uuid.uuid4().hex}"
    schema_name = f"llm_snapshot_{uuid.uuid4().hex}"
    old_ref = f"snapshot-old-{uuid.uuid4().hex}"
    new_ref = f"snapshot-new-{uuid.uuid4().hex}"
    setup = psycopg_runtime.connect(dsn)
    updater = psycopg_runtime.connect(dsn)
    discovered: queue.Queue[tuple[str, Any]] = queue.Queue()
    thread: threading.Thread | None = None
    try:
        _create_isolated_schema(setup, schema_name)
        _set_search_path(updater, schema_name)
        with setup.cursor() as cur:
            cur.execute(
                """
                INSERT INTO extension_releases (
                    release_id, extension_id, descriptor_jsonb, descriptor_sha256
                ) VALUES (%s, %s, %s::jsonb, %s)
                """,
                (
                    "release-1",
                    extension_id,
                    '{"extension_id":"' + extension_id + '"}',
                    "a" * 64,
                ),
            )
            cur.execute(
                """
                INSERT INTO extension_installs (
                    extension_id, account_id, install_id, release_id,
                    remote_base_url, config_jsonb
                ) VALUES (%s, %s, %s, %s, %s, '{}'::jsonb)
                """,
                (
                    extension_id,
                    account_id,
                    install_id,
                    "release-1",
                    "https://runtime-old.example",
                ),
            )
            cur.execute(
                "INSERT INTO secret_refs (account_id, ref_key, encrypted_value) VALUES (%s, %s, %s)",
                (account_id, old_ref, "old-secret"),
            )
            cur.execute(
                """
                INSERT INTO llm_provider_sources (
                    account_id, extension_install_id, extension_id, provider_key,
                    credential_secret_ref, connection_config_jsonb
                ) VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    account_id,
                    install_id,
                    extension_id,
                    provider_key,
                    old_ref,
                    '{"upstream_base_url":"https://upstream-old.example"}',
                ),
            )
        setup.commit()

        with updater.cursor() as cur:
            cur.execute(
                "SELECT install_id FROM extension_installs WHERE account_id = %s AND install_id = %s FOR UPDATE",
                (account_id, install_id),
            )
            cur.execute(
                "UPDATE extension_installs SET remote_base_url = %s WHERE account_id = %s AND install_id = %s",
                ("https://runtime-new.example", account_id, install_id),
            )
            cur.execute(
                "INSERT INTO secret_refs (account_id, ref_key, encrypted_value) VALUES (%s, %s, %s)",
                (account_id, new_ref, "new-secret"),
            )
            cur.execute(
                """
                UPDATE llm_provider_sources
                   SET credential_secret_ref = %s,
                       connection_config_jsonb = %s::jsonb
                 WHERE account_id = %s AND extension_install_id = %s
                """,
                (
                    new_ref,
                    '{"upstream_base_url":"https://upstream-new.example"}',
                    account_id,
                    install_id,
                ),
            )

        def discover() -> None:
            conn = psycopg_runtime.connect(dsn)
            try:
                _set_search_path(conn, schema_name)
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_backend_pid()")
                    discovered.put(("pid", int(cur.fetchone()[0])))
                snapshot = store.begin_install_discovery(
                    conn,
                    account_id=account_id,
                    extension_install_id=install_id,
                    generation="generation-new",
                    expected_release_id="release-1",
                    expected_descriptor_sha256="a" * 64,
                )
                conn.commit()
                discovered.put(("snapshot", snapshot))
            except BaseException as exc:  # pragma: no cover - relayed to main thread
                discovered.put(("error", exc))
            finally:
                conn.close()

        thread = threading.Thread(target=discover, daemon=True)
        thread.start()
        kind, backend_pid = discovered.get(timeout=5)
        assert kind == "pid"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with setup.cursor() as cur:
                cur.execute(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                    (backend_pid,),
                )
                row = cur.fetchone()
            if row and row[0] == "Lock":
                break
            time.sleep(0.02)
        else:
            pytest.fail("discovery did not block on the install row lock")

        updater.commit()
        thread.join(timeout=5)
        assert not thread.is_alive()
        kind, value = discovered.get(timeout=1)
        if kind == "error":
            assert isinstance(value, BaseException)
            raise value
        assert kind == "snapshot"
        assert value["release_id"] == "release-1"
        assert value["descriptor_sha256"] == "a" * 64
        assert value["remote_base_url"] == "https://runtime-new.example"
        assert value["credential_secret_ref"] == new_ref
        assert value["encrypted_value"] == "new-secret"
        assert value["connection_config_jsonb"] == {
            "upstream_base_url": "https://upstream-new.example"
        }
    finally:
        updater.rollback()
        if thread is not None:
            thread.join(timeout=5)
        setup.rollback()
        setup.autocommit = True
        with setup.cursor() as cur:
            cur.execute(
                psycopg_runtime.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    psycopg_runtime.sql.Identifier(schema_name)
                )
            )
        setup.close()
        updater.close()


@pytest.mark.integration
def test_discovery_claim_and_completion_cas_require_release_identity() -> None:
    dsn = str(resolve_test_db_dsn() or "").strip()
    if not dsn:
        pytest.skip("FS_DB_DSN is required for PostgreSQL release CAS verification")

    account_id = str(uuid.uuid4())
    install_id = f"release-cas-{uuid.uuid4().hex}"
    schema_name = f"llm_release_cas_{uuid.uuid4().hex}"
    conn = psycopg_runtime.connect(dsn)
    try:
        _create_isolated_schema(conn, schema_name)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO extension_releases (
                    release_id, extension_id, descriptor_jsonb, descriptor_sha256
                ) VALUES (%s, %s, %s::jsonb, %s)
                """,
                (
                    "release-new",
                    "fixture.release-cas",
                    '{"extension_id":"fixture.release-cas"}',
                    "a" * 64,
                ),
            )
            cur.execute(
                """
                INSERT INTO extension_installs (
                    extension_id, account_id, install_id, release_id,
                    remote_base_url, config_jsonb
                ) VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    "fixture.release-cas",
                    account_id,
                    install_id,
                    "release-new",
                    "https://runtime.example.com",
                    '{"llm_provider_state_revision":"revision-1"}',
                ),
            )
        conn.commit()

        with pytest.raises(store.InstallDiscoveryReleaseMismatchError):
            store.begin_install_discovery(
                conn,
                account_id=account_id,
                extension_install_id=install_id,
                generation="stale-generation",
                expected_release_id="release-old",
                expected_descriptor_sha256="a" * 64,
            )
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT config_jsonb ->> 'llm_provider_discovery_generation'
                  FROM extension_installs
                 WHERE account_id = %s AND install_id = %s
                """,
                (account_id, install_id),
            )
            assert cur.fetchone()[0] is None

        snapshot = store.begin_install_discovery(
            conn,
            account_id=account_id,
            extension_install_id=install_id,
            generation="generation-1",
            expected_release_id="release-new",
            expected_descriptor_sha256="a" * 64,
        )
        assert snapshot is not None
        assert snapshot["release_id"] == "release-new"
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE extension_installs
                   SET release_id = 'release-later'
                 WHERE account_id = %s AND install_id = %s
                """,
                (account_id, install_id),
            )
        conn.commit()

        assert not store.lock_install_discovery_generation_current(
            conn,
            account_id=account_id,
            extension_install_id=install_id,
            generation="generation-1",
            state_revision="revision-1",
            release_id="release-new",
            descriptor_sha256="a" * 64,
        )
        conn.rollback()
    finally:
        conn.rollback()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                psycopg_runtime.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    psycopg_runtime.sql.Identifier(schema_name)
                )
            )
        conn.close()


@pytest.mark.integration
def test_discovery_rejects_blank_release_identity_before_claim_or_publication() -> None:
    dsn = str(resolve_test_db_dsn() or "").strip()
    if not dsn:
        pytest.skip("FS_DB_DSN is required for PostgreSQL release CAS verification")

    account_id = str(uuid.uuid4())
    install_id = f"blank-release-cas-{uuid.uuid4().hex}"
    schema_name = f"llm_blank_release_cas_{uuid.uuid4().hex}"
    conn = psycopg_runtime.connect(dsn)
    try:
        _create_isolated_schema(conn, schema_name)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO extension_installs (
                    extension_id, account_id, install_id, release_id,
                    remote_base_url, config_jsonb
                ) VALUES (%s, %s, %s, NULL, %s, %s::jsonb)
                """,
                (
                    "fixture.blank-release-cas",
                    account_id,
                    install_id,
                    "https://runtime.example.com",
                    '{"llm_provider_state_revision":"revision-1"}',
                ),
            )
        conn.commit()

        for expected_release_id in ("", "release-1"):
            with pytest.raises(store.InstallDiscoveryReleaseMismatchError):
                store.begin_install_discovery(
                    conn,
                    account_id=account_id,
                    extension_install_id=install_id,
                    generation="generation-1",
                    expected_release_id=expected_release_id,
                    expected_descriptor_sha256="a" * 64,
                )
            conn.rollback()

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT config_jsonb ->> 'llm_provider_discovery_generation'
                  FROM extension_installs
                 WHERE account_id = %s AND install_id = %s
                """,
                (account_id, install_id),
            )
            assert cur.fetchone()[0] is None
            cur.execute(
                """
                UPDATE extension_installs
                   SET release_id = 'release-1',
                       config_jsonb = jsonb_set(
                           config_jsonb,
                           '{llm_provider_discovery_generation}',
                           '"generation-1"'::jsonb
                       )
                 WHERE account_id = %s AND install_id = %s
                """,
                (account_id, install_id),
            )
        conn.commit()

        assert not store.lock_install_discovery_generation_current(
            conn,
            account_id=account_id,
            extension_install_id=install_id,
            generation="generation-1",
            state_revision="revision-1",
            release_id="",
            descriptor_sha256="a" * 64,
        )
        conn.rollback()

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE extension_installs
                   SET release_id = NULL
                 WHERE account_id = %s AND install_id = %s
                """,
                (account_id, install_id),
            )
        conn.commit()

        publication_reached = store.lock_install_discovery_generation_current(
            conn,
            account_id=account_id,
            extension_install_id=install_id,
            generation="generation-1",
            state_revision="revision-1",
            release_id="release-1",
            descriptor_sha256="a" * 64,
        )
        assert not publication_reached
        conn.rollback()

        assert not store.lock_install_discovery_generation_current(
            conn,
            account_id=account_id,
            extension_install_id=install_id,
            generation="generation-1",
            state_revision="revision-1",
            release_id="",
            descriptor_sha256="a" * 64,
        )
        conn.rollback()
    finally:
        conn.rollback()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                psycopg_runtime.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    psycopg_runtime.sql.Identifier(schema_name)
                )
            )
        conn.close()


@pytest.mark.integration
def test_same_release_descriptor_drift_before_claim_is_rejected() -> None:
    dsn = str(resolve_test_db_dsn() or "").strip()
    if not dsn:
        pytest.skip("FS_DB_DSN is required for PostgreSQL descriptor CAS verification")

    account_id = str(uuid.uuid4())
    install_id = f"descriptor-claim-{uuid.uuid4().hex}"
    extension_id = f"fixture.descriptor-claim-{uuid.uuid4().hex}"
    release_id = "same-version-release"
    schema_name = f"llm_descriptor_claim_{uuid.uuid4().hex}"
    conn = psycopg_runtime.connect(dsn)
    scanner = psycopg_runtime.connect(dsn)
    claim_results: queue.Queue[tuple[str, Any]] = queue.Queue()
    claim_thread: threading.Thread | None = None
    try:
        _create_isolated_schema(conn, schema_name)
        _set_search_path(scanner, schema_name)
        with scanner.cursor() as cur:
            cur.execute(
                """
                INSERT INTO extension_releases (
                    release_id, extension_id, descriptor_jsonb, descriptor_sha256
                ) VALUES (
                    %s, %s,
                    jsonb_build_object('extension_id', %s::text, 'revision', 'old'),
                    %s
                )
                """,
                (release_id, extension_id, extension_id, "a" * 64),
            )
            cur.execute(
                """
                INSERT INTO extension_installs (
                    extension_id, account_id, install_id, release_id,
                    remote_base_url, config_jsonb
                ) VALUES (%s, %s, %s, %s, %s, '{}'::jsonb)
                """,
                (
                    extension_id,
                    account_id,
                    install_id,
                    release_id,
                    "https://runtime.example.com",
                ),
            )
        scanner.commit()

        with scanner.cursor() as cur:
            cur.execute(
                """
                SELECT install_id
                  FROM extension_installs
                 WHERE account_id = %s AND install_id = %s
                 FOR UPDATE
                """,
                (account_id, install_id),
            )
            assert cur.fetchone()[0] == install_id
            cur.execute(
                """
                UPDATE extension_releases
                   SET descriptor_jsonb = jsonb_build_object(
                           'extension_id', extension_id, 'revision', 'new'
                       ),
                       descriptor_sha256 = %s
                 WHERE release_id = %s
                """,
                ("b" * 64, release_id),
            )

        def claim() -> None:
            claimant = psycopg_runtime.connect(dsn)
            try:
                _set_search_path(claimant, schema_name)
                with claimant.cursor() as cur:
                    cur.execute("SELECT pg_backend_pid()")
                    claim_results.put(("pid", int(cur.fetchone()[0])))
                store.begin_install_discovery(
                    claimant,
                    account_id=account_id,
                    extension_install_id=install_id,
                    generation="generation-1",
                    expected_release_id=release_id,
                    expected_descriptor_sha256="a" * 64,
                )
                claim_results.put(("unexpected", None))
            except BaseException as exc:  # pragma: no cover - relayed to main thread
                claim_results.put(("error", exc))
            finally:
                claimant.close()

        claim_thread = threading.Thread(target=claim, daemon=True)
        claim_thread.start()
        kind, claim_pid = claim_results.get(timeout=5)
        assert kind == "pid"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                    (claim_pid,),
                )
                row = cur.fetchone()
            if row and row[0] == "Lock":
                break
            time.sleep(0.02)
        else:
            pytest.fail("discovery claim did not wait for same-version scan publication")

        scanner.commit()
        claim_thread.join(timeout=5)
        assert not claim_thread.is_alive()
        kind, value = claim_results.get(timeout=1)
        assert kind == "error"
        assert isinstance(value, store.InstallDiscoveryReleaseMismatchError)

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT config_jsonb ->> 'llm_provider_discovery_generation'
                  FROM extension_installs
                 WHERE account_id = %s AND install_id = %s
                """,
                (account_id, install_id),
            )
            assert cur.fetchone()[0] is None
    finally:
        scanner.rollback()
        if claim_thread is not None:
            claim_thread.join(timeout=5)
        scanner.close()
        conn.rollback()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                psycopg_runtime.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    psycopg_runtime.sql.Identifier(schema_name)
                )
            )
        conn.close()


@pytest.mark.integration
def test_same_release_descriptor_drift_after_claim_blocks_completion() -> None:
    dsn = str(resolve_test_db_dsn() or "").strip()
    if not dsn:
        pytest.skip("FS_DB_DSN is required for PostgreSQL descriptor CAS verification")

    account_id = str(uuid.uuid4())
    install_id = f"descriptor-completion-{uuid.uuid4().hex}"
    extension_id = f"fixture.descriptor-completion-{uuid.uuid4().hex}"
    release_id = "same-version-release"
    schema_name = f"llm_descriptor_completion_{uuid.uuid4().hex}"
    conn = psycopg_runtime.connect(dsn)
    scanner = psycopg_runtime.connect(dsn)
    try:
        _create_isolated_schema(conn, schema_name)
        _set_search_path(scanner, schema_name)
        with scanner.cursor() as cur:
            cur.execute(
                """
                INSERT INTO extension_releases (
                    release_id, extension_id, descriptor_jsonb, descriptor_sha256
                ) VALUES (
                    %s, %s,
                    jsonb_build_object('extension_id', %s::text, 'revision', 'old'),
                    %s
                )
                """,
                (release_id, extension_id, extension_id, "a" * 64),
            )
            cur.execute(
                """
                INSERT INTO extension_installs (
                    extension_id, account_id, install_id, release_id,
                    remote_base_url, config_jsonb
                ) VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    extension_id,
                    account_id,
                    install_id,
                    release_id,
                    "https://runtime.example.com",
                    '{"llm_provider_state_revision":"revision-1"}',
                ),
            )
        scanner.commit()

        snapshot = store.begin_install_discovery(
            conn,
            account_id=account_id,
            extension_install_id=install_id,
            generation="generation-1",
            expected_release_id=release_id,
            expected_descriptor_sha256="a" * 64,
        )
        assert snapshot is not None
        assert snapshot["descriptor_sha256"] == "a" * 64
        conn.commit()

        with scanner.cursor() as cur:
            cur.execute(
                """
                UPDATE extension_releases
                   SET descriptor_jsonb = jsonb_build_object(
                           'extension_id', extension_id, 'revision', 'new'
                       ),
                       descriptor_sha256 = %s
                 WHERE release_id = %s
                """,
                ("b" * 64, release_id),
            )
        scanner.commit()

        assert not store.lock_install_discovery_generation_current(
            conn,
            account_id=account_id,
            extension_install_id=install_id,
            generation="generation-1",
            state_revision="revision-1",
            release_id=release_id,
            descriptor_sha256="a" * 64,
        )
        conn.rollback()
    finally:
        conn.rollback()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                psycopg_runtime.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    psycopg_runtime.sql.Identifier(schema_name)
                )
            )
        conn.close()
        scanner.close()


@pytest.mark.integration
def test_scan_serializes_cross_account_late_join_and_rejects_stale_completion() -> None:
    dsn = str(resolve_test_db_dsn() or "").strip()
    if not dsn:
        pytest.skip("FS_DB_DSN is required for PostgreSQL membership-lock verification")

    first_account_id = str(uuid.uuid4())
    late_account_id = str(uuid.uuid4())
    first_install_id = f"membership-first-{uuid.uuid4().hex}"
    late_install_id = f"membership-late-{uuid.uuid4().hex}"
    extension_id = f"fixture.membership-{uuid.uuid4().hex}"
    release_id = f"release-membership-{uuid.uuid4().hex}"
    schema_name = f"llm_membership_{uuid.uuid4().hex}"
    setup = psycopg_runtime.connect(dsn)
    scanner = psycopg_runtime.connect(dsn)
    writer_results: queue.Queue[tuple[str, Any]] = queue.Queue()
    writer_thread: threading.Thread | None = None
    try:
        _create_isolated_schema(setup, schema_name)
        _set_search_path(scanner, schema_name)
        with setup.cursor() as cur:
            cur.execute(
                """
                INSERT INTO extension_releases (
                    release_id, extension_id, descriptor_jsonb, descriptor_sha256
                ) VALUES (
                    %s, %s,
                    jsonb_build_object('extension_id', %s::text, 'revision', 'old'),
                    %s
                )
                """,
                (release_id, extension_id, extension_id, "a" * 64),
            )
            cur.execute(
                """
                INSERT INTO extension_installs (
                    extension_id, account_id, install_id, release_id,
                    remote_base_url, config_jsonb
                ) VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    extension_id,
                    first_account_id,
                    first_install_id,
                    release_id,
                    "https://runtime.example.com",
                    '{"llm_provider_state_revision":"revision-1"}',
                ),
            )
        setup.commit()

        snapshot = store.begin_install_discovery(
            setup,
            account_id=first_account_id,
            extension_install_id=first_install_id,
            generation="generation-before-scan",
            expected_release_id=release_id,
            expected_descriptor_sha256="a" * 64,
        )
        assert snapshot is not None
        assert snapshot["descriptor_sha256"] == "a" * 64
        setup.commit()

        with scanner.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                ("fs.extension_release_membership", release_id),
            )
            cur.execute(
                """
                SELECT account_id, install_id
                  FROM extension_installs
                 WHERE release_id = %s
              ORDER BY account_id, install_id
                 FOR UPDATE
                """,
                (release_id,),
            )
            locked_members = cur.fetchall()
            assert [str(row[1]) for row in locked_members] == [first_install_id]
            cur.execute(
                """
                UPDATE extension_releases
                   SET descriptor_jsonb = jsonb_build_object(
                           'extension_id', extension_id, 'revision', 'new'
                       ),
                       descriptor_sha256 = %s
                 WHERE release_id = %s
                """,
                ("b" * 64, release_id),
            )

        def late_join() -> None:
            writer = psycopg_runtime.connect(dsn)
            try:
                _set_search_path(writer, schema_name)
                with writer.cursor() as cur:
                    cur.execute("SELECT pg_backend_pid()")
                    writer_results.put(("pid", int(cur.fetchone()[0])))
                DBExtensionInstallStore(writer).install(
                    extension_id=extension_id,
                    version="1.0.0",
                    account_id=late_account_id,
                    install_id=late_install_id,
                    release_id=release_id,
                )
                writer.commit()
                writer_results.put(("joined", None))
            except BaseException as exc:  # pragma: no cover - relayed to main thread
                writer_results.put(("error", exc))
            finally:
                writer.close()

        writer_thread = threading.Thread(target=late_join, daemon=True)
        writer_thread.start()
        kind, writer_pid = writer_results.get(timeout=5)
        assert kind == "pid"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with setup.cursor() as cur:
                cur.execute(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                    (writer_pid,),
                )
                row = cur.fetchone()
            if row and row[0] == "Lock":
                break
            time.sleep(0.02)
        else:
            pytest.fail("late join did not wait for the release membership scan")

        scanner.commit()
        writer_thread.join(timeout=5)
        assert not writer_thread.is_alive(), "late join deadlocked with the scanner"
        kind, value = writer_results.get(timeout=1)
        if kind == "error":
            assert isinstance(value, BaseException)
            raise value
        assert kind == "joined"

        assert not store.lock_install_discovery_generation_current(
            setup,
            account_id=first_account_id,
            extension_install_id=first_install_id,
            generation="generation-before-scan",
            state_revision="revision-1",
            release_id=release_id,
            descriptor_sha256="a" * 64,
        )
        setup.rollback()

        with setup.cursor() as cur:
            cur.execute(
                "SELECT install_id FROM extension_installs WHERE release_id = %s ORDER BY install_id",
                (release_id,),
            )
            assert [str(row[0]) for row in cur.fetchall()] == sorted(
                [first_install_id, late_install_id]
            )
            cur.execute(
                "SELECT descriptor_sha256 FROM extension_releases WHERE release_id = %s",
                (release_id,),
            )
            assert cur.fetchone()[0] == "b" * 64
    finally:
        scanner.rollback()
        if writer_thread is not None:
            writer_thread.join(timeout=5)
        scanner.close()
        setup.rollback()
        setup.autocommit = True
        with setup.cursor() as cur:
            cur.execute(
                psycopg_runtime.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    psycopg_runtime.sql.Identifier(schema_name)
                )
            )
        setup.close()
