"""Released migrations are immutable, and a database that recorded one still starts.

Editing a migration that has already shipped changes its checksum. Every database
that recorded the old one then fails startup closed, because the runner refuses to
re-run an edited migration. `db/released_migrations.json` records what shipped so
that CI catches the edit in the pull request instead of an operator catching it
during an upgrade.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.application.tooling.db import migration_runner

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "db" / "released_migrations.json"
PROBE_MIGRATION = "20990101000000_released_probe.sql"


def _released() -> dict[str, str]:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return dict(payload["migrations"])


def test_released_migrations_still_have_their_recorded_checksum() -> None:
    released = _released()
    on_disk = {
        path.name: migration_runner.migration_checksum(path.read_text(encoding="utf-8"))
        for path in migration_runner.discover_migration_files(ROOT)
    }

    missing = sorted(set(released) - set(on_disk))
    assert not missing, (
        f"released migrations are gone from db/migrations: {missing}. "
        "A shipped migration cannot be removed; databases that applied it will "
        "never converge with a tree that no longer contains it."
    )

    edited = sorted(name for name, checksum in released.items() if on_disk[name] != checksum)
    assert not edited, (
        f"released migrations were edited in place: {edited}. Every database that "
        "recorded the previous checksum fails startup closed. Revert the edit and "
        "add a new forward-only migration instead."
    )


def test_every_migration_on_disk_is_recorded_as_released() -> None:
    released = _released()
    unrecorded = sorted(
        path.name
        for path in migration_runner.discover_migration_files(ROOT)
        if path.name not in released
    )
    assert not unrecorded, (
        f"migrations are not recorded in {MANIFEST.name}: {unrecorded}. Add each one "
        "with its checksum in the same commit that adds the migration, so a later "
        "in-place edit is caught."
    )


def test_a_database_that_recorded_the_released_checksums_needs_no_ledger_edit() -> None:
    """OPS-001: the fail-closed upgrade a shipped database must not hit."""
    released = _released()
    conn = _LedgerOnlyConn(released)

    result = migration_runner.apply_pending_migrations(conn, root=ROOT)

    assert result["applied"] == []
    assert sorted(result["skipped"]) == sorted(released)


def test_editing_a_released_migration_still_fails_the_upgrade_closed(tmp_path: Path) -> None:
    """The guard above replaces nothing: drift must still stop an upgrade."""
    migrations = tmp_path / "db" / "migrations"
    migrations.mkdir(parents=True)
    (migrations / PROBE_MIGRATION).write_text("SELECT 1;\n", encoding="utf-8")
    conn = _LedgerOnlyConn({PROBE_MIGRATION: "0" * 64})

    with pytest.raises(migration_runner.MigrationChecksumError) as excinfo:
        migration_runner.apply_pending_migrations(conn, root=tmp_path)

    assert PROBE_MIGRATION in str(excinfo.value)


class _LedgerOnlyCursor:
    """Answers the runner's ledger queries; any migration body would be a failure."""

    def __init__(self, applied: dict[str, str]) -> None:
        self._applied = applied
        self._result: list | None = None

    def __enter__(self) -> _LedgerOnlyCursor:
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def execute(self, sql: str, params=None) -> None:
        if "pg_advisory" in sql or "CREATE TABLE IF NOT EXISTS" in sql:
            return
        if "to_regclass('public.platform_schema_migrations')" in sql:
            self._result = [(True,)]
            return
        if sql.strip().upper().startswith("SELECT MIGRATION_ID"):
            self._result = list(self._applied.items())
            return
        if sql.strip().upper().startswith(("SET LOCAL SEARCH_PATH", "SET SEARCH_PATH", "RESET")):
            # The runner's own search-path guard, not a migration body. The
            # assertion below is about migration SQL re-running, and this is the
            # statement that makes migration SQL resolve the same way every time.
            return
        raise AssertionError(f"no migration body should run here, got: {sql[:60]!r}")

    def fetchall(self):
        return self._result

    def fetchone(self):
        rows = self._result or []
        return rows[0] if rows else None


class _LedgerOnlyConn:
    def __init__(self, applied: dict[str, str]) -> None:
        self.autocommit = True
        self._applied = applied

    def cursor(self) -> _LedgerOnlyCursor:
        return _LedgerOnlyCursor(self._applied)

    def commit(self) -> None:  # pragma: no cover - nothing is written
        raise AssertionError("an up-to-date database must not be written to")

    def rollback(self) -> None:  # pragma: no cover - nothing is written
        raise AssertionError("an up-to-date database must not be written to")
