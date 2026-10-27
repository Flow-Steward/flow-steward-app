"""ADR 0037: each pytest-xdist worker runs against its own copy of the audit database."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests import _disposable_db, _xdist_isolation

AUDIT_DSN = "host=pg-audit port=5432 dbname=flow_steward_audit user=audit password=audit"  # pragma: allowlist secret - public disposable audit database fixture
AUDIT_URI = "postgresql://audit:audit@pg-audit:5432/flow_steward_audit"  # pragma: allowlist secret - public disposable audit database fixture


@pytest.fixture
def worker_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(_xdist_isolation, "_provisioned", None)
    monkeypatch.setenv("FS_AUDIT_ISOLATED", "1")
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
    monkeypatch.setenv("FS_DB_DSN", AUDIT_DSN)
    monkeypatch.setenv("DATABASE_URL", AUDIT_URI)
    monkeypatch.setenv("LANGGRAPH_POSTGRES_URI", AUDIT_URI)
    monkeypatch.setenv("POSTGRES_DB", "flow_steward_audit")
    monkeypatch.setenv("ARTIFACT_LOCAL_ROOT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("FS_ARTIFACT_ROOT", str(tmp_path / "ops"))
    created: list[tuple[str, str]] = []
    monkeypatch.setattr(
        _xdist_isolation,
        "_create_from_template",
        lambda admin_dsn, database: created.append((admin_dsn, database)),
    )
    return created


def test_worker_gets_its_own_database_and_scratch_roots(worker_environment, tmp_path) -> None:
    _xdist_isolation.isolate_worker()

    [(admin_dsn, database)] = worker_environment
    assert database == "flow_steward_audit_gw3"
    assert "dbname=postgres" in admin_dsn
    assert "dbname=flow_steward_audit_gw3" in os.environ["FS_DB_DSN"]
    assert "host=pg-audit" in os.environ["FS_DB_DSN"]
    assert os.environ["DATABASE_URL"] == AUDIT_URI + "_gw3"
    assert os.environ["LANGGRAPH_POSTGRES_URI"] == AUDIT_URI + "_gw3"
    assert os.environ["POSTGRES_DB"] == "flow_steward_audit_gw3"
    assert os.environ["ARTIFACT_LOCAL_ROOT_DIR"] == str(tmp_path / "artifacts" / "gw3")
    assert (tmp_path / "ops" / "gw3").is_dir()


def test_nested_run_reuses_the_parent_workers_database(worker_environment, monkeypatch) -> None:
    """A pytest subprocess inherits the redirect and must not drop its parent's copy."""
    _xdist_isolation.isolate_worker()
    redirected = os.environ["FS_DB_DSN"]
    monkeypatch.setattr(_xdist_isolation, "_provisioned", None)

    _xdist_isolation.isolate_worker()

    assert len(worker_environment) == 1
    assert os.environ["FS_DB_DSN"] == redirected
    assert _xdist_isolation._provisioned is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("FS_DB_DSN", "host=pg-fs port=5432 dbname=flow_steward user=flow_steward"),
        ("FS_DB_DSN", "host=pg-audit port=5432 dbname=flow_steward user=audit"),
        ("DATABASE_URL", "postgresql://prod@pg-fs/flow_steward"),
    ],
)
def test_worker_refuses_a_database_that_is_not_the_audit_database(
    worker_environment, monkeypatch, key, value
) -> None:
    monkeypatch.setenv(key, value)

    with pytest.raises(RuntimeError, match="Refusing to parallelise"):
        _xdist_isolation.isolate_worker()
    assert worker_environment == []


@pytest.mark.parametrize("unset", ["PYTEST_XDIST_WORKER", "FS_AUDIT_ISOLATED"])
def test_serial_or_unisolated_runs_are_untouched(worker_environment, monkeypatch, unset) -> None:
    monkeypatch.delenv(unset)

    _xdist_isolation.isolate_worker()

    assert worker_environment == []
    assert os.environ["FS_DB_DSN"] == AUDIT_DSN
    assert _xdist_isolation.audit_database_name() == "flow_steward_audit"


def test_malformed_worker_id_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0; DROP DATABASE x")

    with pytest.raises(RuntimeError, match="worker id"):
        _xdist_isolation.xdist_worker()


@pytest.mark.parametrize(
    ("database", "accepted"),
    [
        ("flow_steward_audit", True),
        ("flow_steward_audit_gw3", True),
        ("flow_steward_audit_gw2", False),
        ("flow_steward", False),
    ],
)
def test_audit_guard_accepts_only_the_shared_name_and_this_workers_copy(
    monkeypatch, database, accepted
) -> None:
    monkeypatch.setenv("FS_AUDIT_ISOLATED", "1")
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
    dsn = f"host=pg-audit port=5432 dbname={database} user=audit password=audit"  # pragma: allowlist secret - public disposable audit database fixture

    if accepted:
        assert _disposable_db._audit_connection_info(dsn)["dbname"] == database
    else:
        with pytest.raises(ValueError, match="audit database"):
            _disposable_db._audit_connection_info(dsn)


def test_shared_cache_groups_name_existing_modules() -> None:
    """A renamed module would silently fall out of its group and rebuild the cache."""
    from tests.unit import conftest as unit_conftest

    for group, modules in unit_conftest._SHARED_CACHE_GROUPS.items():
        for module in modules:
            path = unit_conftest._UNIT_ROOT / module
            assert path.is_file(), (group, module)
            assert unit_conftest._shared_cache_group(path) == group
    assert unit_conftest._shared_cache_group(Path(__file__)) is None


def test_a_module_importing_a_cache_owner_joins_its_group() -> None:
    import types

    from tests.unit import conftest as unit_conftest

    scan = types.ModuleType("probe_scan")
    from tests.support import source_index

    scan.source_index = source_index
    assert unit_conftest._imported_cache_group(scan) == "source-scans"
    unrelated = types.ModuleType("probe_unrelated")
    unrelated.Path = Path
    assert unit_conftest._imported_cache_group(unrelated) is None
