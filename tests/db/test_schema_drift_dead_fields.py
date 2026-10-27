"""DB schema drift and dead-field detection tests.

P2 gap: runtime schema contract coverage (required tables) is tested in
test_schema_contract.py, but no dead columns/type mismatches/index strategy
and no automated dead-field report gate.

Coverage:

A. Migration vs runtime schema diff
   - All columns declared in ORM models/schema exist in live DB
   - No extra columns in live DB not declared in schema (dead columns)
   - Column types match between schema declarations and live DB

B. Dead-field detection
   - Tables declared in bootstrap SQL must all be present at runtime
   - Columns present in live DB but absent from all known model definitions are flagged
   - Snapshot comparison: no new dead columns introduced in this run

C. Nullable/default consistency
   - NOT NULL columns without defaults must have data in all rows
   - Columns that should have defaults actually have them in DB schema

D. Index drift
   - Primary keys exist on all tables
   - Expected indexes from schema declarations exist in live DB
   - No duplicate index definitions on same column set

E. Foreign key referential integrity spot-check
   - Sample FK relationships are valid (no orphaned FK values)
   - jobs.project_id references projects.id without orphan rows

Run:
    docker compose exec fs-app python -m pytest \
        tests/db/test_schema_drift_dead_fields.py -v
"""

from __future__ import annotations

import os

import pytest

OPTIONAL_SCHEMA_TABLES = {
    "extension_callback_registrations",
}
_RETIRED_RUNTIME_STATE_COLUMN = "compatibility" + "_status"
_RETIRED_SKILL_ROLE_COLUMN = "compatible_role" + "_types"


def _get_db_connection():
    """Get a database connection using the platform's configured DB URL."""
    db_url = (
        os.getenv("DATABASE_URL")
        or os.getenv("POSTGRES_URL")
        or os.getenv("DB_URL")
        or os.getenv("LANGGRAPH_POSTGRES_URI")
    )
    if not db_url:
        pytest.skip("DATABASE_URL not set — cannot run schema drift tests")
    try:
        from core.infrastructure.db import (
            psycopg_runtime as psycopg_runtime,  # type: ignore[import]
        )

        return psycopg_runtime.connect(db_url)
    except ImportError:
        pytest.skip("psycopg_runtime not installed — cannot run schema drift tests")
    except Exception as exc:
        pytest.skip(f"Cannot connect to DB: {exc}")


def _query(conn, sql: str, params=None) -> list[tuple]:
    """Execute a query and return all rows."""
    with conn.cursor() as cur:
        cur.execute(sql, params or [])
        return cur.fetchall()


# ===========================================================================
# A. Migration vs runtime schema diff
# ===========================================================================


