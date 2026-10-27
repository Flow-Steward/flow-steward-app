from __future__ import annotations

from dataclasses import dataclass

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

POSTGRES_COLOCATION_GROUP = "postgres"

SUPPORTED_MEMORY_PROFILES = frozenset({"postgres_unified"})
SUPPORTED_REPOSITORY_BACKENDS = frozenset({"postgres"})
SUPPORTED_KNOWLEDGE_BACKENDS = frozenset({"postgres"})
SUPPORTED_TEXT_SEARCH_BACKENDS = frozenset({"postgres_fts", "postgres_trgm"})
SUPPORTED_VECTOR_BACKENDS = frozenset({"null", "postgres_pgvector"})
SUPPORTED_CONTEXT_GRAPH_BACKENDS = frozenset({"null", "postgres_age"})


@dataclass(frozen=True, slots=True)
class MemoryRuntimeConfig:
    profile: str = "postgres_unified"
    repository_backend: str = "postgres"
    knowledge_backend: str = "postgres"
    text_search_backend: str = "postgres_fts"
    vector_backend: str = "null"
    context_graph_backend: str = "postgres_age"
    pgvector_dsn: str = ""


@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    supports_vector_search: bool
    supports_text_search: bool
    supports_graph_expansion: bool
    supports_acl_pushdown: bool
    supports_hybrid_single_query: bool
    co_location_group: str


def _env(name: str, default: str = "") -> str:
    return str(_runtime_env.get(name, default) or default).strip()


def _normalized(name: str, default: str = "") -> str:
    return _env(name, default).lower()


def load_memory_runtime_config() -> MemoryRuntimeConfig:
    return MemoryRuntimeConfig(
        profile=_normalized("FS_MEMORY_PROFILE", "postgres_unified"),
        repository_backend=_normalized("FS_MEMORY_REPOSITORY_BACKEND", "postgres"),
        knowledge_backend=_normalized("FS_MEMORY_KNOWLEDGE_BACKEND", "postgres"),
        text_search_backend=_normalized("FS_MEMORY_TEXT_SEARCH_BACKEND", "postgres_fts"),
        # AC17-R131: pgvector, like every Compose file. Falling back to "null"
        # turned vector search off without an error anywhere -- /api/search still
        # answers 200 with partial=false and simply searches lexically.
        vector_backend=_normalized("FS_MEMORY_VECTOR_BACKEND", "postgres_pgvector"),
        context_graph_backend=_normalized("FS_CONTEXT_GRAPH_BACKEND", "postgres_age"),
        pgvector_dsn=_env("FS_PGVECTOR_DSN"),
    )


def _validate_choice(name: str, value: str, supported: frozenset[str]) -> list[str]:
    if value in supported:
        return []
    allowed = ", ".join(sorted(supported))
    return [f"{name}={value!r} is unsupported. Supported values: {allowed}."]


def backend_capabilities(kind: str, value: str) -> BackendCapabilities:
    normalized = str(value or "null").strip().lower()
    if kind in {"repository", "knowledge"} and normalized == "postgres":
        return BackendCapabilities(False, False, False, True, False, POSTGRES_COLOCATION_GROUP)
    if kind == "text_search" and normalized in {"postgres_fts", "postgres_trgm"}:
        return BackendCapabilities(False, True, False, True, True, POSTGRES_COLOCATION_GROUP)
    if kind == "vector" and normalized == "postgres_pgvector":
        return BackendCapabilities(True, False, False, True, True, POSTGRES_COLOCATION_GROUP)
    if kind == "vector" and normalized == "null":
        return BackendCapabilities(False, False, False, True, True, POSTGRES_COLOCATION_GROUP)
    if kind == "context_graph" and normalized == "postgres_age":
        return BackendCapabilities(False, False, True, True, True, POSTGRES_COLOCATION_GROUP)
    if kind == "context_graph" and normalized == "null":
        return BackendCapabilities(False, False, False, True, True, POSTGRES_COLOCATION_GROUP)
    return BackendCapabilities(False, False, False, False, False, "unsupported")


def memory_backend_matrix(config: MemoryRuntimeConfig) -> dict[str, str]:
    return {
        "profile": config.profile,
        "repository": config.repository_backend,
        "knowledge": config.knowledge_backend,
        "text_search": config.text_search_backend,
        "vector": config.vector_backend,
        "context_graph": config.context_graph_backend,
    }


def retrieval_colocated_in_postgres(config: MemoryRuntimeConfig) -> bool:
    groups = {
        backend_capabilities("repository", config.repository_backend).co_location_group,
        backend_capabilities("knowledge", config.knowledge_backend).co_location_group,
        backend_capabilities("text_search", config.text_search_backend).co_location_group,
        backend_capabilities("vector", config.vector_backend).co_location_group,
        backend_capabilities("context_graph", config.context_graph_backend).co_location_group,
    }
    return groups == {POSTGRES_COLOCATION_GROUP}


def validate_memory_runtime_config(config: MemoryRuntimeConfig) -> list[str]:
    errors: list[str] = []
    errors += _validate_choice("FS_MEMORY_PROFILE", config.profile, SUPPORTED_MEMORY_PROFILES)
    errors += _validate_choice(
        "FS_MEMORY_REPOSITORY_BACKEND",
        config.repository_backend,
        SUPPORTED_REPOSITORY_BACKENDS,
    )
    errors += _validate_choice(
        "FS_MEMORY_KNOWLEDGE_BACKEND",
        config.knowledge_backend,
        SUPPORTED_KNOWLEDGE_BACKENDS,
    )
    errors += _validate_choice(
        "FS_MEMORY_TEXT_SEARCH_BACKEND",
        config.text_search_backend,
        SUPPORTED_TEXT_SEARCH_BACKENDS,
    )
    errors += _validate_choice(
        "FS_MEMORY_VECTOR_BACKEND",
        config.vector_backend,
        SUPPORTED_VECTOR_BACKENDS,
    )
    errors += _validate_choice(
        "FS_CONTEXT_GRAPH_BACKEND",
        config.context_graph_backend,
        SUPPORTED_CONTEXT_GRAPH_BACKENDS,
    )
    if config.profile == "postgres_unified" and not retrieval_colocated_in_postgres(config):
        errors.append(
            "FS_MEMORY_PROFILE=postgres_unified requires repository, knowledge, text, "
            "vector, and context graph backends to share the Postgres co-location group."
        )
    return errors


def required_postgres_extensions(config: MemoryRuntimeConfig) -> tuple[str, ...]:
    required: list[str] = []
    if config.vector_backend == "postgres_pgvector":
        required.append("vector")
    if config.text_search_backend == "postgres_trgm":
        required.extend(["pg_trgm", "unaccent"])
    if config.context_graph_backend == "postgres_age":
        required.append("age")
    return tuple(dict.fromkeys(required))


def redacted_memory_backend_matrix(config: MemoryRuntimeConfig) -> dict[str, str]:
    matrix = memory_backend_matrix(config)
    if config.pgvector_dsn:
        matrix["pgvector_dsn"] = "<redacted>"
    return matrix
