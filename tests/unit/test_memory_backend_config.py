from __future__ import annotations

from core.infrastructure.config.memory_config import (
    MemoryRuntimeConfig,
    load_memory_runtime_config,
    required_postgres_extensions,
    retrieval_colocated_in_postgres,
    validate_memory_runtime_config,
)
from core.infrastructure.runtime.memory_preflight import validate_memory_preflight


def test_load_memory_runtime_config_reads_fs_namespace(monkeypatch) -> None:
    monkeypatch.setenv("FS_MEMORY_PROFILE", "postgres_unified")
    monkeypatch.setenv("FS_MEMORY_REPOSITORY_BACKEND", "postgres")
    monkeypatch.setenv("FS_MEMORY_KNOWLEDGE_BACKEND", "postgres")
    monkeypatch.setenv("FS_MEMORY_TEXT_SEARCH_BACKEND", "postgres_trgm")
    monkeypatch.setenv("FS_MEMORY_VECTOR_BACKEND", "postgres_pgvector")
    monkeypatch.setenv("FS_CONTEXT_GRAPH_BACKEND", "postgres_age")
    monkeypatch.setenv("FS_PGVECTOR_DSN", "postgresql://pgvector")

    config = load_memory_runtime_config()

    assert config == MemoryRuntimeConfig(
        profile="postgres_unified",
        repository_backend="postgres",
        knowledge_backend="postgres",
        text_search_backend="postgres_trgm",
        vector_backend="postgres_pgvector",
        context_graph_backend="postgres_age",
        pgvector_dsn="postgresql://pgvector",
    )


def test_postgres_unified_requires_postgres_colocation() -> None:
    config = MemoryRuntimeConfig(
        profile="postgres_unified",
        vector_backend="chroma",
    )

    errors = validate_memory_runtime_config(config)

    assert retrieval_colocated_in_postgres(config) is False
    assert any("FS_MEMORY_PROFILE=postgres_unified requires" in error for error in errors)


def test_unsupported_release_backend_values_fail_validation() -> None:
    config = MemoryRuntimeConfig(
        profile="federated",
        vector_backend="qdrant",
        context_graph_backend="apache_age",
    )

    errors = validate_memory_runtime_config(config)

    assert any("FS_MEMORY_PROFILE='federated' is unsupported" in error for error in errors)
    assert any("FS_MEMORY_VECTOR_BACKEND='qdrant' is unsupported" in error for error in errors)
    assert any("FS_CONTEXT_GRAPH_BACKEND='apache_age' is unsupported" in error for error in errors)


def test_required_postgres_extensions_are_backend_specific() -> None:
    assert required_postgres_extensions(MemoryRuntimeConfig(context_graph_backend="null")) == ()
    assert required_postgres_extensions(
        MemoryRuntimeConfig(
            text_search_backend="postgres_trgm",
            vector_backend="postgres_pgvector",
            context_graph_backend="postgres_age",
        )
    ) == ("vector", "pg_trgm", "unaccent", "age")


def test_pgvector_does_not_require_an_environment_provider_key() -> None:
    errors = validate_memory_runtime_config(MemoryRuntimeConfig(vector_backend="postgres_pgvector"))

    assert errors == []


def test_preflight_rejects_old_unsupported_release_combination() -> None:
    result = validate_memory_preflight(
        config=MemoryRuntimeConfig(
            profile="postgres_unified",
            vector_backend="chroma",
        ),
        check_connectivity=False,
    )

    assert result["ok"] is False
    assert any(
        "FS_MEMORY_VECTOR_BACKEND='chroma' is unsupported" in error for error in result["errors"]
    )


