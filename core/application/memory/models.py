from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

ScopeId = str | int

MEMORY_TYPES = frozenset(
    {
        "fact",
        "decision",
        "lesson",
        "project_rule",
        "preference",
        "known_issue",
        "workflow_pattern",
        "extension_note",
        "verification_result",
        "procedural_playbook",
        "docs_note",
        "dataset_note",
    }
)

LIFECYCLE_STATUSES = frozenset({"candidate", "active", "superseded", "rejected", "expired"})
VISIBILITIES = frozenset({"private_user", "project", "account", "tenant", "system"})
SOURCE_TRUST_LEVELS = frozenset(
    {
        "user_supplied",
        "agent_inferred",
        "tool_observed",
        "system_generated",
        "human_approved",
        "canonical_doc",
    }
)

CONFIRMATION_REQUIRED_TYPES = frozenset({"project_rule", "decision", "known_issue", "dataset_note"})
DATASET_SOURCE_TYPES = frozenset(
    {"dataset_schema", "dataset_query_template", "dataset_run_artifact"}
)

DEFAULT_CONTEXT_CATEGORIES = (
    "project_profile_context",
    "project_memory",
    "workflow_memory",
    "procedural_memory",
    "docs_memory",
    "graph_context",
    "dataset_context",
)


def approximate_token_count(*values: Any) -> int:
    text = " ".join(str(value or "") for value in values if value is not None).strip()
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


@dataclass(slots=True)
class EvidenceRef:
    ref_type: str
    ref_id: str
    label: str = ""
    uri: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Citation:
    record_id: str
    title: str
    source_type: str
    source_id: str
    source_trust: str
    snippet: str = ""
    confidence: float | None = None
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MemoryRecordInput:
    account_id: str
    created_by: str
    memory_type: str
    visibility: str
    title: str
    summary: str
    content: str
    source_type: str
    source_id: str = ""
    source_trust: str = "system_generated"
    lifecycle_status: str = "candidate"
    tenant_id: str | None = None
    project_id: ScopeId | None = None
    workflow_id: str | None = None
    job_id: ScopeId | None = None
    session_id: str | None = None
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    confidence: float | None = None
    importance: float = 0.5
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    supersedes_id: str | None = None
    retention_policy: str | None = None
    expires_at: datetime | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MemoryRecord:
    id: str
    account_id: str
    created_by: str
    memory_type: str
    visibility: str
    title: str
    summary: str
    content: str
    source_type: str
    source_id: str
    source_trust: str
    lifecycle_status: str
    tenant_id: str | None = None
    project_id: ScopeId | None = None
    workflow_id: str | None = None
    job_id: ScopeId | None = None
    session_id: str | None = None
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    confidence: float | None = None
    importance: float = 0.5
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    supersedes_id: str | None = None
    retention_policy: str | None = None
    expires_at: datetime | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def token_estimate(self) -> int:
        return approximate_token_count(self.title, self.summary, self.content)


@dataclass(slots=True)
class MemoryEntity:
    id: str
    account_id: str
    entity_type: str
    canonical_name: str
    normalized_name: str
    created_by: str
    tenant_id: str | None = None
    project_id: ScopeId | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class MemoryEntityAlias:
    id: str
    entity_id: str
    alias: str
    normalized_alias: str
    language_code: str = ""
    created_by: str = ""
    provenance: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None


@dataclass(slots=True)
class UnresolvedMention:
    mention_text: str
    normalized_mention: str
    language_code: str = ""
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class EntityLinkInput:
    record_id: str
    entity_id: str
    mention_text: str = ""
    mention_role: str = "mentioned"
    confidence: float | None = None
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MemoryEdgeInput:
    account_id: str
    edge_type: str
    created_by: str
    from_record_id: str | None = None
    to_record_id: str | None = None
    from_entity_id: str | None = None
    to_entity_id: str | None = None
    tenant_id: str | None = None
    project_id: ScopeId | None = None
    workflow_id: str | None = None
    job_id: ScopeId | None = None
    session_id: str | None = None
    confidence: float | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    valid_from: datetime | None = None
    valid_to: datetime | None = None


@dataclass(slots=True)
class MemoryEdge:
    id: str
    account_id: str
    edge_type: str
    created_by: str
    from_record_id: str | None = None
    to_record_id: str | None = None
    from_entity_id: str | None = None
    to_entity_id: str | None = None
    tenant_id: str | None = None
    project_id: ScopeId | None = None
    workflow_id: str | None = None
    job_id: ScopeId | None = None
    session_id: str | None = None
    confidence: float | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    created_at: datetime | None = None


