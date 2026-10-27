from __future__ import annotations

from core.application.auth.first_admin import FirstAdminAlreadyExists, FirstAdminService
from core.application.auth.first_admin_repository import PostgresFirstAdminRepository
from core.infrastructure.db.db_pool import PooledConnectionProxy, transaction_connection


class _Repository:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def create_if_empty(self, *, email, password_hash, display_name):
        self.calls.append((email, password_hash, display_name))
        return self.result


def test_first_admin_service_uses_single_atomic_repository_operation() -> None:
    repository = _Repository({"id": "1", "username": "admin@example.com", "display_name": "Admin"})
    service = FirstAdminService(repository=repository, password_hash=lambda value: f"hash:{value}")

    result = service.create(
        email="admin@example.com", password="strong-password", display_name="Admin"
    )

    assert result["id"] == "1"
    assert repository.calls == [("admin@example.com", "hash:strong-password", "Admin")]


def test_first_admin_service_reports_atomic_conflict() -> None:
    service = FirstAdminService(repository=_Repository(None), password_hash=lambda value: value)

    try:
        service.create(email="second@example.com", password="strong-password", display_name="")
    except FirstAdminAlreadyExists:
        pass
    else:
        raise AssertionError("zero-user invariant must fail closed")


def test_postgres_repository_locks_before_count_inside_default_pool_transaction() -> None:
    class Cursor:
        def __init__(self):
            self.queries = []
            self.rows = iter(
                (
                    (0,),
                    ("1", "admin@example.com", "Admin"),
                    ("role-1",),
                    ("account-1",),
                )
            )

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            self.queries.append((" ".join(str(query).split()), params))

        def fetchone(self):
            return next(self.rows)

    cursor = Cursor()

    class Connection:
        closed = 0

        def __init__(self):
            self.autocommit = True
            self.commits = 0
            self.rollbacks = 0

        def cursor(self):
            assert self.autocommit is False
            return cursor

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

    connection = Connection()

    class Pool:
        def getconn(self):
            return connection

        def putconn(self, returned, *, close=False):
            assert returned is connection
            assert close is False

    logger = type("Logger", (), {"conn": PooledConnectionProxy(Pool())})()
    repository = PostgresFirstAdminRepository(
        logger=logger,
        seed_default_roles=lambda _logger: None,
        transaction_connection_factory=transaction_connection,
    )

    result = repository.create_if_empty(
        email="admin@example.com", password_hash="hash", display_name="Admin"
    )

    assert result is not None
    assert "pg_advisory_xact_lock" in cursor.queries[0][0]
    assert cursor.queries[1][0] == "SELECT COUNT(*) FROM users"
    assert any(
        "INSERT INTO user_account_memberships" in query
        and params == ("1", "account-1")
        for query, params in cursor.queries
    )
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_first_admin_database_composition_lives_in_runtime_wiring() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    controller = (root / "core/entrypoints/api/auth_sessions.py").read_text(encoding="utf-8")
    runtime = (root / "core/entrypoints/api/first_admin_runtime.py").read_text(encoding="utf-8")
    routes = (root / "core/entrypoints/api/route_registration.py").read_text(encoding="utf-8")

    assert not (root / "core/infrastructure/auth/first_admin_repository.py").exists()
    assert "PostgresFirstAdminRepository" not in controller
    assert "PostgresFirstAdminRepository" in runtime
    assert "first_admin_service_factory" in routes
