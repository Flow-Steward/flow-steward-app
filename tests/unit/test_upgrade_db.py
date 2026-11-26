from __future__ import annotations

from pathlib import Path

import pytest

from core.application.tooling.db import upgrade_db

# Captured before the autouse fixture below replaces it for the sequence tests.
_real_prepared_module_sources = upgrade_db.prepared_module_migration_sources


@pytest.fixture(autouse=True)
def _existing_migration_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        upgrade_db.migration_runner,
        "migrations_table_exists",
        lambda _conn: True,
    )
    monkeypatch.setattr(
        upgrade_db,
        "enforce_canonical_baseline_contract_connection",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        upgrade_db.migration_runner,
        "applied_unknown_migrations",
        lambda _conn: [],
    )


@pytest.fixture(autouse=True)
def _no_preflight_or_phases(monkeypatch):
    """These tests pin the upgrade sequence; preflight and phases have their own tests."""
    monkeypatch.setattr(upgrade_db, "_refuse_blocked_preflight", lambda *_a, **_k: None)
    monkeypatch.setattr(upgrade_db.upgrade_phases, "run_pending_phases", lambda *_a, **_k: {})


@pytest.fixture(autouse=True)
def _no_platform_modules(monkeypatch):
    """No prepared platform module; module migrations have their own tests below."""
    monkeypatch.setattr(upgrade_db, "prepared_module_migration_sources", lambda: ())
    monkeypatch.setattr(
        upgrade_db.migration_runner,
        "apply_platform_module_migrations",
        lambda _conn, _modules, *, acquire_lock=True: {"modules": [], "orphaned_modules": []},
    )


@pytest.mark.parametrize("skip_backup", [False, True])
def test_upgrade_database_runs_upgrade_sequence(monkeypatch, skip_backup) -> None:
    calls: list[str] = []

    monkeypatch.setattr(upgrade_db, "_resolved_dsn", lambda _dsn=None: "postgresql://example")
    monkeypatch.setattr(
        upgrade_db.init_db, "validate_bootstrap_layout", lambda: calls.append("validate")
    )
    monkeypatch.setattr(
        upgrade_db.init_db,
        "init_platform_observability_schema",
        lambda: calls.append("platform"),
    )
    monkeypatch.setattr(
        upgrade_db.init_db,
        "apply_clean_bootstrap",
        lambda *, dsn=None, author="system", root=None: (
            calls.append(f"seed:{author}") or {"lookup": {"accounts": 1}}
        ),
    )
    monkeypatch.setattr(
        upgrade_db.init_db,
        "cleanup_retired_project_schema",
        lambda *, dsn=None: (
            calls.append("cleanup")
            or {
                "dropped_db_type": 1,
                "ensured_extension_install_runtime_status": 1,
                "ensured_project_connection_runtime_status": 1,
                "dropped_agent_types": 1,
            }
        ),
    )
    monkeypatch.setattr(
        upgrade_db.init_db, "init_extension_catalog", lambda: calls.append("extensions")
    )
    monkeypatch.setattr(
        upgrade_db.init_db,
        "init_workflow_knowledge_graph",
        lambda *, dsn=None: (
            calls.append("workflow-knowledge")
            or {
                "graph_name": "platform_workflow_graph",
                "available": True,
                "labels_ready": True,
            }
        ),
    )
    monkeypatch.setattr(
        upgrade_db.init_db, "init_langgraph_schema", lambda: calls.append("langgraph")
    )

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def fetchone(self):

            return (True,)

        def execute(self, sql, _params=None) -> None:
            if "advisory_lock(" in sql and "unlock" not in sql:
                calls.append("lock")
            elif "pg_advisory_unlock" in sql:
                calls.append("unlock")

    class _FakeConnection:
        autocommit = False

        def cursor(self):
            return _FakeCursor()

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(upgrade_db.psycopg_runtime, "connect", lambda _dsn: _FakeConnection())
    monkeypatch.setattr(
        upgrade_db.vector_lifecycle,
        "require_pgvector_image",
        lambda _conn: calls.append("pgvector-image") or {"compatible": True},
    )
    monkeypatch.setattr(
        upgrade_db.migration_runner,
        "inspect_migrations",
        lambda _conn: calls.append("inspect-migrations") or {"pending": [], "drifted": []},
    )
    monkeypatch.setattr(
        upgrade_db,
        "inspect_canonical_schema_state",
        lambda _conn: (
            calls.append("inspect-state")
            or {
                "schema_needs_reconcile": True,
                "seed_needs_upgrade": True,
                "needs_upgrade": True,
            }
        ),
    )
    monkeypatch.setattr(
        upgrade_db,
        "reconcile_canonical_schema",
        lambda **_kwargs: calls.append("reconcile"),
        raising=False,
    )
    monkeypatch.setattr(
        upgrade_db.migration_runner,
        "apply_pending_migrations",
        lambda _conn, *, root=None, acquire_lock=True: (
            calls.append(f"migrations:lock={acquire_lock}")
            or {"total": 2, "applied": ["0001.sql"], "skipped": ["0000.sql"]}
        ),
    )
    monkeypatch.setattr(
        upgrade_db.vector_lifecycle,
        "inspect_vector_health",
        lambda _conn, *, deep=False: calls.append(f"health:deep={deep}") or {"status": "healthy"},
    )
    monkeypatch.setattr(
        upgrade_db,
        "record_canonical_schema_state",
        lambda conn, *, applied_by="system", metadata=None: (
            calls.append(f"state:{applied_by}") or {"schema_fingerprint": "abc123"}
        ),
    )
    monkeypatch.setattr(
        upgrade_db.vector_lifecycle,
        "capture_protected_data_snapshot",
        lambda _conn: {"content": {"rows": 42}},
    )
    summary = upgrade_db.upgrade_database(
        author="release-bot", maintenance_confirmed=skip_backup, skip_db_backup=skip_backup
    )
    assert summary["protected_data_before"] == ({"content": {"rows": 42}} if skip_backup else {})
    assert summary["protected_data_after"] == summary["protected_data_before"]

    assert calls == [
        "validate",
        "lock",
        "pgvector-image",
        "migrations:lock=False",
        "platform",
        "cleanup",
        "seed:release-bot",
        "workflow-knowledge",
        "extensions",
        "langgraph",
        "health:deep=True",
        "state:release-bot",
        "unlock",
        "close",
    ]
    assert summary["schema_state"]["updated"] == 1
    assert summary["schema_state"]["fingerprint"] == "abc123"
    assert summary["schema_migrations"] == {
        "total": 2,
        "applied": ["0001.sql"],
        "skipped": ["0000.sql"],
        "repaired_user_memberships": 0,
        "upgrade_phases": {},
    }
    assert summary["schema_reconcile"] == {
        "files": 0,
        "statements": 0,
        "applied": 0,
        "skipped": 0,
        "failed": [],
    }
    assert summary["schema_cleanup"] == {
        "dropped_db_type": 1,
        "ensured_extension_install_runtime_status": 1,
        "ensured_project_connection_runtime_status": 1,
        "dropped_agent_types": 1,
    }
    assert summary["workflow_knowledge_graph"] == {
        "graph_name": "platform_workflow_graph",
        "available": True,
        "labels_ready": True,
    }