@dataclass(slots=True)
class MemorySearchFilters:
    account_id: str
    actor_id: str = ""
    tenant_id: str | None = None
    project_id: ScopeId | None = None
    workflow_id: str | None = None
    job_id: ScopeId | None = None
    session_id: str | None = None
    memory_types: tuple[str, ...] = ()
    lifecycle_statuses: tuple[str, ...] = ("active", "candidate")
    visibilities: tuple[str, ...] = ("project", "account", "tenant", "system")
    source_trusts: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    source_types: tuple[str, ...] = ()
    include_private_user: bool = False
    # When true, `project_id` restricts instead of widens. General retrieval must
    # keep widening — account-level memories are meant to reach a project's runs —
    # but a surface that presents "this project's knowledge" has to exclude records
    # that belong to no project at all (AUDIT-018).
    project_scope_strict: bool = False
    prefer_active: bool = True
    prefer_non_superseded: bool = True
    only_safe: bool = True
    limit: int = 10
    offset: int = 0


@dataclass(slots=True)
class MemorySearchResult:
    record: MemoryRecord
    score: float
    citation: Citation
    matched_terms: list[str] = field(default_factory=list)


class HybridRetrievalBackend(Protocol):
    supports_vector_search: bool
    supports_text_search: bool
    supports_graph_expansion: bool
    supports_acl_pushdown: bool
    supports_hybrid_single_query: bool
    co_location_group: str

    def search_records(
        self,
        *,
        query: str,
        filters: MemorySearchFilters,
    ) -> list[MemorySearchResult]: ...

    def list_records(
        self,
        *,
        filters: MemorySearchFilters,
    ) -> list[MemoryRecord]: ...

    def count_records(
        self,
        *,
        query: str,
        filters: MemorySearchFilters,
    ) -> int: ...


class TextSearchBackend(Protocol):
    def search_records(
        self,
        *,
        query: str,
        filters: MemorySearchFilters,
    ) -> list[MemorySearchResult]: ...

    def list_records(
        self,
        *,
        filters: MemorySearchFilters,
    ) -> list[MemoryRecord]: ...

    def count_records(
        self,
        *,
        query: str,
        filters: MemorySearchFilters,
    ) -> int: ...


class VectorBackend(Protocol):
    def search_records(
        self,
        *,
        query: str,
        filters: MemorySearchFilters,
    ) -> list[MemorySearchResult]: ...

    def index_record(
        self,
        record: MemoryRecord,
        *,
        repository: MemoryRepository | None = None,
    ) -> None: ...

    def delete_project_scope(
        self,
        *,
        account_id: str,
        project_id: ScopeId,
        repository: MemoryRepository | None = None,
    ) -> None: ...