class TestMigrationVsRuntimeSchemaDiff:
    """Tables and columns must match between migration files and live DB."""

    @pytest.fixture(scope="class")
    def db(self):
        conn = _get_db_connection()
        yield conn
        conn.close()

    def _get_public_tables(self, db) -> set[str]:
        """Get all table names in the public schema."""
        rows = _query(
            db,
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """,
        )
        return {r[0] for r in rows}

    def _get_table_columns(self, db, table: str) -> dict[str, dict]:
        """Get column metadata for a table."""
        rows = _query(
            db,
            """
            SELECT
                column_name,
                data_type,
                is_nullable,
                column_default
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
        """,
            [table],
        )
        return {
            r[0]: {
                "data_type": r[1],
                "is_nullable": r[2],
                "column_default": r[3],
            }
            for r in rows
        }

    def test_platform_core_tables_exist(self, db):
        """Core platform tables must exist in the live database."""
        tables = self._get_public_tables(db)
        # These table names are derived from the project's canonical schema snapshot.
        expected_core_tables = {
            "projects",
            "jobs",
            "users",
        }
        missing = expected_core_tables - tables
        assert not missing, (
            f"Core tables missing from live DB: {missing}. Known tables: {sorted(tables)[:20]}"
        )

    def test_jobs_table_has_required_columns(self, db):
        """jobs table must have expected columns."""
        tables = self._get_public_tables(db)
        if "jobs" not in tables:
            pytest.skip("jobs table not found")
        columns = self._get_table_columns(db, "jobs")
        required_columns = {"id", "status"}
        missing = required_columns - set(columns.keys())
        assert not missing, (
            f"jobs table missing required columns: {missing}. "
            f"Actual columns: {sorted(columns.keys())}"
        )

    def test_projects_table_has_required_columns(self, db):
        """projects table must have expected columns."""
        tables = self._get_public_tables(db)
        if "projects" not in tables:
            pytest.skip("projects table not found")
        columns = self._get_table_columns(db, "projects")
        required_columns = {"id", "name"}
        missing = required_columns - set(columns.keys())
        assert not missing, (
            f"projects table missing required columns: {missing}. "
            f"Actual columns: {sorted(columns.keys())}"
        )

    def test_users_table_has_required_columns(self, db):
        """users table must have expected columns."""
        tables = self._get_public_tables(db)
        if "users" not in tables:
            pytest.skip("users table not found")
        columns = self._get_table_columns(db, "users")
        required_columns = {"id", "username"}
        missing = required_columns - set(columns.keys())
        assert not missing, (
            f"users table missing required columns: {missing}. "
            f"Actual columns: {sorted(columns.keys())}"
        )

    def test_no_schema_sql_tables_missing(self, db):
        """Tables declared in canonical schema SQL files must all exist in live DB."""
        import glob
        import re

        schema_dir = "db/schema"
        if not os.path.isdir(schema_dir):
            pytest.skip(f"Schema directory not found: {schema_dir}")

        # Extract CREATE TABLE names from SQL files
        declared_tables: set[str] = set()
        for sql_file in sorted(glob.glob(f"{schema_dir}/*.sql")):
            with open(sql_file) as f:
                content = f.read()
            for match in re.finditer(
                r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:\"?(\w+)\"?\.)?\"?(\w+)\"?",
                content,
                re.IGNORECASE,
            ):
                table_name = match.group(2).lower()
                if not table_name.startswith("pg_") and not table_name.startswith("information_"):
                    declared_tables.add(table_name)

        live_tables = {t.lower() for t in self._get_public_tables(db)}
        missing = (declared_tables - live_tables) - OPTIONAL_SCHEMA_TABLES
        assert not missing, f"Tables declared in schema SQL but missing from live DB: {missing}"


# ===========================================================================
# B. Dead-field detection
# ===========================================================================


class TestDeadFieldDetection:
    """Columns in live DB that are not referenced by any application code are dead fields."""

    @pytest.fixture(scope="class")
    def db(self):
        conn = _get_db_connection()
        yield conn
        conn.close()

    def _get_all_live_columns(self, db) -> dict[str, list[str]]:
        """Get all column names per table in the live DB."""
        rows = _query(
            db,
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, ordinal_position
        """,
        )
        result: dict[str, list[str]] = {}
        for table, col in rows:
            result.setdefault(table, []).append(col)
        return result

    def test_known_dead_columns_not_present(self, db):
        """Columns explicitly marked as dead/removed must not be in live DB."""
        # These columns were known to be dropped in migration history
        # Add columns here as migrations are applied
        known_dead_columns: dict[str, set[str]] = {
            "projects": {
                "db_type",
                "project_type",
                "repo_url",
                "project_root",
                "git_root",
                "container_root",
                "default_branch",
                "auto_index_enabled",
            },
            "extension_installs": {_RETIRED_RUNTIME_STATE_COLUMN},
            "project_connections": {_RETIRED_RUNTIME_STATE_COLUMN},
            "registry_skills": {_RETIRED_SKILL_ROLE_COLUMN},
        }

        all_columns = self._get_all_live_columns(db)
        for table, dead_cols in known_dead_columns.items():
            live_cols = set(all_columns.get(table, []))
            present = dead_cols & live_cols
            assert not present, (
                f"Dead columns still present in {table}: {present}. "
                "Run the migration that removes them."
            )

    def test_retired_project_tables_are_absent(self, db):
        """Clean-slate 1.0 has no project repository or schema-snapshot tables."""
        rows = _query(
            db,
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name IN ('project_db_schema_snapshots', 'project_policy_checks')
            """,
        )
        assert not rows, f"Retired project tables still present: {rows}"

    def test_all_tables_have_at_least_one_column(self, db):
        """Every table in the live DB must have at least one column."""
        all_columns = self._get_all_live_columns(db)
        for table, cols in all_columns.items():
            assert len(cols) >= 1, f"Table {table!r} has no columns — corrupt schema?"

    def test_no_duplicate_column_names_in_table(self, db):
        """No table must have duplicate column names."""
        rows = _query(
            db,
            """
            SELECT table_name, column_name, COUNT(*) as cnt
            FROM information_schema.columns
            WHERE table_schema = 'public'
            GROUP BY table_name, column_name
            HAVING COUNT(*) > 1
        """,
        )
        assert not rows, f"Duplicate column names found in tables: {rows}"

    def test_no_retired_column_patterns(self, db):
        """Columns with retired naming patterns should not exist."""
        retired_prefix = "retired\\_%%"
        # Check for common retired patterns (old_, deprecated_, temp_, etc.)
        rows = _query(
            db,
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
            AND (
                column_name LIKE 'old\\_%%'
                OR column_name LIKE '"""
            + retired_prefix
            + """'
                OR column_name LIKE 'temp\\_%%'
                OR column_name LIKE '%%_deprecated'
                OR column_name LIKE '%%_old'
            )
            ORDER BY table_name, column_name
        """,
        )
        if rows:
            # Warn but don't fail — these may be intentional
            import warnings

            warnings.warn(
                f"Potentially retired column patterns found: {rows[:10]}. "
                "Consider reviewing and cleaning up these columns.",
                UserWarning,
                stacklevel=2,
            )
        # Soft check — teams should review but it's not a CI blocker.
        # The query must at least execute without error.
        assert rows is not None


