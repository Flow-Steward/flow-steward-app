from __future__ import annotations

from .audit import MemoryAuditService
from .context_assembly import ContextAssemblyService
from .curator import MemoryCurator
from .entities import EntityResolutionService
from .exceptions import (
    MemoryConflictError,
    MemoryError,
    MemoryNotFoundError,
    MemoryPolicyError,
    MemoryUnsafeContentError,
    MemoryValidationError,
)
from .graphs import (
    ContextGraphBackend,
    PostgresAgeContextGraphBackend,
)
from .models import (
    CONFIRMATION_REQUIRED_TYPES,
    DATASET_SOURCE_TYPES,
    DEFAULT_CONTEXT_CATEGORIES,
    LIFECYCLE_STATUSES,
    MEMORY_TYPES,
    SOURCE_TRUST_LEVELS,
    VISIBILITIES,
    Citation,
    ContextAssemblyRequest,
    ContextAssemblyResult,
    CuratedBatch,
    CuratedRecordCandidate,
    EntityLinkInput,
    EvidenceRef,
    HybridRetrievalBackend,
    KnowledgeBackend,
    MemoryEdge,
    MemoryEdgeInput,
    MemoryEntity,
    MemoryEntityAlias,
    MemoryRecord,
    MemoryRecordInput,
    MemorySearchFilters,
    MemorySearchResult,
    TextSearchBackend,
    UnresolvedMention,
    VectorBackend,
)
from .policy import MemoryPolicyResolver, normalize_alias, trust_rank
from .retrieval import (
    MemoryRetrievalService,
    PostgresPgVectorBackend,
    PostgresUnifiedRetrievalBackend,
    record_to_citation,
)
from .writer import MemoryWriter

_RUNTIME_EXPORTS = {
    "MemoryRuntimeConfig",
    "MemoryServiceBundle",
    "build_memory_services",
    "curate_completed_job_to_memory",
}


def __getattr__(name: str):
    if name in _RUNTIME_EXPORTS:
        from . import runtime

        return getattr(runtime, name)
    raise AttributeError(name)


__all__ = [
    "CONFIRMATION_REQUIRED_TYPES",
    "DATASET_SOURCE_TYPES",
    "DEFAULT_CONTEXT_CATEGORIES",
    "LIFECYCLE_STATUSES",
    "MEMORY_TYPES",
    "SOURCE_TRUST_LEVELS",
    "VISIBILITIES",
    "Citation",
    "ContextAssemblyRequest",
    "ContextAssemblyResult",
    "ContextAssemblyService",
    "ContextGraphBackend",
    "CuratedBatch",
    "CuratedRecordCandidate",
    "EntityLinkInput",
    "EntityResolutionService",
    "EvidenceRef",
    "HybridRetrievalBackend",
    "KnowledgeBackend",
    "MemoryAuditService",
    "MemoryConflictError",
    "MemoryCurator",
    "MemoryEdge",
    "MemoryEdgeInput",
    "MemoryEntity",
    "MemoryEntityAlias",
    "MemoryError",
    "MemoryNotFoundError",
    "MemoryPolicyError",
    "MemoryPolicyResolver",
    "MemoryRecord",
    "MemoryRecordInput",
    "MemoryRetrievalService",
    "MemoryRuntimeConfig",
    "MemorySearchFilters",
    "MemorySearchResult",
    "MemoryServiceBundle",
    "MemoryUnsafeContentError",
    "MemoryValidationError",
    "MemoryWriter",
    "PostgresAgeContextGraphBackend",
    "PostgresPgVectorBackend",
    "PostgresUnifiedRetrievalBackend",
    "TextSearchBackend",
    "UnresolvedMention",
    "VectorBackend",
    "build_memory_services",
    "curate_completed_job_to_memory",
    "normalize_alias",
    "record_to_citation",
    "trust_rank",
]
