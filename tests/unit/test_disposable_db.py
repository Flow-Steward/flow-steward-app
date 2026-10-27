from __future__ import annotations

import pytest

from tests import _disposable_db
from tests.conftest import disposable_db

AUDIT_DATABASE_URI = "postgresql://audit:audit@pg-audit:5432/flow_steward_audit"  # pragma: allowlist secret - public disposable audit database fixture


def test_published_host_uses_explicit_container_reachability_override(monkeypatch) -> None:
    monkeypatch.setenv("FS_TEST_DOCKER_PUBLISHED_HOST", "host.docker.internal")

    assert _disposable_db.published_host() == "host.docker.internal"


def test_published_host_defaults_to_localhost(monkeypatch) -> None:
    monkeypatch.delenv("FS_TEST_DOCKER_PUBLISHED_HOST", raising=False)

    assert _disposable_db.published_host() == "127.0.0.1"


@pytest.fixture
def audit_environment(monkeypatch):
    monkeypatch.setenv("FS_AUDIT_ISOLATED", "1")
    monkeypatch.delenv("FS_TEST_DISPOSABLE_DB", raising=False)
    monkeypatch.setenv(
        "FS_DB_DSN", "host=pg-audit port=5432 dbname=flow_steward_audit user=audit password=audit"
    )
    monkeypatch.setenv("DATABASE_URL", AUDIT_DATABASE_URI)
    monkeypatch.setenv("LANGGRAPH_POSTGRES_URI", AUDIT_DATABASE_URI)
    for key in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(_disposable_db.shutil, "which", lambda _command: None)


def test_gate_owned_disposable_db_needs_no_nested_docker(audit_environment, monkeypatch):
    def unexpected_docker(*_args, **_kwargs):
        pytest.fail("The fixture must not start, reset, or remove its gate-owned database")

    monkeypatch.setattr(_disposable_db, "start_disposable_db", unexpected_docker)
    monkeypatch.setattr(_disposable_db, "apply_canonical_state", unexpected_docker)
    monkeypatch.setattr(_disposable_db, "stop_disposable_db", unexpected_docker)
    # The fixture skips only when it finds no gate-owned database; an isolated
    # gate must execute PostgreSQL tests, so that precondition is asserted first.
    assert _disposable_db.isolated_audit_database() is not None
    fixture = disposable_db.__wrapped__()
    database = next(fixture)
    assert "host=pg-audit" in database.dsn
    assert "dbname=flow_steward_audit" in database.dsn
    assert "user=audit" in database.dsn
    assert database.uri == f"{AUDIT_DATABASE_URI}?sslmode=disable"
    with pytest.raises(StopIteration):
        next(fixture)


@pytest.mark.parametrize(
    "dsn",
    [
        "host=pg-fs dbname=flow_steward user=flow_steward",
        "host=pg-audit dbname=flow_steward user=audit",
        "host=pg-audit dbname=flow_steward_audit user=postgres",
        "host=pg-audit port=5433 dbname=flow_steward_audit user=audit",
        "host=pg-audit hostaddr=127.0.0.1 dbname=flow_steward_audit user=audit",
        "service=production host=pg-audit dbname=flow_steward_audit user=audit",
        "not a DSN",
        "",
    ],
)
def test_isolated_gate_refuses_unsafe_database_instead_of_skipping(
    audit_environment, monkeypatch, dsn
):
    monkeypatch.setenv("FS_DB_DSN", dsn)
    fixture_generator = disposable_db.__wrapped__()
    with pytest.raises(ValueError, match="audit database"):
        next(fixture_generator)


@pytest.mark.parametrize(
    "key", ["DATABASE_URL", "LANGGRAPH_POSTGRES_URI", "PGHOSTADDR", "PGSERVICE", "PGOPTIONS"]
)
def test_isolated_gate_refuses_conflicting_connection_environment(
    audit_environment, monkeypatch, key
):
    monkeypatch.setenv(key, "postgresql://prod@pg-fs/flow_steward")
    fixture_generator = disposable_db.__wrapped__()
    with pytest.raises(ValueError, match="audit database"):
        next(fixture_generator)


def test_unmarked_environment_cannot_reuse_existing_database(audit_environment, monkeypatch):
    monkeypatch.delenv("FS_AUDIT_ISOLATED")
    fixture_generator = disposable_db.__wrapped__()
    with pytest.raises(pytest.skip.Exception, match="docker is not on PATH"):
        next(fixture_generator)
