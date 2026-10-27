"""An agent read from a database created before agents lost ``phases``.

Schema changes carry no migration before 1.0, so such a database still has the
column. The row must come back without it, or every later save of the agent
(PATCH with only a description, clone, restore) is refused for a field the
author never sent.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from core.application.registry.db_store_agents_mixin import RegistryDBStoreAgentsMixin


class _Cursor:
    def __init__(self, rows: list[list[dict[str, Any]]]) -> None:
        self._rows = rows

    def execute(self, *_args: Any, **_kwargs: Any) -> None:
        self._current = self._rows.pop(0) if self._rows else []

    def fetchone(self) -> dict[str, Any] | None:
        return self._current[0] if self._current else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._current)


def _store(rows: list[list[dict[str, Any]]]) -> RegistryDBStoreAgentsMixin:
    store = RegistryDBStoreAgentsMixin.__new__(RegistryDBStoreAgentsMixin)
    cursor = _Cursor(rows)

    @contextmanager
    def _cursor():
        yield cursor

    store._cursor = _cursor  # type: ignore[method-assign]
    store._column_exists = lambda *_args: False  # type: ignore[method-assign]
    store._canonicalize_agent_prompts = lambda _agent: None  # type: ignore[method-assign]
    store._apply_agent_composition_fields = lambda _agent: None  # type: ignore[method-assign]
    return store


_LEGACY_ROW = {"agent_id": "writer", "display_name": "Writer", "phases": ["draft", "review"]}


def test_the_row_helper_drops_the_removed_phases_column() -> None:
    agent = RegistryDBStoreAgentsMixin._agent_row(dict(_LEGACY_ROW))

    assert "phases" not in agent
    assert agent["display_name"] == "Writer"


def test_listed_agents_come_back_without_phases() -> None:
    store = _store([[dict(_LEGACY_ROW)], [], []])

    agents = store.list_agents()

    assert [agent["agent_id"] for agent in agents] == ["writer"]
    assert "phases" not in agents[0]