def test_upgrade_missing_tracking_table_verifies_baseline_then_applies_post_baseline_sql(
    monkeypatch,
) -> None:
    calls: list[str] = []

    class _Cursor:
        def fetchone(self):
            return (True,)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, *_args, **_kwargs) -> None:
            return None

    class _Connection:
        autocommit = False

        def cursor(self):
            return _Cursor()

        def close(self) -> None:
            return None

    monkeypatch.setattr(upgrade_db, "_resolved_dsn", lambda _dsn=None: "postgresql://example")
    monkeypatch.setattr(upgrade_db.init_db, "validate_bootstrap_layout", lambda: None)
    monkeypatch.setattr(upgrade_db.psycopg_runtime, "connect", lambda _dsn: _Connection())
    monkeypatch.setattr(upgrade_db.vector_lifecycle, "require_pgvector_image", lambda _conn: {})
    monkeypatch.setattr(
        upgrade_db.migration_runner,
        "migrations_table_exists",
        lambda _conn: False,
    )
    monkeypatch.setattr(
        upgrade_db,
        "enforce_canonical_baseline_contract_connection",
        lambda _conn, *, component, include_data_invariants=True: calls.append(
            f"verify:{component}:data={include_data_invariants}"
        ),
    )
    monkeypatch.setattr(
        upgrade_db,
        "_repair_missing_user_memberships",
        lambda _conn: calls.append("repair-memberships") or 1,
    )
    monkeypatch.setattr(
        upgrade_db.migration_runner,
        "inspect_migrations",
        lambda _conn: {"pending": [], "drifted": []},
    )
    monkeypatch.setattr(
        upgrade_db.migration_runner,
        "apply_pending_migrations",
        lambda _conn, *, root=None, acquire_lock=True: (
            calls.append(f"migrations:lock={acquire_lock}")
            or {
                "total": 1,
                "applied": ["three.sql"],
                "skipped": [],
            }
        ),
    )
    monkeypatch.setattr(upgrade_db.init_db, "init_platform_observability_schema", lambda: None)
    monkeypatch.setattr(
        upgrade_db.init_db,
        "cleanup_retired_project_schema",
        lambda **_: calls.append("cleanup") or {},
    )
    monkeypatch.setattr(
        upgrade_db.init_db,
        "apply_clean_bootstrap",
        lambda **_: calls.append("seed") or {},
    )
    monkeypatch.setattr(
        upgrade_db.init_db,
        "init_workflow_knowledge_graph",
        lambda *, dsn=None: calls.append("workflow-knowledge") or {},
    )
    monkeypatch.setattr(upgrade_db.init_db, "init_extension_catalog", lambda: None)
    monkeypatch.setattr(upgrade_db.init_db, "init_langgraph_schema", lambda: None)
    monkeypatch.setattr(
        upgrade_db.vector_lifecycle,
        "inspect_vector_health",
        lambda *_args, **_kwargs: {"status": "healthy"},
    )
    monkeypatch.setattr(
        upgrade_db,
        "record_canonical_schema_state",
        lambda *_args, **_kwargs: {"schema_fingerprint": "verified"},
    )

    summary = upgrade_db.upgrade_database(author="recovery")

    assert calls == [
        "verify:database upgrade baseline:data=False",
        "migrations:lock=False",
        "cleanup",
        "seed",
        "repair-memberships",
        "verify:database upgrade baseline:data=True",
        "workflow-knowledge",
    ]
    assert summary["schema_migrations"]["stamped"] == []
    assert summary["schema_migrations"]["applied"] == ["three.sql"]
    assert summary["schema_migrations"]["pending"] == []
    assert summary["schema_migrations"]["repaired_user_memberships"] == 1


