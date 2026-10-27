from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .models import MemoryEntity, MemoryEntityAlias, MemoryRepository, ScopeId, UnresolvedMention
from .policy import normalize_alias


class EntityResolutionService:
    def __init__(self, *, repository: MemoryRepository) -> None:
        self._repository = repository

    def resolve_exact_alias(
        self,
        *,
        account_id: str,
        alias: str,
        language_code: str = "",
        project_id: ScopeId | None = None,
    ) -> MemoryEntity | None:
        return self._repository.lookup_entity_by_alias(
            account_id=account_id,
            normalized_alias=normalize_alias(alias),
            language_code=str(language_code or "").strip(),
            project_id=project_id,
        )

    def create_alias(
        self,
        *,
        entity_id: str,
        alias: str,
        created_by: str,
        language_code: str = "",
        provenance: Mapping[str, Any] | None = None,
    ) -> MemoryEntityAlias:
        return self._repository.create_alias(
            entity_id=entity_id,
            alias=str(alias or "").strip(),
            normalized_alias=normalize_alias(alias),
            created_by=str(created_by or "").strip(),
            language_code=str(language_code or "").strip(),
            provenance=provenance,
        )

    def create_or_get_entity(
        self,
        *,
        account_id: str,
        entity_type: str,
        canonical_name: str,
        created_by: str,
        tenant_id: str | None = None,
        project_id: ScopeId | None = None,
        metadata: Mapping[str, Any] | None = None,
        aliases: Sequence[str] | None = None,
        language_code: str = "",
    ) -> MemoryEntity:
        entity = self._repository.create_or_get_entity(
            account_id=account_id,
            entity_type=str(entity_type or "").strip(),
            canonical_name=str(canonical_name or "").strip(),
            normalized_name=normalize_alias(canonical_name),
            created_by=str(created_by or "").strip(),
            tenant_id=str(tenant_id or "").strip() or None,
            project_id=project_id,
            metadata=metadata,
        )
        for alias in aliases or ():
            alias_text = str(alias or "").strip()
            if not alias_text:
                continue
            self.create_alias(
                entity_id=entity.id,
                alias=alias_text,
                created_by=created_by,
                language_code=language_code,
                provenance={"source": "entity_resolution"},
            )
        return entity

    def unresolved_mentions(
        self,
        *,
        account_id: str,
        mentions: Sequence[str],
        language_code: str = "",
        project_id: ScopeId | None = None,
    ) -> list[UnresolvedMention]:
        unresolved: list[UnresolvedMention] = []
        for mention in mentions:
            text = str(mention or "").strip()
            if not text:
                continue
            entity = self.resolve_exact_alias(
                account_id=account_id,
                alias=text,
                language_code=language_code,
                project_id=project_id,
            )
            if entity is None:
                unresolved.append(
                    UnresolvedMention(
                        mention_text=text,
                        normalized_mention=normalize_alias(text),
                        language_code=str(language_code or "").strip(),
                        provenance={"project_id": project_id},
                    )
                )
        return unresolved
