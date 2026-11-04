"""Incomplete release artifacts must stop before database migrations can mutate state."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from core.application.tooling.db import migration_runner, upgrade_db, upgrade_preflight

_BASELINE = "a" * 64
_REQUIRED_SQL = "20261103090000_secret_catalog.sql"
_UNLISTED_SQL = "20261104000000_unlisted.sql"
_DAMAGES = ("missing_sql", "checksum", "missing_manifest", "unlisted_sql")


class _ReadOnlyConnection:
    """Answer the real preflight's reads while refusing any mutating SQL."""

    def __init__(self, *, ledger_exists: bool = True) -> None:
        self.autocommit = True
        self.ledger_exists = ledger_exists
        self.rows: list[tuple[object, ...]] = []

    def cursor(self) -> _ReadOnlyConnection:
        return self

    def __enter__(self) -> _ReadOnlyConnection:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
        statement = " ".join(sql.split())
        assert statement.startswith(("SELECT", "WITH")), "artifact refusal reached mutating SQL"
        if "to_regclass" in statement:
            relation = str(params[0]) if params else statement
            exists = (
                False
                if "platform_upgrade_phases" in relation
                else self.ledger_exists
                if "platform_schema_migrations" in relation
                else True
            )
            value = exists if "IS NOT NULL" in statement else relation if exists else None
            self.rows = [(value,)]
        elif any(
            name in statement
            for name in ("pg_try_advisory_lock", "pg_advisory_lock", "pg_advisory_unlock")
        ):
            self.rows = [(True,)]
        elif "SELECT schema_fingerprint" in statement:
            self.rows = [(_BASELINE,)]
        elif (
            "SELECT migration_id, checksum" in statement
            or "pg_stat_activity" in statement
            or statement.startswith("WITH refs AS")
        ):
            self.rows = []
        else:
            pytest.fail(f"unexpected preflight query: {statement[:100]}")

    def fetchone(self) -> tuple[object, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows

    def close(self) -> None:
        pass


@pytest.fixture
def release_artifacts(tmp_path: Path) -> Path:
    migrations = tmp_path / "db/migrations"
    migrations.mkdir(parents=True)
    body = "CREATE TABLE IF NOT EXISTS artifact_probe (id integer);\n"
    (migrations / _REQUIRED_SQL).write_text(body)
    (tmp_path / "db/released_migrations.json").write_text(
        json.dumps(
            {
                "baseline_schema_fingerprint": _BASELINE,
                "released_baseline_fingerprints": [_BASELINE],
                "migrations": {_REQUIRED_SQL: hashlib.sha256(body.encode()).hexdigest()},
            }
        )
    )
    schema = tmp_path / "db/schema"
    schema.mkdir()
    (schema / "001_probe.sql").write_text("CREATE TABLE bootstrap_probe (id integer);\n")
    seeds = tmp_path / "db/seeds"
    seeds.mkdir()
    for name in ("lookup_seed.json", "registry_seed.json"):
        (seeds / name).write_text("{}\n")
    return tmp_path


def _damage(root: Path, damage: str) -> str:
    if damage == "missing_sql":
        (root / "db/migrations" / _REQUIRED_SQL).unlink()
        return _REQUIRED_SQL
    if damage == "checksum":
        (root / "db/migrations" / _REQUIRED_SQL).write_text("SELECT 'changed release bytes';\n")
        return _REQUIRED_SQL
    if damage == "missing_manifest":
        (root / "db/released_migrations.json").unlink()
        return "released_migrations.json"
    if damage == "unlisted_sql":
        (root / "db/migrations" / _UNLISTED_SQL).write_text("SELECT 1;\n")
        return _UNLISTED_SQL
    raise AssertionError(damage)


@pytest.mark.parametrize("damage", _DAMAGES)
def test_preflight_reports_release_artifact_failure(release_artifacts: Path, damage: str) -> None:
    filename = _damage(release_artifacts, damage)

    report = upgrade_preflight.preflight(_ReadOnlyConnection(), root=release_artifacts)

    assert report["blocking"], "incomplete release artifacts were reported as safe to migrate"
    assert filename in json.dumps(report["blocking"]), (
        "the refusal must identify the broken artifact"
    )


@pytest.mark.parametrize("damage", _DAMAGES)
def test_upgrade_refuses_broken_release_before_any_migration_mutation(
    release_artifacts: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    filename = _damage(release_artifacts, damage)
    monkeypatch.setattr(upgrade_preflight, "_ROOT", release_artifacts)
    monkeypatch.setattr(migration_runner, "REPO_ROOT", release_artifacts)
    original_layout = upgrade_db.init_db.validate_bootstrap_layout
    monkeypatch.setattr(
        upgrade_db.init_db, "validate_bootstrap_layout", lambda: original_layout(release_artifacts)
    )
    monkeypatch.setattr(upgrade_db.psycopg_runtime, "connect", lambda _dsn: _ReadOnlyConnection())
    monkeypatch.setattr(upgrade_db.vector_lifecycle, "require_pgvector_image", lambda _conn: None)

    def mutation_is_forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("broken release artifacts reached database mutation")

    monkeypatch.setattr(migration_runner, "apply_pending_migrations", mutation_is_forbidden)
    monkeypatch.setattr(upgrade_db.init_db, "apply_clean_bootstrap", mutation_is_forbidden)
    monkeypatch.setattr(upgrade_db.upgrade_phases, "run_pending_phases", mutation_is_forbidden)

    with pytest.raises(RuntimeError) as captured:
        upgrade_db.upgrade_database(dsn="unit-test", maintenance_confirmed=False)

    assert filename in str(captured.value), (
        "the operator needs the exact missing or changed artifact"
    )


def test_complete_release_artifacts_leave_supported_preflight_unblocked(
    release_artifacts: Path,
) -> None:
    report = upgrade_preflight.preflight(_ReadOnlyConnection(), root=release_artifacts)

    assert report["blocking"] == []
    assert report["pending_migrations"] == [_REQUIRED_SQL]


def test_installer_baseline_refuses_missing_release_sql_before_any_migration_mutation(
    release_artifacts: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filename = _damage(release_artifacts, "missing_sql")
    monkeypatch.setattr(upgrade_preflight, "_ROOT", release_artifacts)
    monkeypatch.setattr(migration_runner, "REPO_ROOT", release_artifacts)
    original_layout = upgrade_db.init_db.validate_bootstrap_layout
    monkeypatch.setattr(
        upgrade_db.init_db, "validate_bootstrap_layout", lambda: original_layout(release_artifacts)
    )
    monkeypatch.setattr(
        upgrade_db.psycopg_runtime, "connect", lambda _dsn: _ReadOnlyConnection(ledger_exists=False)
    )
    monkeypatch.setattr(
        upgrade_db, "enforce_canonical_baseline_contract_connection", lambda *_args, **_kwargs: None
    )

    def mutation_is_forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("installer baseline reached migration mutation with a missing release artifact")

    monkeypatch.setattr(migration_runner, "apply_pending_migrations", mutation_is_forbidden)

    with pytest.raises(RuntimeError) as captured:
        upgrade_db.baseline_migration_history(dsn="unit-test")

    assert filename in str(captured.value)