def test_membership_recovery_is_insert_only_and_idempotent() -> None:
    statements: list[str] = []

    class _Cursor:
        def fetchone(self):
            return (True,)

        rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql: str) -> None:
            statements.append(" ".join(sql.split()))

    class _Connection:
        def cursor(self):
            return _Cursor()

    assert upgrade_db._repair_missing_user_memberships(_Connection()) == 1
    assert len(statements) == 1
    assert statements[0].startswith("INSERT INTO public.user_account_memberships")
    assert "WHERE NOT EXISTS" in statements[0]
    assert "ON CONFLICT (user_id, account_id) DO NOTHING" in statements[0]
    assert "DELETE" not in statements[0]
    assert "UPDATE" not in statements[0]


def test_upgrade_database_stops_before_reconciliation_when_migration_fails(
    monkeypatch,
) -> None:
    calls: list[str] = []

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def fetchone(self):

            return (True,)

        def execute(self, sql, _params=None) -> None:
            if "advisory_lock(" in sql and "unlock" not in sql:
                calls.append("lock")
            elif "pg_advisory_unlock" in sql:
                calls.append("unlock")

    class _Connection:
        autocommit = False

        def cursor(self):
            return _Cursor()

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(upgrade_db, "_resolved_dsn", lambda _dsn=None: "postgresql://example")
    monkeypatch.setattr(upgrade_db.init_db, "validate_bootstrap_layout", lambda: None)
    monkeypatch.setattr(upgrade_db.psycopg_runtime, "connect", lambda _dsn: _Connection())
    monkeypatch.setattr(
        upgrade_db.vector_lifecycle,
        "require_pgvector_image",
        lambda _conn: {"compatible": True},
    )
    monkeypatch.setattr(
        upgrade_db.migration_runner,
        "inspect_migrations",
        lambda _conn: {"pending": ["cutover.sql"], "drifted": []},
    )
    monkeypatch.setattr(
        upgrade_db.migration_runner,
        "apply_pending_migrations",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("migration failed")),
    )
    monkeypatch.setattr(
        upgrade_db,
        "inspect_canonical_schema_state",
        lambda _conn: calls.append("inspect-state"),
    )
    monkeypatch.setattr(
        upgrade_db,
        "reconcile_canonical_schema",
        lambda **_kwargs: calls.append("reconcile"),
        raising=False,
    )
    monkeypatch.setattr(
        upgrade_db.init_db,
        "cleanup_retired_project_schema",
        lambda **_kwargs: calls.append("cleanup"),
    )
    monkeypatch.setattr(
        upgrade_db.init_db,
        "apply_clean_bootstrap",
        lambda **_kwargs: calls.append("seed"),
    )

    with pytest.raises(RuntimeError, match="migration failed"):
        upgrade_db.upgrade_database()

    assert calls == ["lock", "unlock", "close"]


def test_upgrade_database_never_replays_canonical_baseline_schema() -> None:
    assert "reconcile_canonical_schema" not in upgrade_db.upgrade_database.__code__.co_names


