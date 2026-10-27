from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict
from typing import Any

from core.infrastructure.config.memory_config import POSTGRES_COLOCATION_GROUP, BackendCapabilities
from core.infrastructure.runtime.embedding_cache import cached_openai_embedding

from .models import (
    Citation,
    HybridRetrievalBackend,
    KnowledgeBackend,
    MemoryRecord,
    MemoryRepository,
    MemorySearchFilters,
    MemorySearchResult,
    ScopeId,
    TextSearchBackend,
    VectorBackend,
)
from .policy import MemoryPolicyResolver, trust_rank


def record_to_citation(record: MemoryRecord, *, snippet: str = "") -> Citation:
    return Citation(
        record_id=record.id,
        title=record.title,
        source_type=record.source_type,
        source_id=record.source_id,
        source_trust=record.source_trust,
        snippet=snippet or record.summary or record.content[:240],
        confidence=record.confidence,
        provenance={
            "memory_type": record.memory_type,
            "visibility": record.visibility,
            "lifecycle_status": record.lifecycle_status,
            "evidence_refs": [asdict(ref) for ref in record.evidence_refs],
            "metadata": dict(record.metadata or {}),
        },
    )


class MemoryRetrievalService:
    def __init__(
        self,
        *,
        retrieval_backend: HybridRetrievalBackend | None = None,
        text_search_backend: TextSearchBackend | None = None,
        knowledge_backend: KnowledgeBackend,
        vector_backend: VectorBackend | None = None,
        policy_resolver: MemoryPolicyResolver | None = None,
    ) -> None:
        if retrieval_backend is None and text_search_backend is None:
            raise ValueError("MemoryRetrievalService requires a retrieval backend")
        self._retrieval_backend = retrieval_backend or _TextOnlyRetrievalBackend(
            text_search_backend=text_search_backend,
        )
        self._knowledge = knowledge_backend
        self._vector = vector_backend
        self._policy_resolver = policy_resolver or MemoryPolicyResolver()

    @staticmethod
    def _rank_key(result: MemorySearchResult) -> tuple[float, int, float, str, str]:
        record = result.record
        return (
            float(result.score),
            trust_rank(record.source_trust),
            float(record.importance or 0.0),
            str(record.updated_at or record.created_at or ""),
            str(record.id),
        )

    def search(
        self,
        query: str,
        *,
        filters: MemorySearchFilters,
    ) -> list[MemorySearchResult]:
        results = self._retrieval_backend.search_records(query=query, filters=filters)
        return sorted(results, key=self._rank_key, reverse=True)

    def list_records(self, *, filters: MemorySearchFilters) -> list[MemoryRecord]:
        records = self._retrieval_backend.list_records(filters=filters)
        return sorted(
            records,
            key=lambda record: (
                1 if record.lifecycle_status == "active" else 0,
                trust_rank(record.source_trust),
                float(record.importance or 0.0),
                str(record.updated_at or record.created_at or ""),
                str(record.id),
            ),
            reverse=True,
        )

    def count_records(self, query: str, *, filters: MemorySearchFilters) -> int:
        normalized_query = str(query or "").strip() or "*"
        return int(
            self._retrieval_backend.count_records(query=normalized_query, filters=filters) or 0
        )

    def query_with_policy(
        self,
        query: str,
        *,
        account_id: str,
        actor_id: str = "",
        tenant_id: str | None = None,
        project_id: ScopeId | None = None,
        workflow_id: str | None = None,
        session_id: str | None = None,
        job_id: ScopeId | None = None,
        memory_types: Iterable[str] | None = None,
        lifecycle_statuses: Iterable[str] | None = None,
        source_trusts: Iterable[str] | None = None,
        tags: Iterable[str] | None = None,
        source_types: Iterable[str] | None = None,
        limit: int = 10,
        offset: int = 0,
        include_private_user: bool = False,
        project_scope_strict: bool = False,
        prefer_active: bool = True,
    ) -> list[MemorySearchResult]:
        filters = self._policy_resolver.resolve_filters(
            account_id=account_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
            project_id=project_id,
            workflow_id=workflow_id,
            session_id=session_id,
            job_id=job_id,
            memory_types=memory_types,
            lifecycle_statuses=lifecycle_statuses,
            source_trusts=source_trusts,
            tags=tags,
            source_types=source_types,
            limit=limit,
            offset=offset,
            include_private_user=include_private_user,
            project_scope_strict=project_scope_strict,
            prefer_active=prefer_active,
        )
        return self.search(query, filters=filters)

    def project_profile(
        self,
        *,
        account_id: str,
        project_id: ScopeId,
    ) -> MemoryRecord | None:
        return self._knowledge.load_project_profile_record(
            account_id=account_id,
            project_id=project_id,
        )

    def exact_project_profile(
        self,
        *,
        account_id: str,
        project_id: ScopeId,
    ) -> MemoryRecord | None:
        return self._knowledge.load_exact_project_profile_record(
            account_id=account_id,
            project_id=project_id,
        )

    def citations_for(self, results: Iterable[MemorySearchResult]) -> list[dict[str, Any]]:
        citations = []
        for result in results:
            citation = record_to_citation(result.record, snippet=result.citation.snippet)
            citations.append(asdict(citation))
        return citations


