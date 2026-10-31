"""Concurrent agent creates on the audit PostgreSQL: one object, one conflict, no overwrite.

The first create is held inside its transaction right after its insert, so the
second create provably runs while the first is uncommitted. Without the
transaction-scoped advisory lock the second create would not see the first row
and both would commit.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.command_services_agent import AgentCommandService
from core.application.management.errors import ServiceConflictError
from core.application.management.registry_name_conflicts import (
    ID_CONFLICT_CODE,
    NAME_CONFLICT_CODE,
)
from core.application.management.scope import normalize_scope
from core.application.registry.db_store import RegistryDBStore

_VALID = {"valid": True, "errors": [], "warnings": []}
_HOLD_SECONDS = 10


class _HeldFirstInsert:
    """Wrap ``save_agent``: the first caller pauses after its insert until released."""

    def __init__(self, store: RegistryDBStore) -> None:
        self.store = store
        self.save = store.save_agent
        self.inserted = threading.Event()
        self.release = threading.Event()
        self.first = threading.Lock()
        self.held = False

    def __call__(self, data, author="admin", **kwargs):
        self.save(data, author, **kwargs)
        with self.first:
            hold, self.held = not self.held, True
        if hold:
            self.inserted.set()
            assert self.release.wait(_HOLD_SECONDS), "the test never released the first create"


def _service(store: RegistryDBStore) -> AgentCommandService:
    return AgentCommandService(
        get_db=lambda: store,
        reload_registry=MagicMock(),
        get_agent_validation=MagicMock(return_value=_VALID),
    )


@pytest.fixture
def stores(audit_db):
    _store, conn, _workflow_id, _project_id = audit_db
    first, second = RegistryDBStore(_store.dsn), RegistryDBStore(_store.dsn)
    suffix = uuid.uuid4().hex[:10]
    created: list[str] = []
    try:
        yield first, second, conn, suffix, created
    finally:
        for agent_id in created:
            conn.execute("DELETE FROM registry_agents WHERE agent_id = %s", (agent_id,))


def _race(stores, first_payload, second_payload):
    first, second, conn, _suffix, created = stores
    created.extend({first_payload["agent_id"], second_payload["agent_id"]})
    held = _HeldFirstInsert(first)
    first.save_agent = held
    scope = normalize_scope(account_id=DEFAULT_ACCOUNT_ID, actor_id="race")
    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(_service(first).create_agent, first_payload, scope=scope)
        assert held.inserted.wait(_HOLD_SECONDS), winner.exception(timeout=1)
        loser = pool.submit(_service(second).create_agent, second_payload, scope=scope)
        # The second create must wait for the first transaction, not finish beside it.
        with pytest.raises(TimeoutError):
            loser.result(timeout=1)
        held.release.set()
        result = winner.result(timeout=_HOLD_SECONDS)
        with pytest.raises(ServiceConflictError) as refused:
            loser.result(timeout=_HOLD_SECONDS)
    rows = conn.execute(
        "SELECT agent_id, display_name FROM registry_agents WHERE agent_id = ANY(%s)",
        (sorted(set(created)),),
    ).fetchall()
    return result, refused.value.payload, rows


def test_concurrent_creates_with_one_name_create_one_agent(stores):
    suffix = stores[3]
    name = f"Race Writer {suffix}"
    result, payload, rows = _race(
        stores,
        {"agent_id": f"race_a_{suffix}", "display_name": name},
        {"agent_id": f"race_b_{suffix}", "display_name": f"  race   WRITER {suffix.upper()} "},
    )

    assert result.agent_id == f"race_a_{suffix}"
    assert payload["code"] == NAME_CONFLICT_CODE
    assert [match["id"] for match in payload["matches"]] == [f"race_a_{suffix}"]
    assert rows == [(f"race_a_{suffix}", name)]


def test_concurrent_creates_with_one_id_never_overwrite(stores):
    suffix = stores[3]
    agent_id = f"race_id_{suffix}"
    result, payload, rows = _race(
        stores,
        {"agent_id": agent_id, "display_name": f"First {suffix}"},
        {"agent_id": agent_id, "display_name": f"Second {suffix}"},
    )

    assert result.agent_id == agent_id
    assert payload["code"] == ID_CONFLICT_CODE
    assert [match["id"] for match in payload["matches"]] == [agent_id]
    assert payload["suggested_id"] == f"{agent_id}_1"
    assert rows == [(agent_id, f"First {suffix}")]
