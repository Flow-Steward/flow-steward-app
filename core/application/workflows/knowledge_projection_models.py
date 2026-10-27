from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class WorkflowKnowledgeNode:
    id: str
    kind: str
    label: str
    summary: str = ""
    text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkflowKnowledgeEdge:
    id: str
    kind: str
    source_id: str
    target_id: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkflowKnowledgeDocument:
    id: str
    entity_id: str
    entity_kind: str
    title: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkflowKnowledgeHit:
    entity_id: str
    entity_kind: str
    score: float
    title: str
    snippet: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkflowKnowledgeNeighbor:
    source_id: str
    edge_kind: str
    target_id: str
    depth: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WorkflowKnowledgeProjection:
    nodes: list[WorkflowKnowledgeNode] = field(default_factory=list)
    edges: list[WorkflowKnowledgeEdge] = field(default_factory=list)
    documents: list[WorkflowKnowledgeDocument] = field(default_factory=list)


class WorkflowKnowledgeBackend(Protocol):
    def scope_sync_lock(
        self,
        *,
        account_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
    ) -> Any: ...

    def scope_sync_transaction(self) -> Any: ...

    def get_scope_sync_digest(
        self,
        *,
        account_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
    ) -> str: ...

    def set_scope_sync_digest(
        self,
        *,
        account_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
        digest: str,
    ) -> None: ...

    def upsert_projection(
        self,
        *,
        projection: WorkflowKnowledgeProjection,
        account_id: str = "",
        tenant_id: str | None = None,
        project_id: str | None = None,
    ) -> None: ...

    def prepare_projection_write(
        self,
        *,
        projection: WorkflowKnowledgeProjection,
        account_id: str = "",
        tenant_id: str | None = None,
        project_id: str | None = None,
    ) -> Any: ...

    def apply_prepared_projection(self, *, prepared: Any) -> None: ...

    def delete_scope(
        self,
        *,
        account_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
        workflow_ids: Sequence[str] = (),
    ) -> None: ...

    def search(
        self,
        *,
        query: str,
        account_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
        entity_kinds: Sequence[str] = (),
        limit: int = 8,
    ) -> list[WorkflowKnowledgeHit]: ...

    def neighbors(
        self,
        *,
        entity_id: str,
        account_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
        edge_kinds: Sequence[str] = (),
        depth: int = 1,
        limit: int = 20,
    ) -> list[WorkflowKnowledgeNeighbor]: ...

    def fetch_subgraph(
        self,
        *,
        entity_ids: Sequence[str],
        account_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
        edge_kinds: Sequence[str] = (),
        depth: int = 2,
        limit: int = 100,
    ) -> WorkflowKnowledgeProjection: ...