def test_upgrade_database_aligns_runtime_status_before_seed(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(upgrade_db, "_resolved_dsn", lambda _dsn=None: "postgresql://example")
    monkeypatch.setattr(upgrade_db.init_db, "validate_bootstrap_layout", lambda: None)
    monkeypatch.setattr(upgrade_db.init_db, "init_platform_observability_schema", lambda: None)

    def cleanup(*, dsn=None):
        assert dsn == "postgresql://example"
        calls.append("cleanup")
        return {
            "ensured_extension_install_runtime_status": 1,
            "ensured_project_connection_runtime_status": 1,
            "dropped_agent_types": 1,
        }

    def seed(*, dsn=None, author="system", root=None):
        assert calls == ["cleanup"]
        calls.append("seed")
        return {"lookup": {"accounts": 1}}

    monkeypatch.setattr(upgrade_db.init_db, "cleanup_retired_project_schema", cleanup)
    monkeypatch.setattr(upgrade_db.init_db, "apply_clean_bootstrap", seed)
    monkeypatch.setattr(
        upgrade_db.init_db,
        "init_workflow_knowledge_graph",
        lambda *, dsn=None: calls.append("workflow-knowledge") or {},
    )
    monkeypatch.setattr(upgrade_db.init_db, "init_extension_catalog", lambda: None)
    monkeypatch.setattr(upgrade_db.init_db, "init_langgraph_schema", lambda: None)

    class _FakeCursor:
        def fetchone(self):
            return (True,)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, *_args, **_kwargs) -> None:
            return None

    class _FakeConnection:
        autocommit = False

        def cursor(self):
            return _FakeCursor()

        def close(self) -> None:
            pass

    monkeypatch.setattr(upgrade_db.psycopg_runtime, "connect", lambda _dsn: _FakeConnection())
    monkeypatch.setattr(
        upgrade_db.vector_lifecycle,
        "require_pgvector_image",
        lambda _conn: {"compatible": True},
    )
    monkeypatch.setattr(
        upgrade_db.migration_runner,
        "inspect_migrations",
        lambda _conn: {"pending": [], "drifted": []},
    )
    monkeypatch.setattr(
        upgrade_db,
        "inspect_canonical_schema_state",
        lambda _conn: {
            "schema_needs_reconcile": False,
            "seed_needs_upgrade": True,
            "needs_upgrade": True,
        },
    )
    monkeypatch.setattr(
        upgrade_db.migration_runner,
        "apply_pending_migrations",
        lambda _conn, *, root=None, acquire_lock=True: {
            "total": 0,
            "applied": [],
            "skipped": [],
        },
    )
    monkeypatch.setattr(
        upgrade_db.vector_lifecycle,
        "inspect_vector_health",
        lambda _conn, *, deep=False: {"status": "healthy"},
    )
    monkeypatch.setattr(
        upgrade_db,
        "record_canonical_schema_state",
        lambda conn, *, applied_by="system", metadata=None: {"schema_fingerprint": "abc123"},
    )
    summary = upgrade_db.upgrade_database(author="release-bot")

    assert calls == ["cleanup", "seed", "workflow-knowledge"]
    assert summary["schema_cleanup"] == {
        "ensured_extension_install_runtime_status": 1,
        "ensured_project_connection_runtime_status": 1,
        "dropped_agent_types": 1,
    }


def test_inspect_database_upgrade_flags_pending_migrations(monkeypatch) -> None:
    """A frozen baseline fingerprint must not hide unapplied post-1.0 migrations."""

    class _FakeConnection:
        def close(self) -> None:
            pass

    monkeypatch.setattr(upgrade_db, "_resolved_dsn", lambda _dsn=None: "postgresql://example")
    monkeypatch.setattr(upgrade_db.psycopg_runtime, "connect", lambda _dsn: _FakeConnection())
    monkeypatch.setattr(
        upgrade_db,
        "inspect_canonical_schema_state",
        lambda conn: {
            "current": {"schema_fingerprint": "frozen"},
            "expected_fingerprint": "frozen",
            "needs_upgrade": False,
        },
    )
    monkeypatch.setattr(
        upgrade_db.migration_runner,
        "inspect_migrations",
        lambda conn: {"pending": ["20260101000000_add_index.sql"], "drifted": []},
    )
    status = upgrade_db.inspect_database_upgrade()

    assert status["needs_upgrade"] is True
    assert status["pending_migrations"] == ["20260101000000_add_index.sql"]
    assert status["drifted_migrations"] == []


def test_inspect_database_upgrade_flags_checksum_drift(monkeypatch) -> None:
    """Drift of an already-applied migration must not read as compatible."""

    class _FakeConnection:
        def close(self) -> None:
            pass

    monkeypatch.setattr(upgrade_db, "_resolved_dsn", lambda _dsn=None: "postgresql://example")
    monkeypatch.setattr(upgrade_db.psycopg_runtime, "connect", lambda _dsn: _FakeConnection())
    monkeypatch.setattr(
        upgrade_db,
        "inspect_canonical_schema_state",
        lambda conn: {
            "current": {"schema_fingerprint": "frozen"},
            "expected_fingerprint": "frozen",
            "needs_upgrade": False,
        },
    )
    monkeypatch.setattr(
        upgrade_db.migration_runner,
        "inspect_migrations",
        lambda conn: {"pending": [], "drifted": ["20260101000000_edited.sql"]},
    )
    status = upgrade_db.inspect_database_upgrade()

    assert status["needs_upgrade"] is True
    assert status["drifted_migrations"] == ["20260101000000_edited.sql"]
    assert status["pending_migrations"] == []