def test_preflight_reports_workflow_knowledge_runtime_errors(monkeypatch) -> None:
    class _BrokenWorkflowKnowledgeStore:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def runtime_status(self, *, ensure_indexes: bool = False):
            del ensure_indexes
            return {
                "ok": False,
                "graph": {"available": False, "error": "Apache AGE extension is not installed."},
                "vector": {"available": False, "error": "pgvector extension is not installed."},
            }

    # AC17-R129: a broken knowledge backend is fatal only when there is no lexical
    # fallback to degrade to, so the test says so instead of relying on a default.
    # It used to unset the variable and lean on the compiled-in "off" -- a value no
    # deployment ran, since every Compose file sets it on.
    monkeypatch.setenv("FS_WORKFLOW_DISCOVERY_LEXICAL_FALLBACK_ENABLED", "0")
    monkeypatch.setattr(
        "core.infrastructure.db.workflow_knowledge_store.PostgresAgeWorkflowKnowledgeStore",
        _BrokenWorkflowKnowledgeStore,
    )

    result = validate_memory_preflight(
        config=MemoryRuntimeConfig(),
        check_connectivity=False,
        check_workflow_knowledge=True,
    )

    assert result["ok"] is False
    assert result["workflow_knowledge"]["checked"] is True
    assert result["workflow_knowledge"]["ok"] is False
    assert any(
        "Workflow knowledge graph backend unavailable" in error for error in result["errors"]
    )
    assert any(
        "Workflow knowledge vector backend unavailable" in error for error in result["errors"]
    )


def test_preflight_reports_empty_scoped_workflow_knowledge_index(monkeypatch) -> None:
    class _EmptyScopedWorkflowKnowledgeStore:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def scope_status(
            self,
            *,
            account_id: str,
            tenant_id: str | None = None,
            project_id: str | None = None,
            expected_digest: str = "",
            ensure_indexes: bool = False,
        ):
            del account_id, tenant_id, project_id, expected_digest, ensure_indexes
            return {
                "ok": True,
                "graph": {"available": True},
                "vector": {"available": True},
                "scope": {
                    "empty": True,
                    "freshness": "empty",
                    "counts": {"documents": 0, "nodes": 0, "edges": 0},
                },
            }

    # AC17-R129: fatal only without a fallback; see the first of these tests.
    monkeypatch.setenv("FS_WORKFLOW_DISCOVERY_LEXICAL_FALLBACK_ENABLED", "0")
    monkeypatch.setattr(
        "core.infrastructure.db.workflow_knowledge_store.PostgresAgeWorkflowKnowledgeStore",
        _EmptyScopedWorkflowKnowledgeStore,
    )

    result = validate_memory_preflight(
        config=MemoryRuntimeConfig(),
        check_connectivity=False,
        check_workflow_knowledge=True,
        workflow_knowledge_account_id="acct_test",
        workflow_knowledge_project_id="proj_test",
        workflow_knowledge_catalog_digest="digest-now",
    )

    assert result["ok"] is False
    assert result["workflow_knowledge"]["scope"]["freshness"] == "empty"
    assert any("Workflow knowledge index is empty" in error for error in result["errors"])


def test_preflight_reports_stale_scoped_workflow_knowledge_index(monkeypatch) -> None:
    class _StaleScopedWorkflowKnowledgeStore:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def scope_status(
            self,
            *,
            account_id: str,
            tenant_id: str | None = None,
            project_id: str | None = None,
            expected_digest: str = "",
            ensure_indexes: bool = False,
        ):
            del account_id, tenant_id, project_id, expected_digest, ensure_indexes
            return {
                "ok": True,
                "graph": {"available": True},
                "vector": {"available": True},
                "scope": {
                    "empty": False,
                    "freshness": "stale",
                    "counts": {"documents": 3, "nodes": 2, "edges": 1},
                },
            }

    # AC17-R129: fatal only without a fallback; see the first of these tests.
    monkeypatch.setenv("FS_WORKFLOW_DISCOVERY_LEXICAL_FALLBACK_ENABLED", "0")
    monkeypatch.setattr(
        "core.infrastructure.db.workflow_knowledge_store.PostgresAgeWorkflowKnowledgeStore",
        _StaleScopedWorkflowKnowledgeStore,
    )

    result = validate_memory_preflight(
        config=MemoryRuntimeConfig(),
        check_connectivity=False,
        check_workflow_knowledge=True,
        workflow_knowledge_account_id="acct_test",
        workflow_knowledge_project_id="proj_test",
        workflow_knowledge_catalog_digest="digest-now",
    )

    assert result["ok"] is False
    assert result["workflow_knowledge"]["scope"]["freshness"] == "stale"
    assert any("Workflow knowledge index is stale" in error for error in result["errors"])
