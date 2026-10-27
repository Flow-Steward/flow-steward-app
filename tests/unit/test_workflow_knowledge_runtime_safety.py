from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from core.application.workflows.knowledge_projection import WorkflowKnowledgeProjection
from core.infrastructure.db.workflow_knowledge_store import PostgresAgeWorkflowKnowledgeStore
from core.infrastructure.runtime.memory_preflight import _workflow_knowledge_runtime_check


def _store() -> PostgresAgeWorkflowKnowledgeStore:
    store = object.__new__(PostgresAgeWorkflowKnowledgeStore)
    store.graph_name = "platform_workflow_graph"
    store._document_vector_column_available_cache = None
    return store


def test_runtime_status_is_read_only_and_does_not_repair_schema() -> None:
    store = _store()
    relational_cursor = MagicMock()
    age_cursor = MagicMock()
    executed: list[str] = []

    relational_cursor.execute.side_effect = lambda statement, *_args: executed.append(
        str(statement)
    )
    age_cursor.execute.side_effect = lambda statement, *_args: executed.append(str(statement))

    @contextmanager
    def connect():
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = relational_cursor
        yield connection

    @contextmanager
    def age_connection():
        yield age_cursor

    store._connect = connect
    store._age_cursor = age_connection
    store._canonical_schema_status = MagicMock(
        return_value={
            "available": True,
            "missing_relations": [],
            "missing_indexes": [],
            "error": "",
        }
    )
    store._pg_extension_available = MagicMock(return_value=True)
    store._installed_extensions = MagicMock(return_value={"age", "vector"})
    store._column_exists = MagicMock(return_value=True)
    store._graph_exists = MagicMock(return_value=True)
    store._label_exists = MagicMock(return_value=True)
    store._ensure_schema = MagicMock(side_effect=AssertionError("runtime status attempted DDL"))
    store.create_vector_index = MagicMock(
        side_effect=AssertionError("runtime status attempted DDL")
    )
    store.create_graph_indexes = MagicMock(
        side_effect=AssertionError("runtime status attempted DDL")
    )
    store._ensure_graph = MagicMock(side_effect=AssertionError("runtime status attempted DDL"))
    store._ensure_labels = MagicMock(side_effect=AssertionError("runtime status attempted DDL"))

    status = store.runtime_status(ensure_indexes=True)

    assert status["ok"] is True
    assert status["schema"]["available"] is True
    assert not any(
        statement.lstrip().upper().startswith(("CREATE ", "ALTER ", "DROP "))
        for statement in executed
    )


def test_runtime_age_cursor_loads_installed_extension_without_creating_it() -> None:
    store = _store()
    cursor = MagicMock()
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor

    @contextmanager
    def connect():
        yield connection

    store._connect = connect

    with store._age_cursor():
        pass

    statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
    assert statements == ["LOAD 'age'", 'SET search_path = ag_catalog, "$user", public']


def test_runtime_status_reports_missing_canonical_schema_without_graph_access() -> None:
    store = _store()
    relational_cursor = MagicMock()

    @contextmanager
    def connect():
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = relational_cursor
        yield connection

    store._connect = connect
    store._canonical_schema_status = MagicMock(
        return_value={
            "available": False,
            "missing_relations": ["public.platform_workflow_knowledge_edges"],
            "missing_indexes": ["public.idx_platform_workflow_knowledge_documents_search"],
            "error": "",
        }
    )
    store._pg_extension_available = MagicMock(return_value=True)
    store._installed_extensions = MagicMock(return_value={"age", "vector"})
    store._age_cursor = MagicMock(side_effect=AssertionError("missing schema must fail closed"))
    store._ensure_schema = MagicMock(
        side_effect=AssertionError("missing schema must not be repaired")
    )

    status = store.runtime_status()

    assert status["ok"] is False
    assert status["schema"] == {
        "available": False,
        "missing_relations": ["public.platform_workflow_knowledge_edges"],
        "missing_indexes": ["public.idx_platform_workflow_knowledge_documents_search"],
        "error": "",
    }
    assert "canonical workflow knowledge schema is incomplete" in status["graph"]["error"]
    assert "canonical workflow knowledge schema is incomplete" in status["vector"]["error"]