def test_inspect_database_upgrade_compatible_when_no_pending_migrations(monkeypatch) -> None:
    class _FakeConnection:
        def close(self) -> None:
            pass

    monkeypatch.setattr(upgrade_db, "_resolved_dsn", lambda _dsn=None: "postgresql://example")
    monkeypatch.setattr(upgrade_db.psycopg_runtime, "connect", lambda _dsn: _FakeConnection())
    monkeypatch.setattr(
        upgrade_db,
        "inspect_canonical_schema_state",
        lambda conn: {
            "current": {"schema_fingerprint": "frozen"},
            "expected_fingerprint": "frozen",
            "needs_upgrade": False,
        },
    )
    monkeypatch.setattr(
        upgrade_db.migration_runner,
        "inspect_migrations",
        lambda conn: {"pending": [], "drifted": []},
    )
    status = upgrade_db.inspect_database_upgrade()

    assert status["needs_upgrade"] is False
    assert status["pending_migrations"] == []
    assert status["drifted_migrations"] == []
    assert status["pre_release_migrations"] == []


def test_inspect_database_upgrade_requires_dsn(monkeypatch) -> None:
    monkeypatch.setattr(
        upgrade_db,
        "_resolved_dsn",
        lambda _dsn=None: (_ for _ in ()).throw(
            RuntimeError("No Postgres DSN configured for database upgrade")
        ),
    )

    with pytest.raises(RuntimeError, match="No Postgres DSN configured"):
        upgrade_db.inspect_database_upgrade()


# A pre-1.0 ledger as a pre-release development database records it: the deleted
# historical chain, versioned before the 1.0 baseline cutoff.
_PRE_RELEASE_LEDGER = [
    "20260705110000_connection_scoped_sessions.sql",
    "20260705123000_connection_oauth_flow.sql",
    "20260706193000_backfill_registry_system_account_ids.sql",
    "20260907110000_extension_invocation_trace_context.sql",
]


class _LockRecordingConnection:
    autocommit = False

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def cursor(self):
        calls = self._calls

        class _Cursor:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def fetchone(self):

                return (True,)

            def execute(self, sql, _params=None) -> None:
                if "advisory_lock(" in sql and "unlock" not in sql:
                    calls.append("lock")
                elif "pg_advisory_unlock" in sql:
                    calls.append("unlock")

        return _Cursor()

    def close(self) -> None:
        self._calls.append("close")


def _pre_release_database(monkeypatch, calls: list[str]) -> None:
    monkeypatch.setattr(upgrade_db, "_resolved_dsn", lambda _dsn=None: "postgresql://example")
    monkeypatch.setattr(upgrade_db.init_db, "validate_bootstrap_layout", lambda: None)
    monkeypatch.setattr(
        upgrade_db.psycopg_runtime, "connect", lambda _dsn: _LockRecordingConnection(calls)
    )
    monkeypatch.setattr(
        upgrade_db.migration_runner,
        "applied_unknown_migrations",
        lambda _conn: list(_PRE_RELEASE_LEDGER),
    )
    for name in ("inspect_migrations", "apply_pending_migrations"):
        monkeypatch.setattr(
            upgrade_db.migration_runner,
            name,
            lambda *_args, _name=name, **_kwargs: calls.append(_name),
        )


def _assert_pre_release_refusal(message: str) -> None:
    assert "\n" not in message  # the installer reports only the refusal's last line
    assert "pre-release (pre-1.0) Flow Steward database" in message
    assert "20260705110000_connection_scoped_sessions.sql" in message
    assert "and 1 more" in message
    assert "cannot upgrade a pre-release database" in message
    assert "Recreate it" in message
    assert "./install.sh" in message
    assert "database status" in message
    assert "canonical schema and seed" in message
    assert "Compact and Scalable" in message
    assert "docs/GUIDE.md" in message
    assert "database bootstrap-seed" not in message  # unsupported by the Compact launcher
    assert "Pre-1.0 development databases" not in message  # absent from README
    # Not the rollback diagnosis: no newer release exists to go back to.
    assert "this release does not know" not in message
    assert "newer release" not in message


def test_pre_release_migrations_are_only_rows_versioned_before_the_baseline() -> None:
    post_release = "20261201000000_add_index.sql"

    assert upgrade_db.pre_release_migrations(
        [post_release, *reversed(_PRE_RELEASE_LEDGER), "not-a-migration.sql"]
    ) == sorted(_PRE_RELEASE_LEDGER)
    assert upgrade_db.pre_release_migrations([post_release]) == []


def test_every_shipped_migration_postdates_the_pre_release_cutoff() -> None:
    root = Path(__file__).resolve().parents[2]
    shipped = [path.name for path in upgrade_db.migration_runner.discover_migration_files(root)]

    assert upgrade_db.pre_release_migrations(shipped) == []


