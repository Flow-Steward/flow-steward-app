from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from .graphs_context_backends import NullContextGraphBackend, PostgresAgeContextGraphBackend
from .models import EntityLinkInput, MemoryEdge, MemoryEdgeInput, ScopeId


class ContextGraphBackend(Protocol):
    def link_record_entity(self, payload: EntityLinkInput) -> dict[str, Any]: ...

    def create_edge(self, payload: MemoryEdgeInput) -> MemoryEdge: ...

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
    ) -> list[dict[str, Any]]: ...


__all__ = [
    "ContextGraphBackend",
    "NullContextGraphBackend",
    "PostgresAgeContextGraphBackend",
]
