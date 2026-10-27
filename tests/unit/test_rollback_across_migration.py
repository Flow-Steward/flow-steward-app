"""AC17-R159 acceptance, condition 3: a rollback across a migration is refused by name.

Rolling back from a release that carried a migration means older code on a newer
schema. The migration check compared this release's files with the ledger only, so a
migration applied by the newer release -- absent from this one's files -- was neither
pending nor drifted, and the installer would have started the older code on it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.application.tooling.db import migration_runner, upgrade_db

_SQL = "-- transaction: true\nCREATE TABLE t (id int);\n"


def _files(root: Path, *names: str) -> None:
    directory = root / "db" / "migrations"
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_text(_SQL, encoding="utf-8")


def test_a_migration_the_newer_release_applied_is_named(tmp_path, monkeypatch) -> None:
    _files(tmp_path, "20260101000000_shared.sql")
    checksum = migration_runner.migration_checksum(_SQL)
    applied = {"20260101000000_shared.sql": checksum, "20260201000000_newer.sql": checksum}
    monkeypatch.setattr(migration_runner, "fetch_applied_migrations", lambda _conn: applied)

    assert migration_runner.applied_unknown_migrations(object(), root=tmp_path) == [
        "20260201000000_newer.sql"
    ]


def test_a_database_this_release_fully_knows_has_nothing_unknown(tmp_path, monkeypatch) -> None:
    _files(tmp_path, "20260101000000_shared.sql", "20260201000000_newer.sql")
    checksum = migration_runner.migration_checksum(_SQL)
    monkeypatch.setattr(
        migration_runner,
        "fetch_applied_migrations",
        lambda _conn: {"20260101000000_shared.sql": checksum},
    )

    assert migration_runner.applied_unknown_migrations(object(), root=tmp_path) == []


def test_the_installer_baseline_refuses_a_database_a_newer_release_upgraded(monkeypatch) -> None:
    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
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
    monkeypatch.setattr(upgrade_db.migration_runner, "migrations_table_exists", lambda _c: True)
    monkeypatch.setattr(
        upgrade_db.migration_runner,
        "inspect_migrations",
        lambda _conn: {"pending": [], "drifted": []},  # all of this release's files applied
    )
    monkeypatch.setattr(
        upgrade_db.migration_runner,
        "applied_unknown_migrations",
        lambda _conn: ["20261201000000_newer.sql"],
    )

    with pytest.raises(RuntimeError) as raised:
        upgrade_db.baseline_migration_history()

    message = str(raised.value)
    assert "this release does not know: 20261201000000_newer.sql" in message
    assert "backup" in message
    assert "pre-release" not in message  # a post-1.0 migration is a rollback, not pre-1.0


def test_a_refused_baseline_prints_its_reason_not_a_traceback(monkeypatch, capsys) -> None:
    """AC17-R160: the operator read a Python traceback where the refusal should be."""
    import sys

    def refused(**_kwargs):
        raise RuntimeError("Migration history already exists but does not match this release")

    monkeypatch.setattr(sys, "argv", ["upgrade_db", "--baseline-migration-history"])
    source = Path(upgrade_db.__file__).read_text(encoding="utf-8")
    namespace = dict(vars(upgrade_db))
    namespace.update(__name__="__main__", baseline_migration_history=refused)
    main_block = source[source.index('if __name__ == "__main__":') :]

    code = compile(main_block, upgrade_db.__file__, "exec")
    with pytest.raises(SystemExit) as exited:
        exec(code, namespace)

    assert exited.value.code == 2
    err = capsys.readouterr().err
    assert err.strip() == "Migration history already exists but does not match this release"
    assert "Traceback" not in err