def test_installer_baseline_refuses_a_pre_release_database_with_recreate_guidance(
    monkeypatch,
) -> None:
    calls: list[str] = []
    _pre_release_database(monkeypatch, calls)

    with pytest.raises(RuntimeError) as raised:
        upgrade_db.baseline_migration_history()

    _assert_pre_release_refusal(str(raised.value))
    assert calls == ["lock", "unlock", "close"]  # refused before the ledger is compared


def test_upgrade_database_refuses_a_pre_release_database_before_any_mutation(
    monkeypatch,
) -> None:
    calls: list[str] = []
    _pre_release_database(monkeypatch, calls)
    monkeypatch.setattr(
        upgrade_db.vector_lifecycle,
        "require_pgvector_image",
        lambda _conn: {"compatible": True},
    )
    for name in (
        "init_platform_observability_schema",
        "cleanup_retired_project_schema",
        "apply_clean_bootstrap",
    ):
        monkeypatch.setattr(
            upgrade_db.init_db,
            name,
            lambda *_args, _name=name, **_kwargs: calls.append(_name),
        )

    with pytest.raises(RuntimeError) as raised:
        upgrade_db.upgrade_database(maintenance_confirmed=True)

    _assert_pre_release_refusal(str(raised.value))
    assert calls == ["lock", "unlock", "close"]


def test_inspect_database_upgrade_flags_a_pre_release_ledger(monkeypatch) -> None:
    class _FakeConnection:
        def close(self) -> None:
            pass

    monkeypatch.setattr(upgrade_db, "_resolved_dsn", lambda _dsn=None: "postgresql://example")
    monkeypatch.setattr(upgrade_db.psycopg_runtime, "connect", lambda _dsn: _FakeConnection())
    monkeypatch.setattr(
        upgrade_db,
        "inspect_canonical_schema_state",
        lambda conn: {
            "current": {"schema_fingerprint": "frozen"},
            "expected_fingerprint": "frozen",
            "needs_upgrade": False,
        },
    )
    monkeypatch.setattr(
        upgrade_db.migration_runner,
        "inspect_migrations",
        lambda conn: {"pending": [], "drifted": []},
    )
    monkeypatch.setattr(
        upgrade_db.migration_runner,
        "applied_unknown_migrations",
        lambda _conn: list(_PRE_RELEASE_LEDGER),
    )

    status = upgrade_db.inspect_database_upgrade()

    assert status["needs_upgrade"] is True
    assert status["pre_release_migrations"] == sorted(_PRE_RELEASE_LEDGER)


def test_skip_backup_never_waives_writer_confirmation(monkeypatch):
    def connect(*args, **kwargs):
        raise AssertionError("must refuse before connecting")

    monkeypatch.setattr(upgrade_db.psycopg_runtime, "connect", connect)
    with pytest.raises(RuntimeError, match="writers must be stopped"):
        upgrade_db.upgrade_database(skip_db_backup=True)


# ---------------------------------------------------------------------------
# Platform-module migrations (ADR 0082)
# ---------------------------------------------------------------------------


def _stub_upgrade_sequence(monkeypatch, calls: list[str]) -> None:
    monkeypatch.setattr(upgrade_db, "_resolved_dsn", lambda _dsn=None: "postgresql://example")
    monkeypatch.setattr(upgrade_db.init_db, "validate_bootstrap_layout", lambda: None)
    monkeypatch.setattr(
        upgrade_db.psycopg_runtime, "connect", lambda _dsn: _LockRecordingConnection(calls)
    )
    monkeypatch.setattr(upgrade_db.vector_lifecycle, "require_pgvector_image", lambda _conn: {})
    monkeypatch.setattr(
        upgrade_db.migration_runner,
        "apply_pending_migrations",
        lambda _conn, *, root=None, acquire_lock=True: (
            calls.append(f"core-migrations:lock={acquire_lock}")
            or {"total": 0, "applied": [], "skipped": []}
        ),
    )
    monkeypatch.setattr(
        upgrade_db.init_db,
        "init_platform_observability_schema",
        lambda: calls.append("platform"),
    )
    monkeypatch.setattr(upgrade_db.init_db, "cleanup_retired_project_schema", lambda **_: {})
    monkeypatch.setattr(upgrade_db.init_db, "apply_clean_bootstrap", lambda **_: {})
    monkeypatch.setattr(upgrade_db.init_db, "init_workflow_knowledge_graph", lambda **_: {})
    monkeypatch.setattr(upgrade_db.init_db, "init_extension_catalog", lambda: None)
    monkeypatch.setattr(upgrade_db.init_db, "init_langgraph_schema", lambda: None)
    monkeypatch.setattr(upgrade_db, "_repair_missing_user_memberships", lambda _conn: 0)
    monkeypatch.setattr(
        upgrade_db.vector_lifecycle,
        "inspect_vector_health",
        lambda *_args, **_kwargs: {"status": "healthy"},
    )
    monkeypatch.setattr(
        upgrade_db,
        "record_canonical_schema_state",
        lambda *_args, **_kwargs: {"schema_fingerprint": "abc"},
    )


