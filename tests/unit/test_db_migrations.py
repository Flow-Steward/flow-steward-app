"""Unit tests for the post-1.0 SQL migration runner (db/migrations/)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.application.tooling.db import generate_migration, migration_runner

# ---------------------------------------------------------------------------
# Fake DB connection
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn
        self._result: list | None = None

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def execute(self, sql: str, params=None) -> None:
        conn = self._conn
        conn.executed.append((sql, params))
        if "pg_advisory_lock" in sql:
            conn.locks.append(params[0])
            return
        if "pg_advisory_unlock" in sql:
            conn.unlocks.append(params[0])
            return
        if "CREATE TABLE IF NOT EXISTS public.platform_schema_migrations" in sql:
            conn.table_created = True
            return
        if "to_regclass('public.platform_schema_migrations')" in sql:
            self._result = [(conn.table_created or bool(conn.committed),)]
            return
        if sql.strip().upper().startswith("SELECT MIGRATION_ID"):
            self._result = [(mid, chk) for mid, chk in conn.committed.items()]
            return
        if "INSERT INTO public.platform_schema_migrations" in sql:
            migration_id, checksum = params
            if conn.autocommit:
                conn.committed[migration_id] = checksum
            else:
                conn.staged[migration_id] = checksum
            return
        if "UPDATE public.platform_schema_migrations" in sql:
            checksum, migration_id = params
            if conn.fail_finalize:
                raise KeyboardInterrupt("simulated crash after non-transactional SQL")
            conn.committed[migration_id] = checksum
            return
        if "search_path" in sql:
            # Session state the runner sets around a body, not a migration body.
            conn.search_path.append(sql.strip())
            return
        # Arbitrary migration body SQL.
        conn.bodies.append((sql, conn.autocommit))
        if conn.fail_on is not None and conn.fail_on in sql:
            raise RuntimeError("boom: migration body failed")

    def fetchall(self):
        return self._result

    def fetchone(self):
        rows = self._result or []
        return rows[0] if rows else None


class _FakeConn:
    def __init__(
        self,
        *,
        applied: dict | None = None,
        fail_on: str | None = None,
        fail_finalize: bool = False,
    ) -> None:
        self.autocommit = False
        self.committed: dict[str, str] = dict(applied or {})
        self.staged: dict[str, str] = {}
        self.executed: list = []
        self.bodies: list = []
        self.search_path: list[str] = []
        self.locks: list = []
        self.unlocks: list = []
        self.table_created = False
        self.fail_on = fail_on
        self.fail_finalize = fail_finalize
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1
        self.committed.update(self.staged)
        self.staged = {}

    def rollback(self) -> None:
        self.rollbacks += 1
        self.staged = {}


def _write_migration(root: Path, filename: str, body: str) -> Path:
    directory = root / "db" / "migrations"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(body, encoding="utf-8")
    return path


_REAL_SQL = "-- transaction: true\nCREATE TABLE public.example (id int);\n"


# ---------------------------------------------------------------------------
# Filename generation
# ---------------------------------------------------------------------------


def test_generate_migration_filename_uses_timestamp_and_slug():
    moment = datetime(2026, 6, 29, 13, 45, 7, tzinfo=UTC)
    filename = migration_runner.generate_migration_filename("add_status_index", now=moment)
    assert filename == "20260629134507_add_status_index.sql"


def test_generate_migration_filename_slugifies_freetext_name():
    moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    filename = migration_runner.generate_migration_filename("Add Status Index!", now=moment)
    assert filename == "20260102030405_add_status_index.sql"


def test_slugify_rejects_empty_name():
    with pytest.raises(ValueError):
        migration_runner.slugify_migration_name("!!!")


# ---------------------------------------------------------------------------
# Filename validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename",
    [
        "20260629134507_add_status_index.sql",
        "20260101000000_a.sql",
        "20251231235959_add_multi_word_slug.sql",
    ],
)
def test_is_valid_migration_filename_accepts_canonical_names(filename):
    assert migration_runner.is_valid_migration_filename(filename) is True


@pytest.mark.parametrize(
    "filename",
    [
        "add_status_index.sql",  # no timestamp
        "2026_add_status_index.sql",  # short timestamp
        "20260629134507_Add_Status.sql",  # uppercase slug
        "20260629134507_add status.sql",  # space in slug
        "20260629134507_add_status_index.txt",  # wrong extension
        "20260629134507_.sql",  # empty slug
        "20260629134507_add__status.sql",  # double underscore
    ],
)
def test_is_valid_migration_filename_rejects_bad_names(filename):
    assert migration_runner.is_valid_migration_filename(filename) is False


# ---------------------------------------------------------------------------
# Template generation
# ---------------------------------------------------------------------------


def test_template_is_sql_only_with_transaction_marker_and_no_begin_commit():
    template = migration_runner.render_migration_template("add_status_index")
    assert "-- transaction: true" in template
    assert "BEGIN" not in template
    assert "COMMIT" not in template
    assert "-- transaction: false" in template  # documented for CONCURRENTLY
    # Template alone has no executable SQL.
    assert migration_runner.migration_has_executable_sql(template) is False


def test_create_migration_file_creates_directory_and_file(tmp_path):
    moment = datetime(2026, 6, 29, 13, 45, 7, tzinfo=UTC)
    assert not (tmp_path / "db" / "migrations").exists()
    path = migration_runner.create_migration_file("add_status_index", root=tmp_path, now=moment)
    assert path.name == "20260629134507_add_status_index.sql"
    assert path.parent == tmp_path / "db" / "migrations"
    assert path.is_file()
    assert "-- transaction: true" in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Discovery order
# ---------------------------------------------------------------------------


def test_discover_migration_files_sorted_lexicographically(tmp_path):
    _write_migration(tmp_path, "20260103000000_third.sql", _REAL_SQL)
    _write_migration(tmp_path, "20260101000000_first.sql", _REAL_SQL)
    _write_migration(tmp_path, "20260102000000_second.sql", _REAL_SQL)

    files = migration_runner.discover_migration_files(root=tmp_path)
    assert [path.name for path in files] == [
        "20260101000000_first.sql",
        "20260102000000_second.sql",
        "20260103000000_third.sql",
    ]


def test_discover_migration_files_empty_when_dir_missing(tmp_path):
    assert migration_runner.discover_migration_files(root=tmp_path) == []


def test_discover_migration_files_rejects_invalid_filename(tmp_path):
    _write_migration(tmp_path, "not_a_migration.sql", _REAL_SQL)
    with pytest.raises(migration_runner.InvalidMigrationFilenameError):
        migration_runner.discover_migration_files(root=tmp_path)


# ---------------------------------------------------------------------------
# Transaction marker parsing
# ---------------------------------------------------------------------------


def test_parse_transactional_defaults_true_without_marker():
    assert migration_runner.parse_transactional("CREATE TABLE x (id int);") is True


def test_parse_transactional_true_marker():
    assert migration_runner.parse_transactional("-- transaction: true\nSELECT 1;") is True


def test_parse_transactional_false_marker():
    sql = "-- transaction: false\nCREATE INDEX CONCURRENTLY i ON t (c);"
    assert migration_runner.parse_transactional(sql) is False


def test_parse_transactional_ignores_documentation_lines():
    template = migration_runner.render_migration_template("noop")
    # The template documents both true and false but defaults to true.
    assert migration_runner.parse_transactional(template) is True


# ---------------------------------------------------------------------------
# Empty / template-only rejection
# ---------------------------------------------------------------------------


def test_migration_has_executable_sql_detects_real_statements():
    assert migration_runner.migration_has_executable_sql(_REAL_SQL) is True


def test_migration_has_executable_sql_false_for_comments_and_blanks():
    assert migration_runner.migration_has_executable_sql("-- just a comment\n\n  \n") is False
    assert migration_runner.migration_has_executable_sql("/* block */\n;\n") is False


def test_apply_rejects_template_only_migration(tmp_path):
    template = migration_runner.render_migration_template("add_status_index")
    _write_migration(tmp_path, "20260101000000_add_status_index.sql", template)
    conn = _FakeConn()
    with pytest.raises(migration_runner.EmptyMigrationError):
        migration_runner.apply_pending_migrations(conn, root=tmp_path)
    assert conn.committed == {}


# ---------------------------------------------------------------------------
# Explicit transaction-control rejection
# ---------------------------------------------------------------------------


def test_find_explicit_transaction_control_flags_top_level_statements():
    sql = "CREATE TABLE t (id int);\nCOMMIT;\n"
    assert migration_runner.find_explicit_transaction_control(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "BEGIN;\nCREATE TABLE t (id int);",
        "CREATE TABLE t (id int);\nCOMMIT;",
        "ROLLBACK;",
        "START TRANSACTION;\nSELECT 1;",
        "SAVEPOINT sp1;",
    ],
)
def test_find_explicit_transaction_control_detects_each_form(sql):
    assert migration_runner.find_explicit_transaction_control(sql)


def test_find_explicit_transaction_control_ignores_plpgsql_body():
    sql = (
        "CREATE FUNCTION f() RETURNS void AS $$\n"
        "BEGIN\n"
        "  PERFORM 1;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql;\n"
        "DO $$ BEGIN PERFORM 1; END $$;\n"
        "SELECT CASE WHEN true THEN 1 ELSE 0 END;\n"
    )
    assert migration_runner.find_explicit_transaction_control(sql) == []


def test_apply_rejects_explicit_transaction_control(tmp_path):
    body = "-- transaction: true\nCREATE TABLE t (id int);\nCOMMIT;\n"
    _write_migration(tmp_path, "20260101000000_bad_txn.sql", body)
    conn = _FakeConn()
    with pytest.raises(migration_runner.MigrationTransactionControlError):
        migration_runner.apply_pending_migrations(conn, root=tmp_path)
    assert conn.committed == {}


# ---------------------------------------------------------------------------
# Pending migration ids (release-state detection)
# ---------------------------------------------------------------------------


def test_pending_migration_ids_lists_only_unapplied_in_order(tmp_path):
    _write_migration(tmp_path, "20260101000000_first.sql", _REAL_SQL)
    _write_migration(tmp_path, "20260102000000_second.sql", _REAL_SQL)
    conn = _FakeConn(applied={"20260101000000_first.sql": "whatever"})

    pending = migration_runner.pending_migration_ids(conn, root=tmp_path)
    assert pending == ["20260102000000_second.sql"]


def test_pending_migration_ids_empty_when_all_applied(tmp_path):
    _write_migration(tmp_path, "20260101000000_first.sql", _REAL_SQL)
    conn = _FakeConn(applied={"20260101000000_first.sql": "whatever"})
    assert migration_runner.pending_migration_ids(conn, root=tmp_path) == []


def test_inspect_migrations_separates_pending_from_drift(tmp_path):
    first = _write_migration(tmp_path, "20260101000000_first.sql", _REAL_SQL)
    _write_migration(tmp_path, "20260102000000_second.sql", _REAL_SQL)
    # first is applied but its recorded checksum no longer matches the on-disk
    # content (drift); second has never been applied (pending).
    conn = _FakeConn(applied={"20260101000000_first.sql": "stale-checksum"})

    result = migration_runner.inspect_migrations(conn, root=tmp_path)
    assert result == {
        "pending": ["20260102000000_second.sql"],
        "drifted": ["20260101000000_first.sql"],
    }

    # When the recorded checksum matches the on-disk content, it is neither
    # pending nor drifted.
    good_checksum = migration_runner.migration_checksum(first.read_text(encoding="utf-8"))
    conn_ok = _FakeConn(
        applied={
            "20260101000000_first.sql": good_checksum,
            "20260102000000_second.sql": migration_runner.migration_checksum(_REAL_SQL),
        }
    )
    assert migration_runner.inspect_migrations(conn_ok, root=tmp_path) == {
        "pending": [],
        "drifted": [],
    }


def test_inspect_migrations_does_not_create_tracking_table(tmp_path):
    _write_migration(tmp_path, "20260101000000_first.sql", _REAL_SQL)
    conn = _FakeConn()

    assert migration_runner.inspect_migrations(conn, root=tmp_path) == {
        "pending": ["20260101000000_first.sql"],
        "drifted": [],
    }
    assert conn.table_created is False


def test_apply_can_reuse_caller_owned_lock(tmp_path):
    _write_migration(tmp_path, "20260101000000_first.sql", _REAL_SQL)
    conn = _FakeConn()

    result = migration_runner.apply_pending_migrations(
        conn,
        root=tmp_path,
        acquire_lock=False,
    )

    assert result["applied"] == ["20260101000000_first.sql"]
    assert conn.locks == []
    assert conn.unlocks == []


# ---------------------------------------------------------------------------
# Applied migration tracking
# ---------------------------------------------------------------------------


def test_apply_records_and_then_skips_applied_migration(tmp_path):
    path = _write_migration(tmp_path, "20260101000000_first.sql", _REAL_SQL)
    conn = _FakeConn()

    first = migration_runner.apply_pending_migrations(conn, root=tmp_path)
    assert first["applied"] == ["20260101000000_first.sql"]
    assert first["skipped"] == []
    checksum = migration_runner.migration_checksum(path.read_text(encoding="utf-8"))
    assert conn.committed == {"20260101000000_first.sql": checksum}

    second = migration_runner.apply_pending_migrations(conn, root=tmp_path)
    assert second["applied"] == []
    assert second["skipped"] == ["20260101000000_first.sql"]


# ---------------------------------------------------------------------------
# Checksum drift detection
# ---------------------------------------------------------------------------


def test_apply_raises_on_checksum_drift(tmp_path):
    _write_migration(tmp_path, "20260101000000_first.sql", _REAL_SQL)
    conn = _FakeConn(applied={"20260101000000_first.sql": "stale-checksum"})
    with pytest.raises(migration_runner.MigrationChecksumError):
        migration_runner.apply_pending_migrations(conn, root=tmp_path)


# ---------------------------------------------------------------------------
# Transactional rollback behavior
# ---------------------------------------------------------------------------


def test_transactional_migration_rolls_back_and_records_nothing_on_failure(tmp_path):
    body = "-- transaction: true\nCREATE TABLE will_fail (id int);\n"
    _write_migration(tmp_path, "20260101000000_boom.sql", body)
    conn = _FakeConn(fail_on="will_fail")

    with pytest.raises(RuntimeError, match="migration body failed"):
        migration_runner.apply_pending_migrations(conn, root=tmp_path)

    assert conn.rollbacks == 1
    assert conn.commits == 0
    assert conn.committed == {}  # no tracking row written for the failed migration


def test_transactional_migration_commits_body_and_tracking_together(tmp_path):
    _write_migration(tmp_path, "20260101000000_ok.sql", _REAL_SQL)
    conn = _FakeConn()

    migration_runner.apply_pending_migrations(conn, root=tmp_path)

    assert conn.commits == 1
    # The migration body ran while NOT in autocommit (wrapped in a transaction).
    assert conn.bodies
    assert conn.bodies[0][1] is False
    assert "20260101000000_ok.sql" in conn.committed


def test_caller_owned_autocommit_is_restored_after_transactional_migration(tmp_path):
    _write_migration(tmp_path, "20260101000000_ok.sql", _REAL_SQL)
    conn = _FakeConn()
    conn.autocommit = True

    migration_runner.apply_pending_migrations(
        conn,
        root=tmp_path,
        acquire_lock=False,
    )

    assert conn.autocommit is True


def test_caller_owned_autocommit_is_restored_after_transactional_failure(tmp_path):
    body = "-- transaction: true\nCREATE TABLE will_fail (id int);\n"
    _write_migration(tmp_path, "20260101000000_boom.sql", body)
    conn = _FakeConn(fail_on="will_fail")
    conn.autocommit = True

    with pytest.raises(RuntimeError, match="migration body failed"):
        migration_runner.apply_pending_migrations(
            conn,
            root=tmp_path,
            acquire_lock=False,
        )

    assert conn.autocommit is True


# ---------------------------------------------------------------------------
# Non-transactional migration behavior
# ---------------------------------------------------------------------------


def test_non_transactional_migration_runs_without_wrapping_and_records_after(tmp_path):
    body = "-- transaction: false\nCREATE INDEX CONCURRENTLY idx ON t (c);\n"
    _write_migration(tmp_path, "20260101000000_concurrent.sql", body)
    conn = _FakeConn()

    result = migration_runner.apply_pending_migrations(conn, root=tmp_path)

    assert result["applied"] == ["20260101000000_concurrent.sql"]
    # Body executed in autocommit (not wrapped in a transaction).
    assert conn.bodies
    assert conn.bodies[0][1] is True
    assert conn.commits == 0  # no explicit commit/rollback wrapping
    assert "20260101000000_concurrent.sql" in conn.committed


def test_runner_restores_non_autocommit_after_non_transactional_migration(tmp_path):
    body = "-- transaction: false\nCREATE INDEX CONCURRENTLY idx ON t (c);\n"
    _write_migration(tmp_path, "20260101000000_concurrent.sql", body)
    conn = _FakeConn()

    migration_runner.apply_pending_migrations(conn, root=tmp_path)

    assert conn.autocommit is False


def test_non_transactional_crash_window_is_durable_and_never_reruns_sql(tmp_path):
    body = "-- transaction: false\nVACUUM public.large_table;\n"
    migration_id = "20260101000000_vacuum.sql"
    _write_migration(tmp_path, migration_id, body)
    conn = _FakeConn(fail_finalize=True)

    with pytest.raises(KeyboardInterrupt, match="simulated crash"):
        migration_runner.apply_pending_migrations(conn, root=tmp_path)

    assert len(conn.bodies) == 1
    assert conn.committed[migration_id].startswith("in_progress:")

    conn.fail_finalize = False
    with pytest.raises(migration_runner.MigrationInProgressError) as error:
        migration_runner.apply_pending_migrations(conn, root=tmp_path)

    assert migration_id in str(error.value)
    assert "flow-steward database status --json" in str(error.value)
    assert len(conn.bodies) == 1


# ---------------------------------------------------------------------------
# Advisory lock acquisition
# ---------------------------------------------------------------------------


def test_apply_acquires_and_releases_advisory_lock(tmp_path):
    _write_migration(tmp_path, "20260101000000_first.sql", _REAL_SQL)
    conn = _FakeConn()

    migration_runner.apply_pending_migrations(conn, root=tmp_path)

    assert conn.locks == [migration_runner.MIGRATION_ADVISORY_LOCK_KEY]
    assert conn.unlocks == [migration_runner.MIGRATION_ADVISORY_LOCK_KEY]


def test_advisory_lock_released_even_when_no_migrations(tmp_path):
    conn = _FakeConn()
    result = migration_runner.apply_pending_migrations(conn, root=tmp_path)
    assert result == {"total": 0, "applied": [], "skipped": []}
    assert conn.locks == [migration_runner.MIGRATION_ADVISORY_LOCK_KEY]
    assert conn.unlocks == [migration_runner.MIGRATION_ADVISORY_LOCK_KEY]


def test_advisory_lock_released_on_failure(tmp_path):
    body = "-- transaction: true\nCREATE TABLE will_fail (id int);\n"
    _write_migration(tmp_path, "20260101000000_boom.sql", body)
    conn = _FakeConn(fail_on="will_fail")

    with pytest.raises(RuntimeError):
        migration_runner.apply_pending_migrations(conn, root=tmp_path)

    assert conn.unlocks == [migration_runner.MIGRATION_ADVISORY_LOCK_KEY]


def test_advisory_lock_acquisition_failure_restores_caller_autocommit(tmp_path, monkeypatch):
    conn = _FakeConn()

    def fail_acquisition(connection):
        connection.autocommit = True
        raise RuntimeError("advisory lock acquisition failed")

    monkeypatch.setattr(migration_runner, "_acquire_advisory_lock", fail_acquisition)

    with pytest.raises(RuntimeError, match="advisory lock acquisition failed"):
        migration_runner.apply_pending_migrations(conn, root=tmp_path)

    assert conn.autocommit is False
    assert conn.unlocks == []


# ---------------------------------------------------------------------------
# Schema-driven CLI command registration
# ---------------------------------------------------------------------------


def test_db_tooling_group_exposes_generate_migration_command():
    from core.application.management.command_specs.database import DATABASE_COMMAND_SPECS

    specs = {spec.command_id: spec for spec in DATABASE_COMMAND_SPECS}

    assert specs["database.generate-migration"].path == ("database", "generate-migration")
    assert specs["database.generate-migration"].service_ref == "database.generate-migration"


def test_generate_migration_cli_invokes_create_migration_file(monkeypatch, tmp_path):
    captured: dict[str, str] = {}

    def _fake_create(name: str, **_kwargs) -> Path:
        captured["name"] = name
        return tmp_path / "db" / "migrations" / "20260101000000_add_status_index.sql"

    monkeypatch.setattr(migration_runner, "create_migration_file", _fake_create)

    exit_code = generate_migration.main(["--name", "add_status_index"])
    assert exit_code == 0
    assert captured["name"] == "add_status_index"


# ---------------------------------------------------------------------------
# Search path stabilisation at the execution boundary
# ---------------------------------------------------------------------------


def test_transactional_migration_body_runs_under_a_fixed_search_path(tmp_path):
    """A migration must read the same catalog whatever the caller's session is set to.

    The guarantee belongs to the runner: putting it inside a migration file means
    editing that file later, which breaks every database that already recorded its
    checksum.
    """
    _write_migration(tmp_path, "20260101000000_stable.sql", _REAL_SQL)
    conn = _FakeConn()

    migration_runner.apply_pending_migrations(conn, root=tmp_path)

    assert conn.search_path == [
        f"SET LOCAL search_path TO {migration_runner.MIGRATION_SEARCH_PATH}"
    ]
    assert [body for body, _ in conn.bodies] == [_REAL_SQL]


def test_non_transactional_migration_sets_and_releases_the_search_path(tmp_path):
    """Autocommit has no transaction to scope SET LOCAL, so the override is released."""
    body = "-- transaction: false\nCREATE INDEX CONCURRENTLY idx ON public.example (id);\n"
    _write_migration(tmp_path, "20260101000000_concurrent.sql", body)
    conn = _FakeConn()

    migration_runner.apply_pending_migrations(conn, root=tmp_path)

    assert conn.search_path == [
        f"SET search_path TO {migration_runner.MIGRATION_SEARCH_PATH}",
        "RESET search_path",
    ]


def test_non_transactional_migration_releases_the_search_path_after_a_failure(tmp_path):
    """A failed body must not leave the connection on the migration search path."""
    body = "-- transaction: false\nCREATE INDEX CONCURRENTLY boom ON public.example (id);\n"
    _write_migration(tmp_path, "20260101000000_concurrent.sql", body)
    conn = _FakeConn(fail_on="CREATE INDEX CONCURRENTLY boom")

    with pytest.raises(RuntimeError):
        migration_runner.apply_pending_migrations(conn, root=tmp_path)

    assert conn.search_path[-1] == "RESET search_path"
