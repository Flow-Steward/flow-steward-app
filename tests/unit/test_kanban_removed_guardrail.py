"""
Kanban removal regression guardrail.

The Kanban feature (kanban boards/tasks/columns) was created by the developer
agent for a test-project story and mistakenly landed in the Flow Steward system tree.
It was removed as a P0 cleanup.  These tests enforce that it does not reappear.

Run:
    docker compose exec fs-app python -m pytest \
        tests/unit/test_kanban_removed_guardrail.py -v
"""

from __future__ import annotations

from pathlib import Path

from core.infrastructure.db.schema_state import read_canonical_schema_sql

ROOT = Path(__file__).parent.parent.parent


# ---------------------------------------------------------------------------
# 1. No kanban directory in the project root
# ---------------------------------------------------------------------------


def test_kanban_module_directory_does_not_exist():
    """The kanban/ Python module must not re-appear at project root."""
    assert not (ROOT / "kanban").exists(), (
        "kanban/ directory found — Kanban was removed as a P0 cleanup "
        "(test-project story leaked into Flow Steward system tree). "
        "Do not re-add kanban/ without an explicit product decision."
    )


# ---------------------------------------------------------------------------
# 2. No kanban templates
# ---------------------------------------------------------------------------


def test_kanban_templates_directory_does_not_exist():
    """The templates/kanban/ directory must not re-appear."""
    assert not (ROOT / "templates" / "kanban").exists(), (
        "templates/kanban/ found — Kanban templates were removed. "
        "Do not re-add without an explicit product decision."
    )


# ---------------------------------------------------------------------------
# 3. No kanban route mounted in the canonical API entrypoint
# ---------------------------------------------------------------------------


def test_api_entrypoint_does_not_mount_kanban_router():
    """The canonical API entrypoint must not import or mount a kanban router."""
    api_sources = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROOT / "core/entrypoints/api").rglob("*.py")
    )
    assert "kanban" not in api_sources.lower(), (
        "The canonical API entrypoint still references 'kanban'."
    )


# ---------------------------------------------------------------------------
# 4. No kanban tables in the canonical bootstrap schema
# ---------------------------------------------------------------------------


def test_bootstrap_schema_does_not_contain_kanban_tables():
    """db/schema/*.sql must not contain kanban table definitions."""
    content = read_canonical_schema_sql()
    kanban_markers = ["kanban_boards", "kanban_columns", "kanban_tasks", "kanban_task_activity"]
    found = [m for m in kanban_markers if m in content]
    assert not found, (
        f"Schema contains kanban table markers: {found}. "
        "Kanban schema was removed; do not re-add it."
    )


# ---------------------------------------------------------------------------
# 5. No kanban migration file
# ---------------------------------------------------------------------------


def test_kanban_migration_file_does_not_exist():
    """migrations/116_kanban_task_tracker.sql must not re-appear."""
    migration = ROOT / "migrations" / "116_kanban_task_tracker.sql"
    assert not migration.exists(), (
        "migrations/116_kanban_task_tracker.sql re-appeared. "
        "The migrations/ directory is also banned after the clean-bootstrap cutover."
    )


def test_migrations_directory_does_not_exist():
    """The migrations/ directory must not re-appear after the clean-bootstrap cutover."""
    migrations_dir = ROOT / "migrations"
    assert not migrations_dir.exists(), (
        "migrations/ directory re-appeared. After the clean-bootstrap cutover, "
        "all schema changes go into db/schema/*.sql. "
        "See core/application/tooling/db/init_db.py validate_bootstrap_layout() for the enforced policy."
    )