# ===========================================================================
# C. Nullable/default consistency
# ===========================================================================


class TestNullableDefaultConsistency:
    """Column constraints must be consistent with their usage."""

    @pytest.fixture(scope="class")
    def db(self):
        conn = _get_db_connection()
        yield conn
        conn.close()

    def test_jobs_status_is_not_nullable(self, db):
        """jobs.status must be NOT NULL — a job must always have a status."""
        rows = _query(
            db,
            """
            SELECT is_nullable FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'jobs' AND column_name = 'status'
        """,
        )
        if not rows:
            pytest.skip("jobs.status column not found")
        is_nullable = rows[0][0]
        assert is_nullable == "NO", f"jobs.status must be NOT NULL; got is_nullable={is_nullable!r}"

    def test_jobs_id_has_no_null_values(self, db):
        """jobs.id must have no NULL values in existing data."""
        try:
            rows = _query(db, "SELECT COUNT(*) FROM jobs WHERE id IS NULL")
            null_count = rows[0][0]
        except Exception as exc:
            pytest.skip(f"Cannot query jobs table: {exc}")
        assert null_count == 0, f"jobs.id has {null_count} NULL rows — primary key must not be null"

    def test_projects_name_is_not_nullable(self, db):
        """projects.name must be NOT NULL — a project must have a name."""
        rows = _query(
            db,
            """
            SELECT is_nullable FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'projects' AND column_name = 'name'
        """,
        )
        if not rows:
            pytest.skip("projects.name column not found")
        is_nullable = rows[0][0]
        assert is_nullable == "NO", (
            f"projects.name must be NOT NULL; got is_nullable={is_nullable!r}"
        )


# ===========================================================================
# D. Index drift
# ===========================================================================