def test_upgrade_applies_module_migrations_after_core_under_the_same_lock(monkeypatch) -> None:
    calls: list[str] = []
    _stub_upgrade_sequence(monkeypatch, calls)
    source = upgrade_db.migration_runner.ModuleMigrationSource(
        "acme.billing", "1.2.0", Path("/modules/acme-billing/db/migrations")
    )
    received: list[tuple] = []
    module_result = {
        "modules": [{"module_id": "acme.billing", "applied": ["20261201000000_init.sql"]}],
        "orphaned_modules": [],
    }

    def apply_modules(_conn, modules, *, acquire_lock=True):
        received.append((tuple(modules), acquire_lock))
        calls.append("module-migrations")
        return module_result

    monkeypatch.setattr(
        upgrade_db.migration_runner, "apply_platform_module_migrations", apply_modules
    )

    summary = upgrade_db.upgrade_database(module_sources=lambda: [source])

    assert received == [((source,), False)]
    assert calls[:4] == ["lock", "core-migrations:lock=False", "module-migrations", "platform"]
    assert calls[-2:] == ["unlock", "close"]
    assert summary["platform_module_migrations"] == module_result
    assert "platform_module_migrations" not in summary["schema_migrations"]


def test_upgrade_uses_prepared_modules_when_no_provider_is_given(monkeypatch) -> None:
    calls: list[str] = []
    _stub_upgrade_sequence(monkeypatch, calls)
    prepared = (("acme.billing", "2.0.0", Path("/modules/acme/db/migrations")),)
    monkeypatch.setattr(upgrade_db, "prepared_module_migration_sources", lambda: prepared)
    received: list[tuple] = []
    monkeypatch.setattr(
        upgrade_db.migration_runner,
        "apply_platform_module_migrations",
        lambda _conn, modules, *, acquire_lock=True: (
            received.append(tuple(modules)) or {"modules": [], "orphaned_modules": []}
        ),
    )

    upgrade_db.upgrade_database()

    assert received == [prepared]


def test_prepared_module_sources_map_module_directories_without_touching_sys_path(
    monkeypatch, tmp_path
) -> None:
    import sys
    import types

    seen: dict[str, object] = {}

    def activate(role, *, root=None, sys_path=None, **_kwargs):
        seen["role"] = role
        seen["sys_path"] = sys_path
        return (
            types.SimpleNamespace(
                module_id="acme.billing",
                version="1.2.0",
                import_name="acme_billing",
                path=tmp_path / "acme-billing",
            ),
        )

    fake = types.ModuleType("core.infrastructure.platform_modules.loader")
    fake.activate_prepared_platform_modules = activate
    monkeypatch.setitem(sys.modules, "core.infrastructure.platform_modules.loader", fake)

    result = _real_prepared_module_sources()

    assert seen["role"] == "control"
    assert seen["sys_path"] == list(sys.path)
    assert seen["sys_path"] is not sys.path
    assert result == (
        upgrade_db.migration_runner.ModuleMigrationSource(
            module_id="acme.billing",
            module_version="1.2.0",
            migrations_dir=tmp_path / "acme-billing" / "db" / "migrations",
        ),
    )


def test_prepared_module_sources_are_empty_without_platform_module_support(monkeypatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "core.infrastructure.platform_modules.loader", None)

    assert _real_prepared_module_sources() == ()


def _inspection_without_core_changes(monkeypatch) -> None:
    class _FakeConnection:
        def close(self) -> None:
            pass

    monkeypatch.setattr(upgrade_db, "_resolved_dsn", lambda _dsn=None: "postgresql://example")
    monkeypatch.setattr(upgrade_db.psycopg_runtime, "connect", lambda _dsn: _FakeConnection())
    monkeypatch.setattr(
        upgrade_db,
        "inspect_canonical_schema_state",
        lambda conn: {"needs_upgrade": False},
    )
    monkeypatch.setattr(
        upgrade_db.migration_runner,
        "inspect_migrations",
        lambda conn: {"pending": [], "drifted": []},
    )


def test_inspection_needs_an_upgrade_for_a_module_only_change(monkeypatch) -> None:
    _inspection_without_core_changes(monkeypatch)
    source = ("acme.billing", "1.1.0", Path("/modules/acme/db/migrations"))
    report = {
        "acme.billing": {
            "pending": ["20261215000000_currency.sql"],
            "drifted": [],
            "unknown": [],
        }
    }
    received: list[tuple] = []
    monkeypatch.setattr(
        upgrade_db.migration_runner,
        "inspect_module_migrations",
        lambda _conn, modules: received.append(tuple(modules)) or report,
    )

    status = upgrade_db.inspect_database_upgrade(module_sources=lambda: [source])

    assert received == [(source,)]
    assert status["needs_upgrade"] is True
    assert status["pending_migrations"] == []
    assert status["module_migrations"] == report