class _TextOnlyRetrievalBackend:
    supports_vector_search = False
    supports_text_search = True
    supports_graph_expansion = False
    supports_acl_pushdown = True
    supports_hybrid_single_query = False
    co_location_group = POSTGRES_COLOCATION_GROUP

    def __init__(self, *, text_search_backend: TextSearchBackend | None) -> None:
        if text_search_backend is None:
            raise ValueError("text_search_backend is required")
        self._text = text_search_backend

    def search_records(
        self, *, query: str, filters: MemorySearchFilters
    ) -> list[MemorySearchResult]:
        return self._text.search_records(query=query, filters=filters)

    def list_records(self, *, filters: MemorySearchFilters) -> list[MemoryRecord]:
        return self._text.list_records(filters=filters)

    def count_records(self, *, query: str, filters: MemorySearchFilters) -> int:
        return int(self._text.count_records(query=query, filters=filters) or 0)


class CandidateVectorBackend:
    supports_vector_search = True
    supports_text_search = False
    supports_graph_expansion = False
    supports_acl_pushdown = False
    supports_hybrid_single_query = False
    co_location_group = "external"

    def candidate_ids(self, *, query: str, limit: int) -> list[tuple[str, float]]:
        del query, limit
        return []

    @staticmethod
    def _embedding_key(account_id: str) -> str | None:
        from core.application.settings.embedding_credentials import (
            resolve_embedding_openai_api_key,
        )
        from core.infrastructure.db.platform_stores import PlatformConnectionStore

        logger = PlatformConnectionStore.get_shared_instance()
        return resolve_embedding_openai_api_key(account_id=account_id, conn=logger.conn)

    def embed_query(self, query: str, *, account_id: str = "") -> list[float]:
        api_key = self._embedding_key(account_id)
        normalized_query = str(query or "").strip()
        if not api_key or not normalized_query:
            return []
        return list(
            cached_openai_embedding(
                api_key,
                "text-embedding-3-small",
                normalized_query,
            )
        )

    def embed_record(self, record: MemoryRecord) -> list[float]:
        text = "\n\n".join(
            part
            for part in (record.title, record.summary, record.content)
            if str(part or "").strip()
        )
        return self.embed_query(text, account_id=record.account_id)

    def index_record(
        self,
        record: MemoryRecord,
        *,
        repository: MemoryRepository | None = None,
    ) -> None:
        del record, repository

    def search_records(
        self, *, query: str, filters: MemorySearchFilters
    ) -> list[MemorySearchResult]:
        del query, filters
        return []

    def delete_project_scope(
        self,
        *,
        account_id: str,
        project_id: ScopeId,
        repository: MemoryRepository | None = None,
    ) -> None:
        del account_id, project_id, repository


