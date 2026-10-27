from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from core.application.tooling.db import migration_runner
from core.infrastructure.db import psycopg_runtime
from core.infrastructure.db.schema_state import split_sql_statements
from tests._db_env import resolve_test_db_dsn

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.integration
@pytest.mark.parametrize(
    "migration_search_path",
    ['"$user", public', "", "unrelated_oauth_schema,pg_catalog"],
    ids=["default-search-path", "empty-search-path", "unrelated-search-path"],
)
def test_clean_install_has_complete_llm_verification_schema(
    migration_search_path: str,
) -> None:
    admin_dsn = str(resolve_test_db_dsn() or "").strip()
    if not admin_dsn:
        pytest.skip("FS_DB_DSN is required for disposable clean-install verification")

    database_name = f"fs_clean_llm_{uuid.uuid4().hex[:12]}"
    admin = psycopg_runtime.connect(admin_dsn)
    admin_host = str(getattr(admin.info, "host", "") or "").strip().lower()
    if admin_host not in {"localhost", "127.0.0.1", "::1", "pg-fs"}:
        admin.close()
        pytest.skip("clean-install database creation is restricted to local test Postgres")
    admin.autocommit = True
    target = None
    try:
        with admin.cursor() as cur:
            cur.execute(
                psycopg_runtime.sql.SQL("CREATE DATABASE {}").format(
                    psycopg_runtime.sql.Identifier(database_name)
                )
            )

        target = psycopg_runtime.connect(admin_dsn, dbname=database_name)
        for path in sorted((ROOT / "db" / "schema").glob("*.sql")):
            sql_text = path.read_text(encoding="utf-8")
            with target.cursor() as cur:
                for statement in split_sql_statements(sql_text):
                    if statement.strip():
                        cur.execute(statement)
            target.commit()

        if migration_search_path.startswith("unrelated_oauth_schema"):
            with target.cursor() as cur:
                cur.execute("CREATE SCHEMA unrelated_oauth_schema")
            target.commit()
        with target.cursor() as cur:
            cur.execute("SELECT set_config('search_path', %s, false)", (migration_search_path,))
        target.commit()

        pending = migration_runner.apply_pending_migrations(target)

        with target.cursor() as cur:
            cur.execute(
                """
                SELECT table_name, column_name
                  FROM information_schema.columns
                 WHERE table_schema = 'public'
                   AND table_name IN (
                       'llm_provider_sources',
                       'llm_provider_models',
                       'model_providers'
                   )
                """
            )
            columns = {(str(row[0]), str(row[1])) for row in cur.fetchall()}
            cur.execute(
                """
                SELECT migration_id
                  FROM public.platform_schema_migrations
                 ORDER BY migration_id
                """
            )
            migration_ids = [str(row[0]) for row in cur.fetchall()]
            cur.execute(
                """
                SELECT column_default, is_nullable
                  FROM information_schema.columns
                 WHERE table_schema = 'public'
                   AND table_name = 'registry_workflows'
                   AND column_name = 'revision'
                """
            )
            workflow_revision_column = cur.fetchone()

        assert {
            ("llm_provider_sources", "connection_config_jsonb"),
            ("llm_provider_models", "declared_capabilities_jsonb"),
            ("llm_provider_models", "verified_capabilities_jsonb"),
            ("llm_provider_models", "verification_status"),
            ("llm_provider_models", "verification_started_at"),
            ("llm_provider_models", "verification_generation"),
            ("llm_provider_models", "upstream_revision"),
            ("llm_provider_models", "upstream_digest"),
            ("model_providers", "verification_status"),
            ("model_providers", "verified_capabilities_jsonb"),
            ("model_providers", "verification_started_at"),
            ("model_providers", "verification_generation"),
            ("model_providers", "upstream_revision"),
        } <= columns
        expected_post_baseline = [
            path.name for path in sorted((ROOT / "db" / "migrations").glob("*.sql"))
        ]
        assert pending["applied"] == expected_post_baseline
        assert migration_ids == expected_post_baseline
        assert workflow_revision_column == ("1", "NO")
    finally:
        if target is not None:
            target.close()
        with admin.cursor() as cur:
            cur.execute(
                psycopg_runtime.sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    psycopg_runtime.sql.Identifier(database_name)
                )
            )
        admin.close()
