from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.application.memory.models import (
    KnowledgeBackend,
    MemoryRecord,
    MemoryRepository,
    MemorySearchFilters,
    MemorySearchResult,
    ScopeId,
    TextSearchBackend,
    VectorBackend,
)
from core.infrastructure.config.memory_config import (
    BackendCapabilities,
    MemoryRuntimeConfig,
    backend_capabilities,
    load_memory_runtime_config,
    validate_memory_runtime_config,
)
from core.infrastructure.db.memory_store import PostgresMemoryRepository

from .audit import MemoryAuditService
from .context_assembly import ContextAssemblyService
from .curator import MemoryCurator
from .entities import EntityResolutionService
from .exceptions import MemoryError
from .graphs import (
    ContextGraphBackend,
    NullContextGraphBackend,
    PostgresAgeContextGraphBackend,
)
from .policy import MemoryPolicyResolver
from .retrieval import (
    MemoryRetrievalService,
    PostgresPgVectorBackend,
    PostgresUnifiedRetrievalBackend,
)
from .writer import MemoryWriter


@dataclass(slots=True)
class MemoryServiceBundle:
    repository: MemoryRepository
    knowledge_backend: KnowledgeBackend
    text_search_backend: TextSearchBackend
    vector_backend: VectorBackend
    audit: MemoryAuditService
    policy: MemoryPolicyResolver
    entity_resolution: EntityResolutionService
    context_graph: ContextGraphBackend
    writer: MemoryWriter
    curator: MemoryCurator
    retrieval: MemoryRetrievalService
    context_assembly: ContextAssemblyService


class NullVectorBackend:
    def search_records(
        self,
        *,
        query: str,
        filters: MemorySearchFilters,
    ) -> list[MemorySearchResult]:
        del query, filters
        return []

    def index_record(
        self,
        record: MemoryRecord,
        *,
        repository: MemoryRepository | None = None,
    ) -> None:
        del record, repository

    def delete_project_scope(
        self,
        *,
        account_id: str,
        project_id: ScopeId,
        repository: MemoryRepository | None = None,
    ) -> None:
        del account_id, project_id, repository


def _default_logger():
    from core.infrastructure.db.platform_logger_common import resolve_observability_dsn
    from core.infrastructure.db.platform_stores import PlatformConnectionStore

    dsn = resolve_observability_dsn()
    if not dsn:
        raise RuntimeError("Memory services require FS_OBSERVABILITY_DSN, FS_DB_DSN, or equivalent")
    # Reuse the app-scoped logger instead of creating a new live connection per call.
    return PlatformConnectionStore.get_shared_instance(dsn)


def _load_runtime_config() -> MemoryRuntimeConfig:
    return load_memory_runtime_config()


def _validate_or_raise(config: MemoryRuntimeConfig) -> None:
    errors = validate_memory_runtime_config(config)
    if errors:
        raise MemoryError("Invalid memory backend configuration: " + " ".join(errors))


def _build_repository(*, logger_obj: Any, config: MemoryRuntimeConfig) -> MemoryRepository:
    if config.repository_backend == "postgres":
        return PostgresMemoryRepository(logger_instance=logger_obj)
    raise MemoryError(
        f"Unsupported memory repository backend '{config.repository_backend}' for this release"
    )


def _build_knowledge_backend(
    *,
    repository: MemoryRepository,
    config: MemoryRuntimeConfig,
) -> KnowledgeBackend:
    if config.knowledge_backend == "postgres":
        return repository
    raise MemoryError(
        f"Unsupported knowledge backend '{config.knowledge_backend}' for this release"
    )


def _build_text_search_backend(
    *,
    repository: MemoryRepository,
    config: MemoryRuntimeConfig,
) -> TextSearchBackend:
    if config.text_search_backend in {"postgres_fts", "postgres_trgm"}:
        return repository
    raise MemoryError(
        f"Unsupported text search backend '{config.text_search_backend}' for this release"
    )


def _build_vector_backend(*, config: MemoryRuntimeConfig) -> VectorBackend:
    if config.vector_backend in {"", "null", "none"}:
        return NullVectorBackend()
    if config.vector_backend == "postgres_pgvector":
        return PostgresPgVectorBackend()
    raise MemoryError(f"Unsupported vector backend '{config.vector_backend}' for this release")