class PostgresPgVectorBackend(CandidateVectorBackend):
    co_location_group = POSTGRES_COLOCATION_GROUP
    supports_acl_pushdown = True
    supports_hybrid_single_query = True

    def embed_query(self, query: str, *, account_id: str = "") -> list[float]:
        return super().embed_query(query, account_id=account_id)

    def index_record(
        self,
        record: MemoryRecord,
        *,
        repository: MemoryRepository | None = None,
    ) -> None:
        if repository is None:
            raise RuntimeError("Postgres pgvector indexing requires the memory repository")
        embedding = self.embed_record(record)
        if not embedding:
            return
        repository.update_record_embedding(record.id, embedding)

    def delete_project_scope(
        self,
        *,
        account_id: str,
        project_id: ScopeId,
        repository: MemoryRepository | None = None,
    ) -> None:
        del account_id, project_id, repository


class PostgresUnifiedRetrievalBackend:
    def __init__(
        self,
        *,
        repository: MemoryRepository,
        capabilities: BackendCapabilities,
        vector_backend: CandidateVectorBackend | None = None,
    ) -> None:
        self._repository = repository
        self.supports_vector_search = capabilities.supports_vector_search
        self.supports_text_search = capabilities.supports_text_search
        self.supports_graph_expansion = capabilities.supports_graph_expansion
        self.supports_acl_pushdown = capabilities.supports_acl_pushdown
        self.supports_hybrid_single_query = capabilities.supports_hybrid_single_query
        self.co_location_group = capabilities.co_location_group
        self._vector_backend = vector_backend

    def search_records(
        self, *, query: str, filters: MemorySearchFilters
    ) -> list[MemorySearchResult]:
        query_embedding = None
        vector_backend = self._vector_backend
        if self.supports_vector_search and vector_backend is not None:
            query_embedding = vector_backend.embed_query(query, account_id=filters.account_id)
        include_vector = query_embedding is not None and len(query_embedding) > 0
        return self._repository.unified_hybrid_search_records(
            query=query,
            filters=filters,
            include_vector=include_vector,
            include_graph=self.supports_graph_expansion,
            query_embedding=query_embedding,
        )

    def list_records(self, *, filters: MemorySearchFilters) -> list[MemoryRecord]:
        return self._repository.list_records(filters=filters)

    def count_records(self, *, query: str, filters: MemorySearchFilters) -> int:
        text_total = int(self._repository.count_records(query=query, filters=filters) or 0)
        vector_backend = self._vector_backend
        if not self.supports_vector_search or vector_backend is None:
            return text_total
        query_embedding = vector_backend.embed_query(query, account_id=filters.account_id)
        if query_embedding is None or len(query_embedding) == 0:
            return text_total
        count_filters = MemorySearchFilters(
            **{
                **asdict(filters),
                "limit": max(filters.limit + filters.offset, filters.limit, 5000),
                "offset": 0,
            }
        )
        results = self._repository.unified_hybrid_search_records(
            query=query,
            filters=count_filters,
            include_vector=True,
            include_graph=self.supports_graph_expansion,
            query_embedding=query_embedding,
        )
        return max(text_total, len({result.record.id for result in results}))


