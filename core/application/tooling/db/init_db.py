# mypy: ignore-errors
"""
Flow Steward database initialization script.

Run once after Docker services are up to finalize a clean DB bootstrap:
  - schema is loaded from db/schema/*.sql on first Postgres startup
  - checked-in lookup + registry seeds are applied idempotently
  - platform observability helpers and LangGraph checkpoint tables are verified

Usage:
    docker compose -f infra/docker/docker-compose.yaml exec fs-app \
        flow-steward database bootstrap-seed --confirm
"""

import argparse
from pathlib import Path
from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.registry.catalog_categories import (
    CATALOG_CATEGORY_IDS,
    DEFAULT_CATALOG_CATEGORY,
)
from core.application.tooling.db import migration_runner
from core.infrastructure.config import runtime_config as core_config
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.db import psycopg_runtime
from core.infrastructure.db.schema_state import (
    CANONICAL_SCHEMA_GLOB,
    canonical_schema_files,
    canonical_seed_fingerprint,
    reconcile_canonical_schema,
    record_canonical_schema_state,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG = getattr(core_config, "CONFIG", {})
REQUIRED_SEED_FILES: tuple[str, ...] = (
    "db/seeds/lookup_seed.json",
    "db/seeds/registry_seed.json",
)


def bootstrap_dir(root: Path | None = None) -> Path:
    repo_root = Path(root) if root is not None else REPO_ROOT
    return repo_root / "db" / "schema"


def seeds_dir(root: Path | None = None) -> Path:
    repo_root = Path(root) if root is not None else REPO_ROOT
    return repo_root / "db" / "seeds"


def validate_bootstrap_layout(root: Path | None = None) -> list[Path]:
    """Ensure the repo only uses the canonical schema/seeds layout."""
    repo_root = Path(root) if root is not None else REPO_ROOT
    schema_files = canonical_schema_files(repo_root)
    required_seed_files = [repo_root / rel_path for rel_path in REQUIRED_SEED_FILES]
    missing = [path for path in required_seed_files if not path.is_file()]
    if missing:
        rendered = ", ".join(
            str(path.relative_to(repo_root)) if path.is_relative_to(repo_root) else str(path)
            for path in missing
        )
        raise FileNotFoundError(
            f"Canonical DB layout is incomplete; missing required files: {rendered}"
        )
    if not schema_files:
        raise FileNotFoundError(
            f"Canonical DB layout is incomplete; missing required files: {CANONICAL_SCHEMA_GLOB}"
        )

    migration_dir = repo_root / "migrations"
    if migration_dir.exists():
        raise RuntimeError(
            "Forbidden migrations directory detected in canonical clean-bootstrap layout: "
            f"{migration_dir.relative_to(repo_root)}"
        )
    return [*schema_files, *required_seed_files]


def bootstrap_canonical_schema_if_empty(
    *,
    dsn: str,
    root: Path | None = None,
) -> dict[str, Any]:
    """Install the canonical baseline only into a genuinely empty public schema."""
    validate_bootstrap_layout(root)
    conn = psycopg_runtime.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname = 'public'")
            row = cur.fetchone()
            public_tables = int((row or (0,))[0] or 0)
            cur.execute("SELECT to_regclass('public.accounts')")
            baseline_present = bool((cur.fetchone() or (None,))[0])
    finally:
        conn.close()
    if baseline_present:
        return {"status": "present", "public_tables": public_tables}
    if public_tables:
        raise RuntimeError(
            "The external database contains a partial or foreign public schema; "
            "refusing automatic baseline installation"
        )
    summary = reconcile_canonical_schema(
        connect=lambda: psycopg_runtime.connect(dsn),
        root=root,
    )
    return {"status": "installed", **summary}


def _resolve_sql_dsn() -> str:
    candidates = (
        CONFIG.get("fs_db_dsn", ""),
        CONFIG.get("langgraph_postgres_uri", ""),
        _runtime_env.get("DATABASE_URL", ""),
        _runtime_env.get("POSTGRES_DSN", ""),
    )
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value:
            return value
    return ""


def apply_clean_bootstrap(
    *,
    dsn: str | None = None,
    author: str = "system",
    root: Path | None = None,
) -> dict[str, dict[str, int]]:
    """Apply the checked-in seed snapshots for the canonical schema/seeds layout."""
    validate_bootstrap_layout(root)
    from core.application.tooling.db.bootstrap_seed import apply_bootstrap_seed

    return apply_bootstrap_seed(dsn=dsn, author=author)


def cleanup_retired_project_schema(*, dsn: str | None = None) -> dict[str, int]:
    """Remove retired schema columns and enforce current canonical constraints."""
    resolved_dsn = str(dsn or _resolve_sql_dsn() or "").strip()
    if not resolved_dsn:
        print("[SKIP] No Postgres DSN configured — skipping retired project schema cleanup")
        return {"skipped": 1}

    conn = psycopg_runtime.connect(resolved_dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'extension_installs'
                          AND column_name = 'runtime_status'
                    ) THEN
                        ALTER TABLE ONLY public.extension_installs
                        ADD COLUMN runtime_status text DEFAULT 'unknown'::text NOT NULL;
                    END IF;
                END $$;
                """
            )
            cur.execute(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'project_connections'
                          AND column_name = 'runtime_status'
                    ) THEN
                        ALTER TABLE ONLY public.project_connections
                        ADD COLUMN runtime_status text;
                    END IF;
                END $$;
                """
            )
            cur.execute("ALTER TABLE ONLY public.projects DROP COLUMN IF EXISTS db_type")
            cur.execute(
                "ALTER TABLE ONLY public.registry_skills DROP COLUMN IF EXISTS supported_role_types"
            )
            cur.execute(
                "ALTER TABLE ONLY public.registry_agents DROP COLUMN IF EXISTS agent_type_id"
            )
            cur.execute("DROP TABLE IF EXISTS public.agent_types")
            recategorized = 0
            for table in ("registry_agents", "registry_tools", "registry_skills"):
                cur.execute(
                    f"UPDATE public.{table} SET category = %s "  # nosec B608 - fixed table names
                    "WHERE category IS NULL OR NOT (category = ANY(%s))",
                    (DEFAULT_CATALOG_CATEGORY, sorted(CATALOG_CATEGORY_IDS)),
                )
                recategorized += max(cur.rowcount or 0, 0)
    finally:
        conn.close()

    print(
        "[OK] Retired project schema cleanup applied "
        "(dropped_db_type=1, "
        "ensured_extension_install_runtime_status=1, "
        "ensured_project_connection_runtime_status=1, "
        "dropped_agent_types=1, "
        f"recategorized_registry_rows={recategorized})"
    )
    return {
        "dropped_db_type": 1,
        "ensured_extension_install_runtime_status": 1,
        "ensured_project_connection_runtime_status": 1,
        "dropped_agent_types": 1,
        "recategorized_registry_rows": recategorized,
    }