class KnowledgeBackend(Protocol):
    def load_job_source(self, *, job_id: ScopeId) -> dict[str, Any] | None: ...

    def load_job_agent_messages(
        self,
        *,
        job_id: ScopeId,
        limit: int = 200,
    ) -> list[dict[str, Any]]: ...

    def load_project_profile_record(
        self,
        *,
        account_id: str,
        project_id: ScopeId,
    ) -> MemoryRecord | None: ...

    def load_exact_project_profile_record(
        self,
        *,
        account_id: str,
        project_id: ScopeId,
    ) -> MemoryRecord | None: ...

    def record_audit_event(
        self,
        *,
        account_id: str,
        actor_id: str,
        action_type: str,
        project_id: ScopeId | None = None,
        job_id: ScopeId | None = None,
        task_id: str = "",
        task_summary: str = "",
        status: str | None = None,
        payload: Mapping[str, Any] | None = None,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> None: ...


@dataclass(slots=True)
class CuratedRecordCandidate:
    record: MemoryRecordInput
    decision: str
    recommended_action: str
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    possible_conflicts: list[str] = field(default_factory=list)
    extracted_entities: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CuratedBatch:
    source_kind: str
    source_id: str
    candidates: list[CuratedRecordCandidate] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    possible_conflicts: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ContextAssemblyRequest:
    account_id: str
    actor_id: str
    token_budget: int
    tenant_id: str | None = None
    project_id: ScopeId | None = None
    session_id: str | None = None
    job_id: ScopeId | None = None
    workflow_id: str | None = None
    intent: str = ""
    allowed_capabilities: tuple[str, ...] = ()
    requested_categories: tuple[str, ...] = DEFAULT_CONTEXT_CATEGORIES
    memory_policy: Mapping[str, Any] | None = None


@dataclass(slots=True)
class ContextAssemblyResult:
    project_profile_context: list[dict[str, Any]] = field(default_factory=list)
    project_memory: list[dict[str, Any]] = field(default_factory=list)
    workflow_memory: list[dict[str, Any]] = field(default_factory=list)
    procedural_memory: list[dict[str, Any]] = field(default_factory=list)
    docs_memory: list[dict[str, Any]] = field(default_factory=list)
    graph_context: list[dict[str, Any]] = field(default_factory=list)
    dataset_context: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    token_usage: int = 0


class MemoryRepository(Protocol):
    def get_record(self, record_id: str) -> MemoryRecord | None: ...

    def find_record_by_source(
        self,
        *,
        account_id: str,
        source_type: str,
        source_id: str,
    ) -> MemoryRecord | None: ...

    def upsert_record(self, payload: MemoryRecordInput) -> MemoryRecord: ...

    def update_record_embedding(
        self,
        record_id: str,
        embedding: Sequence[float],
    ) -> MemoryRecord | None: ...

    def update_record_status(
        self,
        record_id: str,
        *,
        lifecycle_status: str,
        supersedes_id: str | None = None,
        expires_at: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> MemoryRecord | None: ...

    def transaction(self) -> AbstractContextManager[None]: ...

    def add_post_commit_hook(self, callback: Any) -> None: ...

    def search_records(
        self,
        *,
        query: str,
        filters: MemorySearchFilters,
    ) -> list[MemorySearchResult]: ...

    def list_records(
        self,
        *,
        filters: MemorySearchFilters,
    ) -> list[MemoryRecord]: ...

    def count_records(
        self,
        *,
        query: str,
        filters: MemorySearchFilters,
    ) -> int: ...

    def delete_project_scope(
        self,
        *,
        account_id: str,
        project_id: ScopeId,
    ) -> None: ...

    def unified_hybrid_search_records(
        self,
        *,
        query: str,
        filters: MemorySearchFilters,
        include_vector: bool = False,
        include_graph: bool = False,
        query_embedding: Sequence[float] | None = None,
    ) -> list[MemorySearchResult]: ...

    def create_or_get_entity(
        self,
        *,
        account_id: str,
        entity_type: str,
        canonical_name: str,
        normalized_name: str,
        created_by: str,
        tenant_id: str | None = None,
        project_id: ScopeId | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> MemoryEntity: ...

    def create_alias(
        self,
        *,
        entity_id: str,
        alias: str,
        normalized_alias: str,
        created_by: str,
        language_code: str = "",
        provenance: Mapping[str, Any] | None = None,
    ) -> MemoryEntityAlias: ...

    def lookup_entity_by_alias(
        self,
        *,
        account_id: str,
        normalized_alias: str,
        language_code: str = "",
        project_id: ScopeId | None = None,
    ) -> MemoryEntity | None: ...

    def link_record_entity(self, payload: EntityLinkInput) -> dict[str, Any]: ...

    def create_edge(self, payload: MemoryEdgeInput) -> MemoryEdge: ...

    def context_neighbors(
        self,
        *,
        account_id: str,
        actor_id: str = "",
        tenant_id: str | None = None,
        project_id: ScopeId | None = None,
        record_ids: Sequence[str] = (),
        entity_ids: Sequence[str] = (),
        include_private_user: bool = False,
        lifecycle_statuses: Sequence[str] = ("active", "candidate"),
        only_safe: bool = True,
        limit: int = 20,
    ) -> list[dict[str, Any]]: ...

    def load_job_source(self, *, job_id: ScopeId) -> dict[str, Any] | None: ...

    def load_job_agent_messages(
        self, *, job_id: ScopeId, limit: int = 200
    ) -> list[dict[str, Any]]: ...

    def load_project_profile_record(
        self,
        *,
        account_id: str,
        project_id: ScopeId,
    ) -> MemoryRecord | None: ...

    def load_exact_project_profile_record(
        self,
        *,
        account_id: str,
        project_id: ScopeId,
    ) -> MemoryRecord | None: ...

    def record_audit_event(
        self,
        *,
        account_id: str,
        actor_id: str,
        action_type: str,
        project_id: ScopeId | None = None,
        job_id: ScopeId | None = None,
        task_id: str = "",
        task_summary: str = "",
        status: str | None = None,
        payload: Mapping[str, Any] | None = None,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> None: ...