def _record_allowed(record: MemoryRecord, filters: MemorySearchFilters) -> bool:
    def same_scope(left: ScopeId | None, right: ScopeId | None) -> bool:
        if left is None or right is None:
            return left is None and right is None
        return str(left).strip() == str(right).strip()

    if record.account_id != filters.account_id:
        return False
    if filters.tenant_id and record.tenant_id not in {filters.tenant_id, None}:
        return False
    if filters.project_id is not None:
        if filters.project_scope_strict:
            if not same_scope(record.project_id, filters.project_id):
                return False
        elif not (record.project_id is None or same_scope(record.project_id, filters.project_id)):
            return False
    if filters.workflow_id and record.workflow_id not in {filters.workflow_id, None}:
        return False
    if filters.job_id is not None and not (
        record.job_id is None or same_scope(record.job_id, filters.job_id)
    ):
        return False
    if filters.session_id and record.session_id not in {filters.session_id, None}:
        return False
    if filters.memory_types and record.memory_type not in filters.memory_types:
        return False
    if filters.lifecycle_statuses and record.lifecycle_status not in filters.lifecycle_statuses:
        return False
    if filters.visibilities and record.visibility not in filters.visibilities:
        return False
    if record.visibility == "private_user" and (
        not filters.include_private_user or record.created_by != filters.actor_id
    ):
        return False
    if filters.source_trusts and record.source_trust not in filters.source_trusts:
        return False
    if filters.source_types and record.source_type not in filters.source_types:
        return False
    if filters.tags and not (set(filters.tags) & set(record.tags or [])):
        return False
    if filters.only_safe:
        metadata = dict(record.metadata or {})
        if str(metadata.get("unsafe") or "").lower() == "true":
            return False
        if str(metadata.get("sensitive") or "").lower() == "true":
            return False
    return not (filters.prefer_non_superseded and record.lifecycle_status == "superseded")


# Reserved for future adapters; intentionally kept internal in this release.
class _FederatedRetrievalBackend:
    supports_vector_search = True
    supports_text_search = True
    supports_graph_expansion = True
    supports_acl_pushdown = False
    supports_hybrid_single_query = False
    co_location_group = "federated"

    def __init__(
        self,
        *,
        repository: MemoryRepository,
        text_search_backend: TextSearchBackend,
        vector_backend: CandidateVectorBackend | None = None,
    ) -> None:
        self._repository = repository
        self._text = text_search_backend
        self._vector = vector_backend

    def search_records(
        self, *, query: str, filters: MemorySearchFilters
    ) -> list[MemorySearchResult]:
        text_filters = MemorySearchFilters(
            **{
                **asdict(filters),
                "limit": max(filters.limit + filters.offset, filters.limit, 10),
                "offset": 0,
            }
        )
        candidates = self._text.search_records(query=query, filters=text_filters)
        by_id = {item.record.id: item for item in candidates}
        if self._vector is not None:
            for record_id, score in self._vector.candidate_ids(
                query=query,
                limit=max(filters.limit + filters.offset, filters.limit, 10),
            ):
                record = self._repository.get_record(record_id)
                if record is None or not _record_allowed(record, filters):
                    continue
                existing = by_id.get(record.id)
                vector_result = MemorySearchResult(
                    record=record,
                    score=score,
                    citation=record_to_citation(record),
                    matched_terms=[],
                )
                if existing is None or vector_result.score > existing.score:
                    by_id[record.id] = vector_result
        ordered = sorted(by_id.values(), key=MemoryRetrievalService._rank_key, reverse=True)
        start = max(0, int(filters.offset or 0))
        end = start + max(1, int(filters.limit or 10))
        return ordered[start:end]

    def list_records(self, *, filters: MemorySearchFilters) -> list[MemoryRecord]:
        return self._text.list_records(filters=filters)

    def count_records(self, *, query: str, filters: MemorySearchFilters) -> int:
        text_total = int(self._text.count_records(query=query, filters=filters) or 0)
        if self._vector is None:
            return text_total

        seen_text_ids: set[str] = set()
        if text_total > 0:
            text_filters = MemorySearchFilters(
                **{
                    **asdict(filters),
                    "limit": min(max(text_total, filters.limit + filters.offset, 100), 5000),
                    "offset": 0,
                }
            )
            seen_text_ids.update(
                result.record.id
                for result in self._text.search_records(query=query, filters=text_filters)
            )

        vector_only = 0
        for record_id, _score in self._vector.candidate_ids(query=query, limit=1000):
            record = self._repository.get_record(record_id)
            if record is None or not _record_allowed(record, filters):
                continue
            if record.id not in seen_text_ids:
                vector_only += 1
                seen_text_ids.add(record.id)
        return text_total + vector_only