def init_platform_observability_schema():
    """Create Flow Steward observability tables via the Flow Steward logger."""
    dsn = CONFIG.get("fs_db_dsn", "")
    if not dsn:
        print("[SKIP] FS_DB_DSN not set — skipping platform schema init")
        return
    from core.infrastructure.db.platform_stores import PlatformIdentityStore

    PlatformIdentityStore(dsn)
    print("[OK] Flow Steward observability schema ready (projects, platform_action_log)")


def backfill_registry_changelog() -> None:
    """Seed missing registry changelog entries from current live snapshots."""
    dsn = _resolve_sql_dsn()
    if not dsn:
        print("[SKIP] No Postgres DSN configured — skipping registry changelog backfill")
        return

    from core.application.registry.db_store import RegistryDBStore

    store = RegistryDBStore(dsn)

    def _existing_entity_ids(entity_type: str) -> set[str]:
        with store._cursor() as cur:
            cur.execute(
                "SELECT DISTINCT entity_id FROM registry_changelog WHERE entity_type = %s",
                (str(entity_type),),
            )
            rows = cur.fetchall() or []
        return {str(row["entity_id"]) for row in rows if row and row.get("entity_id") is not None}

    loaders = (
        (
            "agent",
            lambda: store.list_agents(enabled_only=False, include_trashed=True),
            "agent_id",
            lambda entity_id: store.get_agent(entity_id),
        ),
        (
            "tool",
            lambda: store.list_tools(enabled_only=False, include_trashed=True),
            "tool_id",
            lambda entity_id: store.get_tool(entity_id),
        ),
        (
            "skill",
            lambda: store.list_skills(enabled_only=False, include_trashed=True),
            "skill_id",
            lambda entity_id: store.get_skill(entity_id),
        ),
        (
            "workflow",
            lambda: store.list_workflows(enabled_only=False, include_trashed=True),
            "workflow_id",
            lambda entity_id: store.get_workflow(entity_id, include_trashed=True),
        ),
        (
            "guardrail",
            lambda: store.list_guardrails(
                enabled_only=False,
                include_trashed=True,
                account_id=DEFAULT_ACCOUNT_ID,
            ),
            "id",
            lambda entity_id: store.get_guardrail(
                str(entity_id),
                include_trashed=True,
                account_id=DEFAULT_ACCOUNT_ID,
            ),
        ),
    )

    inserted_total = 0
    for entity_type, list_fn, id_key, get_fn in loaders:
        existing_ids = _existing_entity_ids(entity_type)
        inserted = 0
        for row in list_fn() or []:
            entity_id = str((row or {}).get(id_key) or "").strip()
            if not entity_id or entity_id in existing_ids:
                continue
            snapshot = get_fn(entity_id)
            if not isinstance(snapshot, dict) or not snapshot:
                continue
            store.log_change(
                entity_type=entity_type,
                entity_id=entity_id,
                action="create",
                snapshot=snapshot,
                author="system",
            )
            existing_ids.add(entity_id)
            inserted += 1
        inserted_total += inserted
        if inserted:
            print(f"[OK] Backfilled {inserted} registry changelog entries for {entity_type}")

    if inserted_total == 0:
        print("[OK] Registry changelog backfill already current")


