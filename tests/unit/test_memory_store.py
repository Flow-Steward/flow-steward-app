from __future__ import annotations

from types import SimpleNamespace

from core.infrastructure.db.memory_store import PostgresMemoryRepository


class _FakeConn:
    def __init__(self, *, autocommit: bool = True) -> None:
        self.autocommit = autocommit
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


def test_transaction_uses_dedicated_connection_for_autocommit_logger(monkeypatch) -> None:
    shared_conn = _FakeConn(autocommit=True)
    tx_conn = _FakeConn(autocommit=True)
    audit_calls: list[object] = []

    monkeypatch.setattr(
        "core.infrastructure.db.memory_store.psycopg_runtime.connect",
        lambda dsn: tx_conn,
    )
    monkeypatch.setattr(
        "core.infrastructure.db.memory_store.action_log_store.log_action",
        lambda **kwargs: audit_calls.append(kwargs["conn"]),
    )

    repo = PostgresMemoryRepository(
        logger_instance=SimpleNamespace(
            conn=shared_conn,
            dsn="postgresql://memory-test",
            auto_commit=True,
        )
    )

    with repo.transaction():
        assert repo._current_conn() is tx_conn
        repo.record_audit_event(
            account_id="acc-1",
            actor_id="user-1",
            action_type="memory_write_candidate",
        )

    assert audit_calls == [tx_conn]
    assert tx_conn.autocommit is False
    assert tx_conn.commits == 1
    assert tx_conn.rollbacks == 0
    assert tx_conn.closed == 1
    assert shared_conn.commits == 0
