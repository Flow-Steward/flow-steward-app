"""Context graph backend implementations for memory services."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .models import EntityLinkInput, MemoryEdge, MemoryEdgeInput, MemoryRepository, ScopeId


class NullContextGraphBackend:
    def link_record_entity(self, payload: EntityLinkInput) -> dict[str, Any]:
        del payload
        return {}

    def create_edge(self, payload: MemoryEdgeInput) -> MemoryEdge:
        raise RuntimeError("Context graph backend is not configured")

    def neighbors(
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
    ) -> list[dict[str, Any]]:
        del (
            account_id,
            actor_id,
            tenant_id,
            project_id,
            record_ids,
            entity_ids,
            include_private_user,
            lifecycle_statuses,
            only_safe,
            limit,
        )
        return []


class PostgresAgeContextGraphBackend:
    def __init__(self, *, repository: MemoryRepository) -> None:
        self._repository = repository

    def link_record_entity(self, payload: EntityLinkInput) -> dict[str, Any]:
        return self._repository.link_record_entity(payload)

    def create_edge(self, payload: MemoryEdgeInput) -> MemoryEdge:
        return self._repository.create_edge(payload)

    def neighbors(
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
    ) -> list[dict[str, Any]]:
        return self._repository.context_neighbors(
            account_id=account_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
            project_id=project_id,
            record_ids=record_ids,
            entity_ids=entity_ids,
            include_private_user=include_private_user,
            lifecycle_statuses=lifecycle_statuses,
            only_safe=only_safe,
            limit=limit,
        )