def test_inspection_of_up_to_date_modules_needs_no_upgrade(monkeypatch) -> None:
    _inspection_without_core_changes(monkeypatch)
    report = {"acme.billing": {"pending": [], "drifted": [], "unknown": []}}
    monkeypatch.setattr(
        upgrade_db.migration_runner, "inspect_module_migrations", lambda _conn, _m: report
    )

    status = upgrade_db.inspect_database_upgrade(
        module_sources=lambda: [("acme.billing", "1.0.0", Path("/m"))]
    )

    assert status["needs_upgrade"] is False


def test_inspection_without_modules_keeps_the_core_report(monkeypatch) -> None:
    _inspection_without_core_changes(monkeypatch)

    status = upgrade_db.inspect_database_upgrade()

    assert "module_migrations" not in status
    assert status["needs_upgrade"] is False


def _baseline_database(monkeypatch, calls: list[str], *, ledger_exists: bool) -> None:
    monkeypatch.setattr(upgrade_db, "_resolved_dsn", lambda _dsn=None: "postgresql://example")
    monkeypatch.setattr(upgrade_db.init_db, "validate_bootstrap_layout", lambda: None)
    monkeypatch.setattr(upgrade_db.upgrade_preflight, "validate_release_artifacts", lambda: None)
    monkeypatch.setattr(
        upgrade_db.psycopg_runtime, "connect", lambda _dsn: _LockRecordingConnection(calls)
    )
    monkeypatch.setattr(
        upgrade_db.migration_runner, "migrations_table_exists", lambda _conn: ledger_exists
    )


def test_fresh_install_runs_prepared_module_migrations_after_core(monkeypatch) -> None:
    calls: list[str] = []
    _baseline_database(monkeypatch, calls, ledger_exists=False)
    monkeypatch.setattr(
        upgrade_db,
        "_stamp_verified_canonical_baseline",
        lambda _conn, *, component: calls.append("core-baseline") or {"applied": []},
    )
    source = ("acme.billing", "1.0.0", Path("/modules/acme/db/migrations"))
    module_result = {"modules": [{"module_id": "acme.billing"}], "orphaned_modules": []}

    def apply_modules(_conn, modules, *, acquire_lock=True):
        calls.append(f"module-migrations:{tuple(modules) == (source,)}:lock={acquire_lock}")
        return module_result

    monkeypatch.setattr(
        upgrade_db.migration_runner, "apply_platform_module_migrations", apply_modules
    )

    summary = upgrade_db.baseline_migration_history(module_sources=lambda: [source])

    assert calls == [
        "lock",
        "core-baseline",
        "module-migrations:True:lock=False",
        "unlock",
        "close",
    ]
    assert summary["platform_module_migrations"] == module_result


def test_fresh_install_without_modules_keeps_the_core_summary(monkeypatch) -> None:
    calls: list[str] = []
    _baseline_database(monkeypatch, calls, ledger_exists=False)
    monkeypatch.setattr(
        upgrade_db,
        "_stamp_verified_canonical_baseline",
        lambda _conn, *, component: {"applied": []},
    )

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("no prepared module, nothing to apply")

    monkeypatch.setattr(
        upgrade_db.migration_runner, "apply_platform_module_migrations", must_not_run
    )

    assert upgrade_db.baseline_migration_history() == {"applied": []}


def test_existing_ledger_refuses_a_baseline_with_pending_module_migrations(monkeypatch) -> None:
    calls: list[str] = []
    _baseline_database(monkeypatch, calls, ledger_exists=True)
    monkeypatch.setattr(
        upgrade_db.migration_runner,
        "inspect_migrations",
        lambda _conn: {"pending": [], "drifted": []},
    )
    monkeypatch.setattr(upgrade_db.upgrade_phases, "unknown_recorded_phases", lambda _c: [])
    monkeypatch.setattr(
        upgrade_db.upgrade_phases,
        "recorded_phases",
        lambda _c: {phase.phase_id for phase in upgrade_db.upgrade_phases.UPGRADE_PHASES},
    )
    monkeypatch.setattr(
        upgrade_db.migration_runner,
        "inspect_module_migrations",
        lambda _conn, _modules: {
            "acme.billing": {
                "pending": ["20261215000000_currency.sql"],
                "drifted": [],
                "unknown": [],
            }
        },
    )

    with pytest.raises(RuntimeError) as raised:
        upgrade_db.baseline_migration_history(
            module_sources=lambda: [("acme.billing", "1.1.0", Path("/m"))]
        )

    assert "database upgrade" in str(raised.value)
    assert "acme.billing: 20261215000000_currency.sql" in str(raised.value)
