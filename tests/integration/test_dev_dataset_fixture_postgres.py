from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[2]
ENABLE_SQL = ROOT / "infra/docker/postgres-dev-dataset-fixture/enable.sql"
DISABLE_SQL = ROOT / "infra/docker/postgres-dev-dataset-fixture/disable.sql"


def _run_fixture_sql(
    dsn: str,
    script: Path,
    *,
    role_name: str,
    schema_name: str,
    password: str = "",
    environment: str = "development",
) -> None:
    assert script.is_file(), f"Missing fixture lifecycle script: {script}"
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(
            "SELECT set_config('flow_steward.dev_dataset_reader_role', %s, false)",
            (role_name,),
        )
        connection.execute(
            "SELECT set_config('flow_steward.dev_dataset_reader_schema', %s, false)",
            (schema_name,),
        )
        connection.execute(
            "SELECT set_config('flow_steward.stack_environment', %s, false)",
            (environment,),
        )
        if password:
            connection.execute(
                "SELECT set_config('flow_steward.dev_dataset_reader_password', %s, false)",
                (password,),
            )
        connection.execute(script.read_text(encoding="utf-8"))


def _reader_dsn(dsn: str, *, role_name: str, password: str) -> str:
    return make_conninfo(dsn, user=role_name, password=password)


@pytest.mark.integration
def test_development_dataset_reader_can_only_read_the_safe_jobs_view(disposable_db) -> None:
    suffix = uuid4().hex[:12]
    role_name = f"fs_dev_reader_{suffix}"
    schema_name = f"fs_dev_dataset_{suffix}"
    password = f"reader-{suffix}"

    try:
        _run_fixture_sql(
            disposable_db.dsn,
            ENABLE_SQL,
            role_name=role_name,
            schema_name=schema_name,
            password=password,
        )

        with psycopg.connect(disposable_db.dsn) as administrator:
            role = administrator.execute(
                """
                SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole,
                       rolreplication, rolinherit
                FROM pg_roles
                WHERE rolname = %s
                """,
                (role_name,),
            ).fetchone()
        assert role == (True, False, False, False, False, False)

        with psycopg.connect(
            _reader_dsn(disposable_db.dsn, role_name=role_name, password=password)
        ) as reader:
            cursor = reader.execute(f'SELECT * FROM "{schema_name}".recent_jobs LIMIT 0')
            assert tuple(column.name for column in cursor.description or ()) == (
                "id",
                "status",
                "selected_workflow",
                "started_at",
                "finished_at",
            )

            cursor = reader.execute(
                "SELECT id, status, selected_workflow, started_at, finished_at "
                "FROM public.jobs LIMIT 0"
            )
            assert tuple(column.name for column in cursor.description or ()) == (
                "id",
                "status",
                "selected_workflow",
                "started_at",
                "finished_at",
            )
            cursor = reader.execute(
                "SELECT id, status, selected_workflow, started_at, finished_at "
                "FROM jobs LIMIT 0"
            )
            assert tuple(column.name for column in cursor.description or ()) == (
                "id",
                "status",
                "selected_workflow",
                "started_at",
                "finished_at",
            )

            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                reader.execute("SELECT task_summary FROM public.jobs LIMIT 1")
            reader.rollback()

            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                reader.execute(f"UPDATE \"{schema_name}\".recent_jobs SET status = 'failed'")
    finally:
        if DISABLE_SQL.is_file():
            _run_fixture_sql(
                disposable_db.dsn,
                DISABLE_SQL,
                role_name=role_name,
                schema_name=schema_name,
                environment="production",
            )


@pytest.mark.integration
def test_production_transition_removes_and_next_dev_transition_recreates_reader(
    disposable_db,
) -> None:
    suffix = uuid4().hex[:12]
    role_name = f"fs_dev_reader_{suffix}"
    schema_name = f"fs_dev_dataset_{suffix}"
    password = f"reader-{suffix}"
    reader_dsn = _reader_dsn(disposable_db.dsn, role_name=role_name, password=password)

    try:
        _run_fixture_sql(
            disposable_db.dsn,
            ENABLE_SQL,
            role_name=role_name,
            schema_name=schema_name,
            password=password,
        )
        reader = psycopg.connect(reader_dsn, autocommit=True)
        reader.execute(f'SELECT count(*) FROM "{schema_name}".recent_jobs').fetchone()

        _run_fixture_sql(
            disposable_db.dsn,
            DISABLE_SQL,
            role_name=role_name,
            schema_name=schema_name,
            environment="production",
        )

        with pytest.raises(psycopg.OperationalError):
            reader.execute("SELECT 1")
        reader.close()
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(reader_dsn, connect_timeout=2)

        with psycopg.connect(disposable_db.dsn) as administrator:
            state = administrator.execute(
                """
                SELECT
                    EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s),
                    EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %s)
                """,
                (role_name, schema_name),
            ).fetchone()
        assert state == (False, False)

        _run_fixture_sql(
            disposable_db.dsn,
            ENABLE_SQL,
            role_name=role_name,
            schema_name=schema_name,
            password=password,
        )
        with psycopg.connect(reader_dsn) as recreated_reader:
            assert (
                recreated_reader.execute(
                    f'SELECT count(*) FROM "{schema_name}".recent_jobs'
                ).fetchone()
                is not None
            )
    finally:
        if DISABLE_SQL.is_file():
            _run_fixture_sql(
                disposable_db.dsn,
                DISABLE_SQL,
                role_name=role_name,
                schema_name=schema_name,
                environment="production",
            )


@pytest.mark.integration
def test_development_dataset_reader_cannot_be_enabled_in_production(disposable_db) -> None:
    suffix = uuid4().hex[:12]
    role_name = f"fs_dev_reader_{suffix}"
    schema_name = f"fs_dev_dataset_{suffix}"

    with pytest.raises(psycopg.errors.RaiseException, match="development only"):
        _run_fixture_sql(
            disposable_db.dsn,
            ENABLE_SQL,
            role_name=role_name,
            schema_name=schema_name,
            password=f"reader-{suffix}",
            environment="production",
        )

    with psycopg.connect(disposable_db.dsn) as administrator:
        state = administrator.execute(
            """
            SELECT
                EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s),
                EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %s)
            """,
            (role_name, schema_name),
        ).fetchone()
    assert state == (False, False)
