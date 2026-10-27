# mypy: ignore-errors
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
from typing import Any

from core.application.memory.models import (
    Citation,
    MemoryRecord,
    MemoryRecordInput,
    MemorySearchFilters,
    MemorySearchResult,
    ScopeId,
)
from core.infrastructure.db.memory_store_common import (
    _SEARCH_VECTOR_SQL,
    VALUE_AND_SQL,
    _db_account_id,
    _db_scope_id,
    _db_tenant_id,
    _json_dumps,
    _row_to_record,
)

_MESSAGE_NULL_DOUBLE_PRECISION = "NULL::double precision"


class MemoryRecordStoreMixin:
    def get_record(self, record_id: str) -> MemoryRecord | None:
        row = self._fetchone("SELECT * FROM memory_records WHERE id = %s LIMIT 1", [record_id])
        return _row_to_record(row) if row else None

    def find_record_by_source(
        self,
        *,
        account_id: str,
        source_type: str,
        source_id: str,
    ) -> MemoryRecord | None:
        if not str(source_id or "").strip():
            return None
        row = self._fetchone(
            """
            SELECT *
            FROM memory_records
            WHERE account_id = %s AND source_type = %s AND source_id = %s
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            [_db_account_id(account_id), source_type, source_id],
        )
        return _row_to_record(row) if row else None

    def upsert_record(self, payload: MemoryRecordInput) -> MemoryRecord:
        existing = self.find_record_by_source(
            account_id=payload.account_id,
            source_type=payload.source_type,
            source_id=payload.source_id,
        )
        if existing is None:
            sql = """
            INSERT INTO memory_records (
                account_id, tenant_id, project_id, workflow_id, job_id, session_id,
                memory_type, visibility, lifecycle_status, title, summary, content,
                source_type, source_id, source_trust, evidence_refs_jsonb, confidence,
                importance, valid_from, valid_to, supersedes_id, retention_policy,
                expires_at, tags_jsonb, metadata_jsonb, created_by
            )
            VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s
            )
            RETURNING *
            """
            row = self._fetchone(
                sql,
                [
                    _db_account_id(payload.account_id),
                    _db_tenant_id(payload.tenant_id),
                    _db_scope_id(payload.project_id),
                    payload.workflow_id,
                    _db_scope_id(payload.job_id),
                    payload.session_id,
                    payload.memory_type,
                    payload.visibility,
                    payload.lifecycle_status,
                    payload.title,
                    payload.summary,
                    payload.content,
                    payload.source_type,
                    payload.source_id,
                    payload.source_trust,
                    _json_dumps([asdict(ref) for ref in payload.evidence_refs]),
                    payload.confidence,
                    payload.importance,
                    payload.valid_from,
                    payload.valid_to,
                    payload.supersedes_id,
                    payload.retention_policy,
                    payload.expires_at,
                    _json_dumps(payload.tags),
                    _json_dumps(payload.metadata),
                    payload.created_by,
                ],
            )
        else:
            row = self._fetchone(
                """
                UPDATE memory_records
                SET tenant_id = %s,
                    project_id = %s,
                    workflow_id = %s,
                    job_id = %s,
                    session_id = %s,
                    memory_type = %s,
                    visibility = %s,
                    lifecycle_status = %s,
                    title = %s,
                    summary = %s,
                    content = %s,
                    source_trust = %s,
                    evidence_refs_jsonb = %s,
                    confidence = %s,
                    importance = %s,
                    valid_from = %s,
                    valid_to = %s,
                    supersedes_id = %s,
                    retention_policy = %s,
                    expires_at = %s,
                    tags_jsonb = %s,
                    metadata_jsonb = %s,
                    created_by = %s,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                [
                    _db_tenant_id(payload.tenant_id),
                    _db_scope_id(payload.project_id),
                    payload.workflow_id,
                    _db_scope_id(payload.job_id),
                    payload.session_id,
                    payload.memory_type,
                    payload.visibility,
                    payload.lifecycle_status,
                    payload.title,
                    payload.summary,
                    payload.content,
                    payload.source_trust,
                    _json_dumps([asdict(ref) for ref in payload.evidence_refs]),
                    payload.confidence,
                    payload.importance,
                    payload.valid_from,
                    payload.valid_to,
                    payload.supersedes_id,
                    payload.retention_policy,
                    payload.expires_at,
                    _json_dumps(payload.tags),
                    _json_dumps(payload.metadata),
                    payload.created_by,
                    existing.id,
                ],
            )
        self._maybe_commit()
        if row is None:
            raise RuntimeError("Memory upsert did not return a row")
        return _row_to_record(row)

    def update_record_embedding(
        self,
        record_id: str,
        embedding: Sequence[float],
    ) -> MemoryRecord | None:
        row = self._fetchone(
            """
            UPDATE memory_records
            SET embedding = %s::vector,
                updated_at = NOW()
            WHERE id = %s
            RETURNING *
            """,
            [list(embedding), record_id],
        )
        self._maybe_commit()
        return _row_to_record(row) if row else None

    def update_record_status(
        self,
        record_id: str,
        *,
        lifecycle_status: str,
        supersedes_id: str | None = None,
        expires_at: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> MemoryRecord | None:
        row = self._fetchone(
            """
            UPDATE memory_records
            SET lifecycle_status = %s,
                supersedes_id = COALESCE(%s, supersedes_id),
                expires_at = COALESCE(%s, expires_at),
                valid_to = COALESCE(%s, valid_to),
                updated_at = NOW()
            WHERE id = %s
            RETURNING *
            """,
            [lifecycle_status, supersedes_id, expires_at, valid_to, record_id],
        )
        self._maybe_commit()
        return _row_to_record(row) if row else None

    def _search_plan(
        self,
        *,
        filters: MemorySearchFilters,
        query: str,
        enforce_query_match: bool = True,
    ) -> tuple[list[str], list[Any], str, list[Any]]:
        conditions = ["account_id = %s"]
        where_params: list[Any] = [_db_account_id(filters.account_id)]
        score_params: list[Any] = []
        score_sql: str
        normalized_query = " ".join(str(query or "").split()).strip()

        if filters.tenant_id:
            conditions.append("(tenant_id = %s OR tenant_id IS NULL)")
            where_params.append(_db_tenant_id(filters.tenant_id))
        if filters.project_id is not None:
            # `OR project_id IS NULL` lets account/system memories reach a project's
            # runs, which is wanted for retrieval and wrong for a project-owned
            # listing: every project's KB page showed all 35 account-level records
            # (AUDIT-018).
            conditions.append(
                "project_id = %s"
                if filters.project_scope_strict
                else "(project_id = %s OR project_id IS NULL)"
            )
            where_params.append(_db_scope_id(filters.project_id))
        if filters.workflow_id:
            conditions.append("(workflow_id = %s OR workflow_id IS NULL)")
            where_params.append(filters.workflow_id)
        if filters.job_id is not None:
            conditions.append("(job_id = %s OR job_id IS NULL)")
            where_params.append(_db_scope_id(filters.job_id))
        if filters.session_id:
            conditions.append("(session_id = %s OR session_id IS NULL)")
            where_params.append(filters.session_id)
        if filters.memory_types:
            conditions.append("memory_type = ANY(%s)")
            where_params.append(list(filters.memory_types))
        if filters.lifecycle_statuses:
            conditions.append("lifecycle_status = ANY(%s)")
            where_params.append(list(filters.lifecycle_statuses))
        if filters.visibilities:
            conditions.append("visibility = ANY(%s)")
            where_params.append(list(filters.visibilities))
        if filters.include_private_user and filters.actor_id:
            conditions.append(
                "((visibility = 'private_user' AND created_by = %s) OR visibility <> 'private_user')"
            )
            where_params.append(filters.actor_id)
        else:
            conditions.append("visibility <> 'private_user'")
        if filters.source_trusts:
            conditions.append("source_trust = ANY(%s)")
            where_params.append(list(filters.source_trusts))
        if filters.source_types:
            conditions.append("source_type = ANY(%s)")
            where_params.append(list(filters.source_types))
        if filters.tags:
            conditions.append(
                """
                EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements_text(COALESCE(tags_jsonb, '[]'::jsonb)) AS tag(value)
                    WHERE tag.value = ANY(%s)
                )
                """
            )
            where_params.append(list(filters.tags))
        if filters.only_safe:
            conditions.append("COALESCE(metadata_jsonb->>'unsafe', 'false') <> 'true'")
            conditions.append("COALESCE(metadata_jsonb->>'sensitive', 'false') <> 'true'")
        if filters.prefer_non_superseded:
            conditions.append("lifecycle_status <> 'superseded'")

        if normalized_query and normalized_query != "*":
            like_value = (
                "%"
                + normalized_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                + "%"
            )
            if enforce_query_match:
                conditions.append(
                    f"""(
                        {_SEARCH_VECTOR_SQL} @@ websearch_to_tsquery('simple', %s)
                        OR title ILIKE %s ESCAPE '\\'
                        OR summary ILIKE %s ESCAPE '\\'
                        OR content ILIKE %s ESCAPE '\\'
                    )"""
                )
                where_params.extend([normalized_query, like_value, like_value, like_value])
            score_sql = (
                f"ts_rank_cd({_SEARCH_VECTOR_SQL}, websearch_to_tsquery('simple', %s)) + "
                "CASE WHEN lifecycle_status = 'active' THEN 0.15 ELSE 0.0 END + "
                "CASE source_trust "
                "WHEN 'canonical_doc' THEN 0.12 "
                "WHEN 'human_approved' THEN 0.10 "
                "WHEN 'tool_observed' THEN 0.08 "
                "WHEN 'system_generated' THEN 0.06 "
                "WHEN 'user_supplied' THEN 0.04 "
                "ELSE 0.02 END + "
                "COALESCE(importance, 0)"
            )
            score_params.append(normalized_query)
        else:
            score_sql = (
                "CASE WHEN lifecycle_status = 'active' THEN 0.15 ELSE 0.0 END + "
                "CASE source_trust "
                "WHEN 'canonical_doc' THEN 0.12 "
                "WHEN 'human_approved' THEN 0.10 "
                "WHEN 'tool_observed' THEN 0.08 "
                "WHEN 'system_generated' THEN 0.06 "
                "WHEN 'user_supplied' THEN 0.04 "
                "ELSE 0.02 END + "
                "COALESCE(importance, 0)"
            )
        return conditions, where_params, score_sql, score_params

    def search_records(
        self,
        *,
        query: str,
        filters: MemorySearchFilters,
    ) -> list[MemorySearchResult]:
        conditions, where_params, score_sql, score_params = self._search_plan(
            filters=filters,
            query=query,
        )
        rows = self._fetchall(
            f"""
            SELECT *, ({score_sql}) AS search_score
            FROM memory_records
            WHERE {VALUE_AND_SQL.join(conditions)}
            ORDER BY
                CASE WHEN lifecycle_status = 'active' THEN 0 ELSE 1 END,
                CASE WHEN supersedes_id IS NULL THEN 0 ELSE 1 END,
                search_score DESC,
                updated_at DESC,
                id DESC
            LIMIT %s OFFSET %s
            """,
            [*score_params, *where_params, filters.limit, filters.offset],
        )
        results: list[MemorySearchResult] = []
        normalized_query = " ".join(str(query or "").split()).strip()
        matched_terms = [part for part in normalized_query.split() if part]
        for row in rows:
            record = _row_to_record(row)
            citation = Citation(
                record_id=record.id,
                title=record.title,
                source_type=record.source_type,
                source_id=record.source_id,
                source_trust=record.source_trust,
                snippet=record.summary or record.content[:240],
                confidence=record.confidence,
                provenance={
                    "memory_type": record.memory_type,
                    "visibility": record.visibility,
                    "lifecycle_status": record.lifecycle_status,
                    "evidence_refs": [asdict(ref) for ref in record.evidence_refs],
                },
            )
            results.append(
                MemorySearchResult(
                    record=record,
                    score=float(row.get("search_score") or 0.0),
                    citation=citation,
                    matched_terms=matched_terms,
                )
            )
        return results

    def list_records(
        self,
        *,
        filters: MemorySearchFilters,
    ) -> list[MemoryRecord]:
        conditions, where_params, _score_sql, _score_params = self._search_plan(
            filters=filters,
            query="*",
        )
        rows = self._fetchall(
            f"""
            SELECT *
            FROM memory_records
            WHERE {VALUE_AND_SQL.join(conditions)}
            ORDER BY
                CASE WHEN lifecycle_status = 'active' THEN 0 ELSE 1 END,
                CASE WHEN supersedes_id IS NULL THEN 0 ELSE 1 END,
                importance DESC,
                updated_at DESC,
                id DESC
            LIMIT %s OFFSET %s
            """,
            [*where_params, filters.limit, filters.offset],
        )
        return [_row_to_record(row) for row in rows]

    def count_records(
        self,
        *,
        query: str,
        filters: MemorySearchFilters,
    ) -> int:
        conditions, where_params, _score_sql, _score_params = self._search_plan(
            filters=filters,
            query=query,
        )
        row = self._fetchone(
            f"""
            SELECT COUNT(*) AS total
            FROM memory_records
            WHERE {VALUE_AND_SQL.join(conditions)}
            """,
            where_params,
        )
        return int((row or {}).get("total") or 0)

    def delete_project_scope(
        self,
        *,
        account_id: str,
        project_id: ScopeId,
    ) -> None:
        normalized_project_id = _db_scope_id(project_id)
        if normalized_project_id is None:
            return
        normalized_account_id = _db_account_id(account_id)
        with self._current_conn().cursor() as cur:
            cur.execute(
                """
                DELETE FROM memory_edges
                WHERE account_id = %s AND project_id = %s
                """,
                (normalized_account_id, normalized_project_id),
            )
            cur.execute(
                """
                DELETE FROM memory_record_entities
                WHERE record_id IN (
                    SELECT id
                    FROM memory_records
                    WHERE account_id = %s AND project_id = %s
                )
                """,
                (normalized_account_id, normalized_project_id),
            )
            cur.execute(
                """
                DELETE FROM memory_records
                WHERE account_id = %s AND project_id = %s
                """,
                (normalized_account_id, normalized_project_id),
            )
            cur.execute(
                """
                DELETE FROM memory_entity_aliases
                WHERE entity_id IN (
                    SELECT id
                    FROM memory_entities
                    WHERE account_id = %s AND project_id = %s
                )
                """,
                (normalized_account_id, normalized_project_id),
            )
            cur.execute(
                """
                DELETE FROM memory_entities
                WHERE account_id = %s AND project_id = %s
                """,
                (normalized_account_id, normalized_project_id),
            )
        self._maybe_commit()

    def unified_hybrid_search_records(
        self,
        *,
        query: str,
        filters: MemorySearchFilters,
        include_vector: bool = False,
        include_graph: bool = False,
        query_embedding: Sequence[float] | None = None,
    ) -> list[MemorySearchResult]:
        text_conditions, text_params, score_sql, score_params = self._search_plan(
            filters=filters,
            query=query,
        )
        policy_conditions, policy_params, _policy_score_sql, _policy_score_params = (
            self._search_plan(
                filters=filters,
                query=query,
                enforce_query_match=False,
            )
        )
        text_where = VALUE_AND_SQL.join(text_conditions)
        policy_where = VALUE_AND_SQL.join(policy_conditions)
        vector_sql_base = _MESSAGE_NULL_DOUBLE_PRECISION
        vector_sql_record = _MESSAGE_NULL_DOUBLE_PRECISION
        vector_sql_candidate = _MESSAGE_NULL_DOUBLE_PRECISION
        vector_params: list[Any] = []
        vector_hits_sql = "SELECT * FROM memory_records WHERE false"
        vector_hits_params: list[Any] = []
        if include_vector:
            if not query_embedding:
                raise RuntimeError("Postgres pgvector retrieval requires a query embedding")
            vector_sql_base = "CASE WHEN b.embedding IS NULL THEN NULL ELSE 1.0 - (b.embedding <=> %s::vector) END"
            vector_sql_record = "CASE WHEN r.embedding IS NULL THEN NULL ELSE 1.0 - (r.embedding <=> %s::vector) END"
            vector_sql_candidate = "CASE WHEN v.embedding IS NULL THEN NULL ELSE 1.0 - (v.embedding <=> %s::vector) END"
            vector_params.append(list(query_embedding))
            vector_hits_sql = f"SELECT * FROM memory_records WHERE {policy_where} AND embedding IS NOT NULL ORDER BY embedding <=> %s::vector LIMIT %s"
            vector_hits_params = [
                *policy_params,
                list(query_embedding),
                max(filters.limit + filters.offset, filters.limit, 10),
            ]
        graph_sql = ", graph_hits AS (SELECT NULL::text AS record_id WHERE false)"
        if include_graph:
            graph_sql = """
            , graph_hits AS (
                SELECT DISTINCT
                    CASE
                        WHEN e.from_record_id = b.id THEN e.to_record_id
                        ELSE e.from_record_id
                    END AS record_id
                FROM text_base b
                JOIN memory_edges e
                  ON e.account_id = b.account_id
                 AND (e.from_record_id = b.id OR e.to_record_id = b.id)
                WHERE (e.valid_to IS NULL OR e.valid_to > now())
            )
            """
        graph_conditions = list(policy_conditions)
        graph_params = list(policy_params)
        if include_graph:
            graph_conditions.append("id IN (SELECT record_id FROM graph_hits)")
        graph_where = VALUE_AND_SQL.join(graph_conditions)
        rows = self._fetchall(
            f"""
            WITH text_base AS (
                SELECT *
                FROM memory_records
                WHERE {text_where}
            )
            , vector_hits AS (
                {vector_hits_sql}
            )
            {graph_sql}
            , expanded AS (
                SELECT b.*, ({score_sql}) AS search_score, 0.0 AS graph_score, ({vector_sql_base}) AS vector_score, TRUE AS lexical_match
                FROM text_base b
                UNION ALL
                SELECT r.*, ({score_sql}) AS search_score, 0.05 AS graph_score, ({vector_sql_record}) AS vector_score, FALSE AS lexical_match
                FROM memory_records r
                WHERE %s AND {graph_where}
                UNION ALL
                SELECT v.*, ({score_sql}) AS search_score, 0.0 AS graph_score, ({vector_sql_candidate}) AS vector_score, FALSE AS lexical_match
                FROM vector_hits v
            )
            , ranked AS (
                SELECT x.*,
                       BOOL_OR(x.lexical_match) OVER (PARTITION BY x.id) AS lexical_match_any,
                       MAX(x.vector_score) OVER (PARTITION BY x.id) AS vector_similarity_max,
                       (MAX(x.vector_score) OVER (PARTITION BY x.id) IS NOT NULL) AS vector_similarity_available,
                       MAX(x.graph_score) OVER (PARTITION BY x.id) AS graph_score_max,
                       (
                           x.search_score + x.graph_score
                           + CASE WHEN %s THEN COALESCE(x.vector_score, 0.0) ELSE 0.0 END
                       ) AS hybrid_score,
                       ROW_NUMBER() OVER (
                           PARTITION BY x.id
                           ORDER BY
                               (
                                   x.search_score + x.graph_score
                                   + CASE WHEN %s THEN COALESCE(x.vector_score, 0.0) ELSE 0.0 END
                               ) DESC,
                               x.updated_at DESC,
                               x.id DESC
                       ) AS record_rank
                FROM expanded x
            )
            SELECT *
            FROM ranked
            WHERE record_rank = 1
            ORDER BY
                CASE WHEN lifecycle_status = 'active' THEN 0 ELSE 1 END,
                CASE WHEN supersedes_id IS NULL THEN 0 ELSE 1 END,
                hybrid_score DESC,
                updated_at DESC,
                id DESC
            LIMIT %s OFFSET %s
            """,
            [
                *text_params,
                *vector_hits_params,
                *score_params,
                *vector_params,
                *score_params,
                *vector_params,
                bool(include_graph),
                *graph_params,
                *score_params,
                *vector_params,
                bool(include_vector),
                bool(include_vector),
                filters.limit,
                filters.offset,
            ],
        )
        normalized_query = " ".join(str(query or "").split()).strip()
        matched_terms = [part for part in normalized_query.split() if part]
        results: list[MemorySearchResult] = []
        for row in rows:
            record = _row_to_record(row)
            raw_vector_similarity = row.get("vector_similarity_max")
            vector_similarity_available = bool(include_vector) and bool(
                row.get("vector_similarity_available") or raw_vector_similarity is not None
            )
            results.append(
                MemorySearchResult(
                    record=record,
                    score=float(row.get("hybrid_score") or 0.0),
                    citation=Citation(
                        record_id=record.id,
                        title=record.title,
                        source_type=record.source_type,
                        source_id=record.source_id,
                        source_trust=record.source_trust,
                        snippet=record.summary or record.content[:240],
                        confidence=record.confidence,
                        provenance={
                            "memory_type": record.memory_type,
                            "visibility": record.visibility,
                            "lifecycle_status": record.lifecycle_status,
                            "retrieval_mode": "postgres_unified",
                            "lexical_match": bool(row.get("lexical_match_any")),
                            "vector_search_applied": bool(include_vector),
                            "vector_similarity_available": vector_similarity_available,
                            "vector_similarity": (
                                float(raw_vector_similarity)
                                if vector_similarity_available and raw_vector_similarity is not None
                                else None
                            ),
                            "graph_score": float(row.get("graph_score_max") or 0.0),
                            "evidence_refs": [asdict(ref) for ref in record.evidence_refs],
                        },
                    ),
                    matched_terms=matched_terms,
                )
            )
        return results
