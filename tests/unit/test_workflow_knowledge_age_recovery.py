from __future__ import annotations

from unittest.mock import MagicMock

from core.infrastructure.db.workflow_knowledge_store import PostgresAgeWorkflowKnowledgeStore


def _store() -> PostgresAgeWorkflowKnowledgeStore:
    store = object.__new__(PostgresAgeWorkflowKnowledgeStore)
    store.graph_name = "platform_workflow_graph"
    store._schema_ensured = True
    return store


def test_reset_graph_drops_catalog_state_before_recreating_labels() -> None:
    store = _store()
    cur = MagicMock()
    age_schema_cursor = MagicMock()
    age_schema_cursor.return_value.__enter__.return_value = cur
    store._age_schema_cursor = age_schema_cursor
    store._graph_exists = MagicMock(return_value=True)
    store.create_graph_indexes = MagicMock()

    store.reset_graph()

    statement = str(cur.execute.call_args.args[0])
    assert "drop_graph" in statement
    store.create_graph_indexes.assert_called_once_with()


def test_semantic_probe_writes_reads_and_deletes_one_probe_vertex() -> None:
    store = _store()
    calls: list[str] = []

    def _run(query, *, columns, params=None):
        del columns
        calls.append(query)
        return [{"probe_id": params["probe_id"]}]

    store._run_cypher = _run

    result = store.semantic_read_write_probe()

    assert result["ok"] is True
    assert any("CREATE" in query for query in calls)
    assert any("MATCH" in query and "DELETE" in query for query in calls)