def init_langgraph_schema():
    """Create LangGraph PostgresSaver checkpoint tables."""
    uri = str(CONFIG.get("langgraph_postgres_uri") or "")
    if not uri:
        print("[SKIP] LANGGRAPH_POSTGRES_URI not set — skipping LangGraph schema init")
        return
    try:
        from langgraph.checkpoint.postgres import PostgresSaver

        with PostgresSaver.from_conn_string(uri) as saver:
            saver.setup()
        print(
            "[OK] LangGraph checkpoint schema ready (checkpoints, checkpoint_blobs, checkpoint_writes)"
        )
    except Exception as e:
        print(f"[ERROR] LangGraph schema init failed: {e}")
        raise


def init_workflow_knowledge_graph(*, dsn: str | None = None) -> dict[str, Any]:
    """Create the required empty AGE graph through an explicit maintenance path."""
    resolved_dsn = str(dsn or _resolve_sql_dsn() or "").strip()
    if not resolved_dsn:
        print("[SKIP] No Postgres DSN configured — skipping workflow knowledge graph init")
        return {"skipped": 1}

    from core.infrastructure.db.workflow_knowledge_store import (
        PostgresAgeWorkflowKnowledgeStore,
    )

    store = PostgresAgeWorkflowKnowledgeStore(dsn=resolved_dsn)
    store.create_graph_indexes()
    graph_status = dict(store.runtime_status().get("graph") or {})
    if not graph_status.get("available") or not graph_status.get("labels_ready"):
        error = str(graph_status.get("error") or "graph or labels are unavailable")
        raise RuntimeError(f"Workflow knowledge graph bootstrap failed: {error}")

    summary = {
        "graph_name": store.graph_name,
        "available": True,
        "labels_ready": True,
    }
    print(f"[OK] Workflow knowledge AGE graph ready (graph={store.graph_name}, labels_ready=true)")
    return summary