def _build_context_graph(
    *, repository: MemoryRepository, config: MemoryRuntimeConfig
) -> ContextGraphBackend:
    if config.context_graph_backend == "postgres_age":
        return PostgresAgeContextGraphBackend(repository=repository)
    if config.context_graph_backend in {"", "null", "none"}:
        return NullContextGraphBackend()
    raise MemoryError(
        f"Unsupported context graph backend '{config.context_graph_backend}' for this release"
    )


def build_memory_services(
    *,
    logger_instance: Any | None = None,
    runtime_config: MemoryRuntimeConfig | None = None,
) -> MemoryServiceBundle:
    resolved_runtime_config = runtime_config or _load_runtime_config()
    _validate_or_raise(resolved_runtime_config)
    logger_obj = logger_instance or _default_logger()
    repository = _build_repository(logger_obj=logger_obj, config=resolved_runtime_config)
    knowledge_backend = _build_knowledge_backend(
        repository=repository,
        config=resolved_runtime_config,
    )
    text_search_backend = _build_text_search_backend(
        repository=repository,
        config=resolved_runtime_config,
    )
    vector_backend = _build_vector_backend(config=resolved_runtime_config)
    vector_capabilities = backend_capabilities("vector", resolved_runtime_config.vector_backend)
    text_capabilities = backend_capabilities(
        "text_search", resolved_runtime_config.text_search_backend
    )
    context_capabilities = backend_capabilities(
        "context_graph", resolved_runtime_config.context_graph_backend
    )
    unified_capabilities = BackendCapabilities(
        supports_vector_search=vector_capabilities.supports_vector_search,
        supports_text_search=text_capabilities.supports_text_search,
        supports_graph_expansion=context_capabilities.supports_graph_expansion,
        supports_acl_pushdown=True,
        supports_hybrid_single_query=True,
        co_location_group="postgres",
    )
    audit = MemoryAuditService(repository=repository)
    policy = MemoryPolicyResolver()
    entity_resolution = EntityResolutionService(repository=repository)
    context_graph = _build_context_graph(
        repository=repository,
        config=resolved_runtime_config,
    )
    writer = MemoryWriter(
        repository=repository,
        audit_service=audit,
        entity_resolution=entity_resolution,
        context_graph_backend=context_graph,
        vector_backend=vector_backend,
    )
    retrieval_backend = PostgresUnifiedRetrievalBackend(
        repository=repository,
        capabilities=unified_capabilities,
        vector_backend=vector_backend
        if isinstance(vector_backend, PostgresPgVectorBackend)
        else None,
    )
    retrieval = MemoryRetrievalService(
        retrieval_backend=retrieval_backend,
        knowledge_backend=knowledge_backend,
        vector_backend=vector_backend,
        policy_resolver=policy,
    )
    context_assembly = ContextAssemblyService(
        retrieval_service=retrieval,
        policy_resolver=policy,
        audit_service=audit,
        context_graph_backend=context_graph,
    )
    return MemoryServiceBundle(
        repository=repository,
        knowledge_backend=knowledge_backend,
        text_search_backend=text_search_backend,
        vector_backend=vector_backend,
        audit=audit,
        policy=policy,
        entity_resolution=entity_resolution,
        context_graph=context_graph,
        writer=writer,
        curator=MemoryCurator(),
        retrieval=retrieval,
        context_assembly=context_assembly,
    )


def curate_completed_job_to_memory(
    *,
    job_id: ScopeId,
    logger_instance: Any | None = None,
) -> list[str]:
    services = build_memory_services(logger_instance=logger_instance)
    source_row = services.knowledge_backend.load_job_source(job_id=job_id)
    if not source_row:
        return []
    record_ids: list[str] = []
    with services.repository.transaction():
        for candidate in services.curator.curate_completed_job(source_row).candidates:
            record = services.writer.create_candidate(candidate.record)
            record_ids.append(record.id)
        message_rows = services.knowledge_backend.load_job_agent_messages(job_id=job_id, limit=200)
        for candidate in services.curator.curate_agent_messages(message_rows).candidates:
            record = services.writer.create_candidate(candidate.record)
            record_ids.append(record.id)
    return record_ids