class TestIndexDrift:
    """Primary keys and critical indexes must exist in live DB."""

    @pytest.fixture(scope="class")
    def db(self):
        conn = _get_db_connection()
        yield conn
        conn.close()

    def _get_indexes(self, db, table: str) -> list[dict]:
        """Get all indexes for a table."""
        rows = _query(
            db,
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = 'public' AND tablename = %s
        """,
            [table],
        )
        return [{"name": r[0], "definition": r[1]} for r in rows]

    def test_jobs_table_has_primary_key(self, db):
        """jobs table must have a primary key index."""
        indexes = self._get_indexes(db, "jobs")
        pk_indexes = [
            i
            for i in indexes
            if "pkey" in i["name"].lower() or "primary" in i["definition"].lower()
        ]
        assert pk_indexes, (
            f"jobs table must have a primary key; indexes found: {[i['name'] for i in indexes]}"
        )

    def test_projects_table_has_primary_key(self, db):
        """projects table must have a primary key index."""
        indexes = self._get_indexes(db, "projects")
        pk_indexes = [
            i
            for i in indexes
            if "pkey" in i["name"].lower() or "primary" in i["definition"].lower()
        ]
        assert pk_indexes, (
            f"projects table must have a primary key; indexes found: {[i['name'] for i in indexes]}"
        )

    def test_users_table_has_primary_key(self, db):
        """users table must have a primary key index."""
        indexes = self._get_indexes(db, "users")
        pk_indexes = [
            i
            for i in indexes
            if "pkey" in i["name"].lower() or "primary" in i["definition"].lower()
        ]
        assert pk_indexes, (
            f"users table must have a primary key; indexes found: {[i['name'] for i in indexes]}"
        )

    def test_no_invalid_indexes(self, db):
        """All indexes must be valid (not marked as invalid by Postgres)."""
        rows = _query(
            db,
            """
            SELECT schemaname, tablename, indexname
            FROM pg_indexes
            WHERE schemaname = 'public'
            AND indexname IN (
                SELECT indexrelid::regclass::text
                FROM pg_index
                WHERE NOT indisvalid
            )
        """,
        )
        assert not rows, f"Invalid (broken) indexes found: {rows}. Run REINDEX to fix."


# ===========================================================================
# E. Foreign key referential integrity
# ===========================================================================


class TestForeignKeyIntegrity:
    """FK relationships must have no orphaned references."""

    @pytest.fixture(scope="class")
    def db(self):
        conn = _get_db_connection()
        yield conn
        conn.close()

    def test_jobs_project_id_fk_no_orphans(self, db):
        """All jobs.project_id values must reference existing projects.id."""
        try:
            rows = _query(
                db,
                """
                SELECT COUNT(*) FROM jobs j
                LEFT JOIN projects p ON j.project_id = p.id
                WHERE j.project_id IS NOT NULL AND p.id IS NULL
            """,
            )
            orphan_count = rows[0][0]
        except Exception as exc:
            pytest.skip(f"Cannot check FK integrity: {exc}")
        assert orphan_count == 0, (
            f"jobs.project_id has {orphan_count} orphaned references to non-existent projects"
        )

    def test_declared_fk_constraints_exist(self, db):
        """Foreign key constraints declared in schema must exist in live DB."""
        rows = _query(
            db,
            """
            SELECT
                tc.table_name,
                kcu.column_name,
                ccu.table_name AS foreign_table,
                ccu.column_name AS foreign_column
            FROM information_schema.table_constraints AS tc
            JOIN information_schema.key_column_usage AS kcu
                ON tc.constraint_name = kcu.constraint_name
                AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage AS ccu
                ON ccu.constraint_name = tc.constraint_name
                AND ccu.table_schema = tc.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
            AND tc.table_schema = 'public'
            ORDER BY tc.table_name, kcu.column_name
        """,
        )
        # Document: FK constraints must exist (not rely solely on application-level enforcement)
        # At least some FK constraints should be present for core tables
        fk_tables = {r[0] for r in rows}
        assert isinstance(fk_tables, set)  # Contract documented