def init_extension_catalog() -> None:
    """Discover and sync extension bundles into DB catalog tables."""
    dsn = _resolve_sql_dsn()
    if not dsn:
        print("[SKIP] No Postgres DSN configured — skipping extension catalog sync")
        return

    from core.application.extensions.install import DBExtensionInstallStore
    from core.infrastructure.db.platform_stores import PlatformIdentityStore
    from core.infrastructure.extension_runtime.bundle_cleanup import (
        reconcile_cleanup_journal,
    )
    from core.infrastructure.extension_runtime.extension_runtime import (
        default_extensions_root,
        scan_extensions,
    )
    from core.infrastructure.extension_runtime.lifecycle_lock import (
        extension_lifecycle_lock,
    )
    from core.infrastructure.extension_runtime.publication_recovery import (
        recover_managed_release_publications,
    )

    root = str(default_extensions_root() or "").strip()
    logger = PlatformIdentityStore(dsn)
    if not hasattr(logger, "sync_extension_scan_records"):
        raise RuntimeError("Extension catalog synchronization is unavailable in this runtime")
    if not hasattr(logger, "list_extensions"):
        raise RuntimeError("Extension catalog publication recovery is unavailable in this runtime")
    conn = getattr(logger, "conn", None)
    if conn is None:
        raise RuntimeError("Extension catalog cleanup reconciliation is unavailable")
    with extension_lifecycle_lock(connection=conn) as acquired:
        if not acquired:
            raise RuntimeError("Extension catalog lifecycle reconciliation is already in progress")
        install_store = DBExtensionInstallStore(conn)
        extensions = logger.list_extensions(enabled_only=False) or []
        recovered = recover_managed_release_publications(
            extensions_root=Path(root),
            extensions=extensions,
        )
        scanned = scan_extensions(root)
        from core.application.extensions.extension_scan_lifecycle import (
            sync_extension_scan_with_llm_lifecycle,
        )

        synced = sync_extension_scan_with_llm_lifecycle(logger, scanned)
        cleanup = reconcile_cleanup_journal(
            extensions_root=Path(root),
            discard_installed_intents=True,
            discovered_extension_ids={
                str(item.get("extension_key") or "").strip()
                for item in scanned
                if isinstance(item, dict) and str(item.get("extension_key") or "").strip()
            },
            is_installed=lambda extension_id, account_id: (
                install_store.get(extension_id=extension_id, account_id=account_id) is not None
            ),
        )
        if cleanup.pending:
            details = "; ".join(cleanup.errors)
            suffix = f": {details}" if details else ""
            raise RuntimeError(f"Extension cleanup reconciliation has pending intents{suffix}")
    print(
        "[OK] Extension catalog synced "
        f"(root={root}, cleanup={cleanup.completed}, recovered={recovered}, "
        f"scanned={len(scanned)}, stored={len(list(synced or []))})"
    )


def initialize_database() -> dict[str, Any]:
    """Finalize database initialization after Postgres loads db/schema/*.sql."""
    validate_bootstrap_layout()
    init_platform_observability_schema()
    dsn = _resolve_sql_dsn()
    summary: dict[str, Any] = {}
    if dsn:
        # A clean 1.0 schema can still have post-1.0 migrations pending. Apply
        # them before canonical seeds use repositories that intentionally fail
        # closed when a required current-schema column is absent.
        summary["post_baseline_migrations"] = migration_runner.run_pending_migrations(dsn=dsn)
    summary.update(
        apply_clean_bootstrap(
            dsn=dsn,
            author="system",
        )
    )
    for group, counts in summary.items():
        rendered = ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))
        print(f"[OK] Applied {group} bootstrap seed: {rendered}")
    summary["schema_cleanup"] = cleanup_retired_project_schema(dsn=dsn)
    summary["workflow_knowledge_graph"] = init_workflow_knowledge_graph(dsn=dsn)
    init_extension_catalog()
    init_langgraph_schema()
    if dsn:
        conn = psycopg_runtime.connect(dsn)
        conn.autocommit = True
        try:
            record_canonical_schema_state(
                conn,
                applied_by="system",
                metadata={
                    "source": "flow-steward database bootstrap-seed",
                    "seed_fingerprint": canonical_seed_fingerprint(),
                },
            )
        finally:
            conn.close()
        summary["schema_state"] = {"updated": 1}
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Initialize Flow Steward platform database schemas."
    )
    parser.parse_args()
    print("Initializing platform database schemas...")
    initialize_database()
    print("\nDatabase initialization complete.")
    print("You can now run the orchestration graph:")
    print(
        "  docker compose -f infra/docker/docker-compose.yaml exec "
        "fs-app python -c \"from core.application.orchestration.graph_runtime import build_registry_graph; print('Graph OK:', "
        'build_registry_graph())"'
    )
