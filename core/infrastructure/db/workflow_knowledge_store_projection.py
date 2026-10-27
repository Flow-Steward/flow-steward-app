# mypy: ignore-errors
"""Projection write and reachability methods for the workflow knowledge store."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from core.application.workflows.knowledge_projection import WorkflowKnowledgeProjection
from core.infrastructure.db import psycopg_runtime

from .workflow_knowledge_store_common import _json_dumps, _scope_value, _tenant_value, logger


class WorkflowKnowledgeProjectionMixin:
    def _clear_scope(self, *, account_id: str, tenant_id: str, project_scope: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM public.platform_workflow_knowledge_edges
                 WHERE account_id = %s
                   AND tenant_id = %s
                   AND project_scope = %s
                """,
                (account_id, tenant_id, project_scope),
            )
            cur.execute(
                """
                DELETE FROM public.platform_workflow_knowledge_documents
                 WHERE account_id = %s
                   AND tenant_id = %s
                   AND project_scope = %s
                """,
                (account_id, tenant_id, project_scope),
            )
            cur.execute(
                """
                DELETE FROM public.platform_workflow_knowledge_nodes
                 WHERE account_id = %s
                   AND tenant_id = %s
                   AND project_scope = %s
                """,
                (account_id, tenant_id, project_scope),
            )
        try:
            self._run_cypher(
                """
                MATCH (n:Entity)
                WHERE n.account_id = $account_id
                  AND n.tenant_id = $tenant_id
                  AND n.project_scope = $project_scope
                DETACH DELETE n
                RETURN 1
                """,
                columns=["result"],
                params={
                    "account_id": account_id,
                    "tenant_id": tenant_id,
                    "project_scope": project_scope,
                },
            )
        except Exception as exc:
            logger.debug("Workflow AGE scope clear skipped: %s", exc)

    def _sql_reachable_ids(
        self,
        *,
        entity_ids: Sequence[str],
        account_id: str,
        tenant_id: str,
        project_scope: str,
        edge_kinds: Sequence[str],
        depth: int,
        limit: int,
    ) -> list[tuple[str, int]]:
        normalized_ids = [str(item or "").strip() for item in entity_ids if str(item or "").strip()]
        if not normalized_ids:
            return []
        max_depth = max(0, int(depth or 0))
        max_limit = max(1, int(limit or 1))
        edge_kind_filter = {
            str(item or "").strip() for item in edge_kinds if str(item or "").strip()
        }
        with (
            self._connect() as conn,
            conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur,
        ):
            cur.execute(
                """
                SELECT source_id, target_id, edge_kind
                  FROM public.platform_workflow_knowledge_edges
                 WHERE account_id = %s
                   AND tenant_id = %s
                   AND project_scope = %s
                """,
                (account_id, tenant_id, project_scope),
            )
            rows = [dict(row) for row in cur.fetchall()]
        adjacency: dict[str, list[tuple[str, str]]] = {}
        for row in rows:
            edge_kind = str(row.get("edge_kind") or "").strip()
            if edge_kind_filter and edge_kind not in edge_kind_filter:
                continue
            source_id = str(row.get("source_id") or "").strip()
            target_id = str(row.get("target_id") or "").strip()
            if not source_id or not target_id:
                continue
            adjacency.setdefault(source_id, []).append((target_id, edge_kind))
            adjacency.setdefault(target_id, []).append((source_id, edge_kind))
        seen_depths: dict[str, int] = {}
        queue: deque[tuple[str, int]] = deque()
        for entity_id in normalized_ids:
            queue.append((entity_id, 0))
            seen_depths.setdefault(entity_id, 0)
        while queue and len(seen_depths) < max_limit:
            current_id, current_depth = queue.popleft()
            if current_depth >= max_depth:
                continue
            for neighbor_id, _edge_kind in adjacency.get(current_id, []):
                if neighbor_id in seen_depths and seen_depths[neighbor_id] <= current_depth + 1:
                    continue
                seen_depths[neighbor_id] = current_depth + 1
                queue.append((neighbor_id, current_depth + 1))
                if len(seen_depths) >= max_limit:
                    break
        return list(seen_depths.items())[:max_limit]

    def _age_reachable_ids(
        self,
        *,
        entity_ids: Sequence[str],
        account_id: str,
        tenant_id: str,
        project_scope: str,
        edge_kinds: Sequence[str],
        depth: int,
        limit: int,
    ) -> list[tuple[str, int]]:
        normalized_ids = [str(item or "").strip() for item in entity_ids if str(item or "").strip()]
        if not normalized_ids:
            return []
        max_depth = max(0, min(int(depth or 0), 4))
        max_limit = max(1, int(limit or 1))
        if max_depth <= 0:
            return [(entity_id, 0) for entity_id in normalized_ids[:max_limit]]
        path_depth = max(1, max_depth)
        filter_clause = ""
        params: dict[str, Any] = {
            "account_id": account_id,
            "tenant_id": tenant_id,
            "project_scope": project_scope,
            "entity_ids": normalized_ids,
            "limit": max_limit,
        }
        normalized_edge_kinds = [
            str(item or "").strip() for item in edge_kinds if str(item or "").strip()
        ]
        if normalized_edge_kinds:
            params["edge_kinds"] = normalized_edge_kinds
            filter_clause = "  AND ALL(rel IN relationships(p) WHERE rel.kind IN $edge_kinds)\n"
        query = f"""
        MATCH p = (s:Entity)-[:RELATES_TO*0..{path_depth}]-(n:Entity)
        WHERE s.account_id = $account_id
          AND s.tenant_id = $tenant_id
          AND s.project_scope = $project_scope
          AND s.id IN $entity_ids
          AND n.account_id = $account_id
          AND n.tenant_id = $tenant_id
          AND n.project_scope = $project_scope
{filter_clause}        RETURN DISTINCT n.id AS entity_id, length(p) AS depth
        ORDER BY depth ASC, entity_id ASC
        LIMIT $limit
        """
        try:
            rows = self._run_cypher(query, columns=["entity_id", "depth"], params=params)
            resolved: list[tuple[str, int]] = []
            for row in rows:
                entity_id = str(row.get("entity_id") or "").strip()
                if not entity_id:
                    continue
                resolved.append((entity_id, int(row.get("depth") or 0)))
            return resolved[:max_limit]
        except Exception as exc:
            logger.debug("Workflow AGE reachability fallback to SQL BFS: %s", exc)
            return self._sql_reachable_ids(
                entity_ids=normalized_ids,
                account_id=account_id,
                tenant_id=tenant_id,
                project_scope=project_scope,
                edge_kinds=edge_kinds,
                depth=max_depth,
                limit=max_limit,
            )

    def upsert_projection(
        self,
        *,
        projection: WorkflowKnowledgeProjection,
        account_id: str = "",
        tenant_id: str | None = None,
        project_id: str | None = None,
    ) -> None:
        prepared = self.prepare_projection_write(
            projection=projection,
            account_id=account_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        self.apply_prepared_projection(prepared=prepared)

    def prepare_projection_write(
        self,
        *,
        projection: WorkflowKnowledgeProjection,
        account_id: str = "",
        tenant_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Prepare rows and embeddings without holding the replacement transaction."""

        normalized_account_id = str(account_id or "").strip()
        if not normalized_account_id:
            raise ValueError("account_id is required for workflow knowledge projection")
        normalized_tenant_id = _tenant_value(tenant_id)
        project_scope = _scope_value(project_id)
        self.enforce_runtime(ensure_indexes=False)
        indexed_at = datetime.now(UTC).isoformat()
        vector_available = self._document_vector_column_available()
        documents = list(projection.documents or [])
        embedding_map: dict[str, list[float]] = {}
        if vector_available and documents:
            texts = [str(document.text or "").strip() for document in documents]
            embeddings = self._embed_texts(texts, account_id=normalized_account_id) or []
            if len(embeddings) == len(documents):
                embedding_map = {
                    document.id: list(embedding)
                    for document, embedding in zip(documents, embeddings, strict=False)
                }

        node_rows = [
            (
                normalized_account_id,
                normalized_tenant_id,
                project_scope,
                str(node.id),
                str(node.kind),
                str(node.label or ""),
                str(node.summary or ""),
                str(node.text or ""),
                _json_dumps(dict(node.metadata or {})),
                indexed_at,
            )
            for node in (projection.nodes or [])
            if str(node.id or "").strip()
        ]
        edge_rows = [
            (
                normalized_account_id,
                normalized_tenant_id,
                project_scope,
                str(edge.id),
                str(edge.kind),
                str(edge.source_id),
                str(edge.target_id),
                _json_dumps(dict(edge.metadata or {})),
                indexed_at,
            )
            for edge in (projection.edges or [])
            if str(edge.id or "").strip()
            and str(edge.source_id or "").strip()
            and str(edge.target_id or "").strip()
        ]
        doc_rows = []
        for document in documents:
            if not str(document.id or "").strip():
                continue
            embedding = embedding_map.get(str(document.id))
            doc_rows.append(
                (
                    normalized_account_id,
                    normalized_tenant_id,
                    project_scope,
                    str(document.id),
                    str(document.entity_id),
                    str(document.entity_kind),
                    str(document.title or ""),
                    str(document.text or ""),
                    _json_dumps(dict(document.metadata or {})),
                    _json_dumps(embedding) if embedding is not None else None,
                    [float(value) for value in embedding] if embedding is not None else None,
                    indexed_at,
                )
            )

        graph_node_rows = []
        for node in projection.nodes or []:
            entity_id = str(node.id or "").strip()
            if not entity_id:
                continue
            metadata = dict(node.metadata or {})
            graph_node_rows.append(
                {
                    "id": entity_id,
                    "account_id": normalized_account_id,
                    "tenant_id": normalized_tenant_id,
                    "project_scope": project_scope,
                    "kind": str(node.kind or ""),
                    "label": str(node.label or ""),
                    "workflow_id": str(metadata.get("workflow_id") or ""),
                }
            )
        graph_edge_rows = []
        for edge in projection.edges or []:
            if not str(edge.id or "").strip():
                continue
            graph_edge_rows.append(
                {
                    "edge_id": str(edge.id),
                    "account_id": normalized_account_id,
                    "tenant_id": normalized_tenant_id,
                    "project_scope": project_scope,
                    "kind": str(edge.kind or ""),
                    "source_id": str(edge.source_id or ""),
                    "target_id": str(edge.target_id or ""),
                }
            )
        return {
            "account_id": normalized_account_id,
            "tenant_id": normalized_tenant_id,
            "project_scope": project_scope,
            "vector_available": vector_available,
            "node_rows": node_rows,
            "edge_rows": edge_rows,
            "doc_rows": doc_rows,
            "graph_node_rows": graph_node_rows,
            "graph_edge_rows": graph_edge_rows,
        }

    def apply_prepared_projection(self, *, prepared: Mapping[str, Any]) -> None:
        """Atomically replace a scope using already prepared rows and embeddings."""

        normalized_account_id = str(prepared.get("account_id") or "").strip()
        normalized_tenant_id = str(prepared.get("tenant_id") or "").strip()
        project_scope = str(prepared.get("project_scope") or "").strip()
        if not normalized_account_id:
            raise ValueError("account_id is required for workflow knowledge projection")
        vector_available = bool(prepared.get("vector_available"))
        node_rows = list(prepared.get("node_rows") or [])
        edge_rows = list(prepared.get("edge_rows") or [])
        doc_rows = list(prepared.get("doc_rows") or [])
        graph_node_rows = list(prepared.get("graph_node_rows") or [])
        graph_edge_rows = list(prepared.get("graph_edge_rows") or [])
        self._clear_scope(
            account_id=normalized_account_id,
            tenant_id=normalized_tenant_id,
            project_scope=project_scope,
        )

        with self._connect() as conn, conn.cursor() as cur:
            if node_rows:
                cur.executemany(
                    """
                    INSERT INTO public.platform_workflow_knowledge_nodes (
                        account_id,
                        tenant_id,
                        project_scope,
                        entity_id,
                        entity_kind,
                        label,
                        summary,
                        content_text,
                        metadata_jsonb,
                        indexed_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    node_rows,
                )
            if doc_rows and vector_available:
                cur.executemany(
                    """
                    INSERT INTO public.platform_workflow_knowledge_documents (
                        account_id,
                        tenant_id,
                        project_scope,
                        doc_id,
                        entity_id,
                        entity_kind,
                        title,
                        content_text,
                        metadata_jsonb,
                        embedding_jsonb,
                        embedding,
                        indexed_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)
                    """,
                    doc_rows,
                )
            elif doc_rows:
                cur.executemany(
                    """
                    INSERT INTO public.platform_workflow_knowledge_documents (
                        account_id,
                        tenant_id,
                        project_scope,
                        doc_id,
                        entity_id,
                        entity_kind,
                        title,
                        content_text,
                        metadata_jsonb,
                        embedding_jsonb,
                        indexed_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [row[:10] + row[11:] for row in doc_rows],
                )
            if edge_rows:
                cur.executemany(
                    """
                    INSERT INTO public.platform_workflow_knowledge_edges (
                        account_id,
                        tenant_id,
                        project_scope,
                        edge_id,
                        edge_kind,
                        source_id,
                        target_id,
                        metadata_jsonb,
                        indexed_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    edge_rows,
                )
        if graph_node_rows:
            self._run_write_batch(
                """
                UNWIND $rows AS row
                MERGE (n:Entity {id: row.id, account_id: row.account_id, tenant_id: row.tenant_id, project_scope: row.project_scope})
                SET n.kind = row.kind,
                    n.label = row.label,
                    n.workflow_id = row.workflow_id
                RETURN count(*)
                """,
                rows=graph_node_rows,
            )
        if graph_edge_rows:
            self._run_write_batch(
                """
                UNWIND $rows AS row
                MATCH (a:Entity {id: row.source_id, account_id: row.account_id, tenant_id: row.tenant_id, project_scope: row.project_scope})
                MATCH (b:Entity {id: row.target_id, account_id: row.account_id, tenant_id: row.tenant_id, project_scope: row.project_scope})
                MERGE (a)-[r:RELATES_TO {edge_id: row.edge_id, account_id: row.account_id, tenant_id: row.tenant_id, project_scope: row.project_scope}]->(b)
                SET r.kind = row.kind,
                    r.source_id = row.source_id,
                    r.target_id = row.target_id
                RETURN count(*)
                """,
                rows=graph_edge_rows,
            )

    def delete_scope(
        self,
        *,
        account_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
        workflow_ids: Sequence[str] = (),
    ) -> None:
        del workflow_ids
        self.enforce_runtime(ensure_indexes=False)
        self._clear_scope(
            account_id=str(account_id or "").strip(),
            tenant_id=_tenant_value(tenant_id),
            project_scope=_scope_value(project_id),
        )