@pytest.mark.parametrize(
    ("method_name", "kwargs"),
    [
        (
            "search",
            {"query": "approval", "account_id": "acct_test"},
        ),
        (
            "upsert_projection",
            {"projection": WorkflowKnowledgeProjection(), "account_id": "acct_test"},
        ),
        ("delete_scope", {"account_id": "acct_test"}),
        ("get_scope_sync_digest", {"account_id": "acct_test"}),
        ("scope_counts", {"account_id": "acct_test"}),
        (
            "set_scope_sync_digest",
            {"account_id": "acct_test", "digest": "digest_test"},
        ),
    ],
)
def test_runtime_operations_fail_closed_before_attempting_schema_repair(
    method_name: str,
    kwargs: dict[str, object],
) -> None:
    store = _store()
    store.enforce_runtime = MagicMock(
        side_effect=RuntimeError("canonical workflow knowledge schema is incomplete")
    )
    store._ensure_schema = MagicMock(side_effect=AssertionError("runtime operation attempted DDL"))
    store.create_vector_index = MagicMock(
        side_effect=AssertionError("runtime operation attempted DDL")
    )
    store.create_graph_indexes = MagicMock(
        side_effect=AssertionError("runtime operation attempted DDL")
    )

    _raises_callable_170_1 = getattr(store, method_name)
    with pytest.raises(RuntimeError, match="canonical workflow knowledge schema is incomplete"):
        _raises_callable_170_1(**kwargs)

    store.enforce_runtime.assert_called_once_with(ensure_indexes=False)
    store._ensure_schema.assert_not_called()
    store.create_vector_index.assert_not_called()
    store.create_graph_indexes.assert_not_called()


def test_scope_status_does_not_scan_content_when_runtime_schema_is_missing() -> None:
    store = _store()
    store.runtime_status = MagicMock(
        return_value={
            "ok": False,
            "schema": {
                "available": False,
                "missing_relations": ["public.platform_workflow_knowledge_documents"],
                "missing_indexes": [],
                "error": "Canonical workflow knowledge schema is incomplete.",
            },
            "graph": {"available": False, "error": "schema incomplete"},
            "vector": {"available": False, "error": "schema incomplete"},
        }
    )
    store.scope_counts = MagicMock(side_effect=AssertionError("content scan attempted"))
    store.get_scope_sync_digest = MagicMock(side_effect=AssertionError("content scan attempted"))

    status = store.scope_status(account_id="acct_test", expected_digest="digest_test")

    assert status["scope"]["freshness"] == "unavailable"
    assert status["scope"]["ready"] is False
    store.scope_counts.assert_not_called()
    store.get_scope_sync_digest.assert_not_called()


def test_preflight_diagnostic_names_missing_canonical_relation_and_index() -> None:
    runtime = MagicMock()
    runtime.runtime_status.return_value = {
        "ok": False,
        "schema": {
            "available": False,
            "missing_relations": ["public.platform_workflow_knowledge_edges"],
            "missing_indexes": ["public.idx_platform_workflow_knowledge_documents_search"],
            "error": "Canonical workflow knowledge schema is incomplete.",
        },
        "graph": {"available": False, "error": "schema incomplete"},
        "vector": {"available": False, "error": "schema incomplete"},
    }

    with patch(
        "core.infrastructure.db.workflow_knowledge_store.PostgresAgeWorkflowKnowledgeStore",
        return_value=runtime,
    ):
        errors, status = _workflow_knowledge_runtime_check(ensure_indexes=False)

    assert status["schema"]["available"] is False
    assert any("public.platform_workflow_knowledge_edges" in error for error in errors)
    assert any(
        "public.idx_platform_workflow_knowledge_documents_search" in error for error in errors
    )
