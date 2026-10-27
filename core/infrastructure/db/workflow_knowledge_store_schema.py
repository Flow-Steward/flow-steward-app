# mypy: ignore-errors
"""Schema and runtime-readiness methods for the workflow knowledge store."""

from __future__ import annotations

import uuid
from typing import Any

from psycopg import sql

from .workflow_knowledge_store_common import (
    _EDGE_INDEX_KEYS,
    _EDGE_LABELS,
    _SCHEMA_MIGRATION_ADVISORY_LOCK_KEY,
    _TENANT_PKEY_ID_COLUMNS,
    _VERTEX_INDEX_KEYS,
    _VERTEX_LABELS,
    logger,
)

_CANONICAL_RELATIONS = (
    "public.platform_workflow_knowledge_nodes",
    "public.platform_workflow_knowledge_edges",
    "public.platform_workflow_knowledge_documents",
)
_CANONICAL_INDEXES = (
    "public.idx_platform_workflow_knowledge_nodes_scope",
    "public.idx_platform_workflow_knowledge_edges_scope",
    "public.idx_platform_workflow_knowledge_documents_scope",
    "public.idx_platform_workflow_knowledge_documents_search",
    "public.idx_platform_workflow_knowledge_docs_embedding_cosine",
)


class WorkflowKnowledgeSchemaMixin:
    def reset_graph(self) -> None:
        """Drop derived AGE state and recreate it from an empty supported graph."""
        with self._age_schema_cursor() as cur:
            if self._graph_exists(cur):
                cur.execute(
                    sql.SQL("SELECT ag_catalog.drop_graph({graph}, true)").format(
                        graph=sql.Literal(self.graph_name)
                    )
                )
        self._schema_ensured = False
        self.create_graph_indexes()

    def semantic_read_write_probe(self) -> dict[str, Any]:
        """Prove AGE can create, read and delete data, not only list catalogs."""
        probe_id = f"__flow_steward_probe__:{uuid.uuid4()}"
        created = self._run_cypher(
            """
            CREATE (n:Entity {id: $probe_id, kind: 'runtime_probe'})
            RETURN n.id AS probe_id
            """,
            columns=["probe_id"],
            params={"probe_id": probe_id},
        )
        try:
            read = self._run_cypher(
                """
                MATCH (n:Entity {id: $probe_id})
                RETURN n.id AS probe_id
                """,
                columns=["probe_id"],
                params={"probe_id": probe_id},
            )
            if not created or not read or str(read[0].get("probe_id") or "") != probe_id:
                raise RuntimeError("workflow knowledge AGE semantic probe could not read its write")
            return {"ok": True, "graph_name": self.graph_name, "probe_id": probe_id}
        finally:
            self._run_cypher(
                """
                MATCH (n:Entity {id: $probe_id})
                DETACH DELETE n
                RETURN $probe_id AS probe_id
                """,
                columns=["probe_id"],
                params={"probe_id": probe_id},
            )

    def create_graph_indexes(self) -> None:
        with self._age_schema_cursor() as cur:
            self._ensure_graph(cur)
            self._ensure_labels(cur)
            for label in _VERTEX_LABELS:
                for key in _VERTEX_INDEX_KEYS:
                    cur.execute(
                        self._property_index_statement(
                            graph_name=self.graph_name,
                            label=label,
                            key=key,
                        )
                    )
            for label in _EDGE_LABELS:
                for key in _EDGE_INDEX_KEYS:
                    cur.execute(
                        self._property_index_statement(
                            graph_name=self.graph_name,
                            label=label,
                            key=key,
                        )
                    )

    def create_vector_index(self) -> None:
        self._ensure_schema()
        with self._connect() as conn, conn.cursor() as cur:
            if not self._pg_extension_available(cur, "vector"):
                self._document_vector_column_available_cache = False
                logger.info(
                    "Skipping workflow knowledge pgvector bootstrap because the vector "
                    "extension is not available in this Postgres image"
                )
                return
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(
                """
                ALTER TABLE public.platform_workflow_knowledge_documents
                ADD COLUMN IF NOT EXISTS embedding vector(1536)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_platform_workflow_knowledge_docs_embedding_cosine
                    ON public.platform_workflow_knowledge_documents
                    USING hnsw (embedding vector_cosine_ops)
                """
            )
            self._document_vector_column_available_cache = True

    @staticmethod
    def _canonical_schema_status(cur: Any) -> dict[str, Any]:
        missing_relations: list[str] = []
        missing_indexes: list[str] = []
        for relation_name in _CANONICAL_RELATIONS:
            cur.execute("SELECT to_regclass(%s)", (relation_name,))
            row = cur.fetchone()
            if not row or row[0] is None:
                missing_relations.append(relation_name)
        for index_name in _CANONICAL_INDEXES:
            cur.execute("SELECT to_regclass(%s)", (index_name,))
            row = cur.fetchone()
            if not row or row[0] is None:
                missing_indexes.append(index_name)
        available = not missing_relations and not missing_indexes
        return {
            "available": available,
            "missing_relations": missing_relations,
            "missing_indexes": missing_indexes,
            "error": "" if available else "Canonical workflow knowledge schema is incomplete.",
        }

    def runtime_status(self, *, ensure_indexes: bool = False) -> dict[str, Any]:
        # Kept for API compatibility. Status checks never create or repair indexes;
        # canonical schema and migrations are the sole owners of database DDL.
        del ensure_indexes
        status: dict[str, Any] = {
            "ok": False,
            "graph_name": self.graph_name,
            "schema": {
                "available": False,
                "missing_relations": [],
                "missing_indexes": [],
                "error": "",
            },
            "graph": {
                "available": False,
                "extension_available": False,
                "extension_installed": False,
                "labels_ready": False,
                "error": "",
            },
            "vector": {
                "available": False,
                "extension_available": False,
                "extension_installed": False,
                "column_available": False,
                "error": "",
            },
        }
        with self._connect() as conn, conn.cursor() as cur:
            status["schema"] = self._canonical_schema_status(cur)
            status["vector"]["extension_available"] = self._pg_extension_available(cur, "vector")
            status["graph"]["extension_available"] = self._pg_extension_available(cur, "age")
            installed = self._installed_extensions(cur, "vector", "age")
            status["vector"]["extension_installed"] = "vector" in installed
            status["graph"]["extension_installed"] = "age" in installed
            if status["schema"]["available"]:
                status["vector"]["column_available"] = self._column_exists(
                    cur,
                    table_name="platform_workflow_knowledge_documents",
                    column_name="embedding",
                )
            status["vector"]["available"] = bool(
                status["schema"]["available"]
                and status["vector"]["extension_installed"]
                and status["vector"]["column_available"]
            )
        if not status["schema"]["available"]:
            schema_error = "canonical workflow knowledge schema is incomplete"
            status["graph"]["error"] = schema_error
            status["vector"]["error"] = schema_error
            return status
        if status["graph"]["extension_installed"]:
            try:
                with self._age_cursor() as cur:
                    graph_exists = self._graph_exists(cur)
                    labels_ready = graph_exists and all(
                        self._label_exists(cur, label) for label in (*_VERTEX_LABELS, *_EDGE_LABELS)
                    )
                status["graph"]["labels_ready"] = labels_ready
                status["graph"]["available"] = labels_ready
                if not graph_exists:
                    status["graph"]["error"] = (
                        f"Workflow knowledge graph {self.graph_name!r} is missing."
                    )
                elif not labels_ready:
                    status["graph"]["error"] = "Workflow knowledge graph labels are not ready."
            except Exception as exc:
                status["graph"]["error"] = str(exc)
        if not status["vector"]["available"] and not status["vector"]["error"]:
            if not status["vector"]["extension_available"]:
                status["vector"]["error"] = (
                    "pgvector extension is not available in this Postgres image."
                )
            elif not status["vector"]["extension_installed"]:
                status["vector"]["error"] = "pgvector extension is not installed in this database."
            elif not status["vector"]["column_available"]:
                status["vector"]["error"] = (
                    "Workflow knowledge embedding column is missing from "
                    "platform_workflow_knowledge_documents."
                )
        if not status["graph"]["available"] and not status["graph"]["error"]:
            if not status["graph"]["extension_available"]:
                status["graph"]["error"] = (
                    "Apache AGE extension is not available in this Postgres image."
                )
            elif not status["graph"]["extension_installed"]:
                status["graph"]["error"] = "Apache AGE extension is not installed in this database."
            else:
                status["graph"]["error"] = "Workflow knowledge graph labels are not ready."
        status["ok"] = bool(status["graph"]["available"] and status["vector"]["available"])
        return status

    def enforce_runtime(
        self,
        *,
        require_graph: bool = True,
        require_vector: bool = True,
        ensure_indexes: bool = False,
    ) -> dict[str, Any]:
        status = self.runtime_status(ensure_indexes=ensure_indexes)
        errors: list[str] = []
        if require_graph and not bool(status["graph"]["available"]):
            errors.append(
                "workflow graph backend unavailable"
                + (
                    f": {status['graph']['error']}"
                    if str(status["graph"]["error"] or "").strip()
                    else ""
                )
            )
        if require_vector and not bool(status["vector"]["available"]):
            errors.append(
                "workflow vector backend unavailable"
                + (
                    f": {status['vector']['error']}"
                    if str(status["vector"]["error"] or "").strip()
                    else ""
                )
            )
        if errors:
            raise RuntimeError("; ".join(errors))
        return status

    def _migrate_tenant_pkey(self, cur: Any) -> None:
        """Retrofit tenant_id into legacy tables' primary key — idempotent and cheap.

        Tables created fresh already declare the tenant-aware primary key, so this
        skips them entirely. The destructive DROP/ADD CONSTRAINT only runs once, for
        pre-tenant tables; afterwards the steady-state runtime/status path issues no
        primary-key DDL at all, which is what removes the concurrent-status race
        ("multiple primary keys ... are not allowed").
        """
        for table_name, id_column in _TENANT_PKEY_ID_COLUMNS.items():
            cur.execute(
                """
                SELECT 1
                  FROM pg_index i
                  JOIN pg_attribute a
                    ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                 WHERE i.indrelid = %s::regclass
                   AND i.indisprimary
                   AND a.attname = 'tenant_id'
                 LIMIT 1
                """,
                (f"public.{table_name}",),
            )
            if cur.fetchone() is not None:
                continue
            cur.execute(
                sql.SQL(
                    "ALTER TABLE public.{table} "
                    "ADD COLUMN IF NOT EXISTS tenant_id text DEFAULT '' NOT NULL"
                ).format(table=sql.Identifier(table_name))
            )
            cur.execute(
                sql.SQL("ALTER TABLE public.{table} DROP CONSTRAINT IF EXISTS {pkey}").format(
                    table=sql.Identifier(table_name),
                    pkey=sql.Identifier(f"{table_name}_pkey"),
                )
            )
            cur.execute(
                sql.SQL(
                    "ALTER TABLE public.{table} ADD CONSTRAINT {pkey} "
                    "PRIMARY KEY (account_id, tenant_id, project_scope, {idcol})"
                ).format(
                    table=sql.Identifier(table_name),
                    pkey=sql.Identifier(f"{table_name}_pkey"),
                    idcol=sql.Identifier(id_column),
                )
            )

    def _ensure_schema(self) -> None:
        if self._schema_ensured:
            return
        with self._connect() as conn, conn.cursor() as cur:
            # Serialize schema creation/migration across processes. autocommit is on,
            # so a session-level advisory lock (held until explicit unlock / conn
            # close) is what keeps two concurrent callers from both running the
            # one-time primary-key retrofit and colliding.
            cur.execute("SELECT pg_advisory_lock(%s)", (_SCHEMA_MIGRATION_ADVISORY_LOCK_KEY,))
            try:
                self._ensure_schema_locked(cur)
            finally:
                cur.execute("SELECT pg_advisory_unlock(%s)", (_SCHEMA_MIGRATION_ADVISORY_LOCK_KEY,))
        self._schema_ensured = True

    def _ensure_schema_locked(self, cur: Any) -> None:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.platform_workflow_knowledge_nodes (
                account_id text NOT NULL,
                tenant_id text DEFAULT '' NOT NULL,
                project_scope text DEFAULT '' NOT NULL,
                entity_id text NOT NULL,
                entity_kind text NOT NULL,
                label text DEFAULT '' NOT NULL,
                summary text DEFAULT '' NOT NULL,
                content_text text DEFAULT '' NOT NULL,
                metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
                indexed_at timestamp with time zone DEFAULT now() NOT NULL,
                CONSTRAINT platform_workflow_knowledge_nodes_pkey
                    PRIMARY KEY (account_id, tenant_id, project_scope, entity_id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.platform_workflow_knowledge_edges (
                account_id text NOT NULL,
                tenant_id text DEFAULT '' NOT NULL,
                project_scope text DEFAULT '' NOT NULL,
                edge_id text NOT NULL,
                edge_kind text NOT NULL,
                source_id text NOT NULL,
                target_id text NOT NULL,
                metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
                indexed_at timestamp with time zone DEFAULT now() NOT NULL,
                CONSTRAINT platform_workflow_knowledge_edges_pkey
                    PRIMARY KEY (account_id, tenant_id, project_scope, edge_id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.platform_workflow_knowledge_documents (
                account_id text NOT NULL,
                tenant_id text DEFAULT '' NOT NULL,
                project_scope text DEFAULT '' NOT NULL,
                doc_id text NOT NULL,
                entity_id text NOT NULL,
                entity_kind text NOT NULL,
                title text DEFAULT '' NOT NULL,
                content_text text DEFAULT '' NOT NULL,
                metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
                embedding_jsonb jsonb,
                indexed_at timestamp with time zone DEFAULT now() NOT NULL,
                CONSTRAINT platform_workflow_knowledge_documents_pkey
                    PRIMARY KEY (account_id, tenant_id, project_scope, doc_id)
            )
            """
        )
        self._migrate_tenant_pkey(cur)
        cur.execute("DROP INDEX IF EXISTS public.idx_platform_workflow_knowledge_nodes_scope")
        cur.execute("DROP INDEX IF EXISTS public.idx_platform_workflow_knowledge_edges_scope")
        cur.execute("DROP INDEX IF EXISTS public.idx_platform_workflow_knowledge_documents_scope")
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_platform_workflow_knowledge_nodes_scope
                ON public.platform_workflow_knowledge_nodes
                (account_id, tenant_id, project_scope, entity_kind, entity_id)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_platform_workflow_knowledge_edges_scope
                ON public.platform_workflow_knowledge_edges
                (account_id, tenant_id, project_scope, source_id, target_id, edge_kind)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_platform_workflow_knowledge_documents_scope
                ON public.platform_workflow_knowledge_documents
                (account_id, tenant_id, project_scope, entity_kind, entity_id)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_platform_workflow_knowledge_documents_search
                ON public.platform_workflow_knowledge_documents
                USING gin (
                    to_tsvector(
                        'simple',
                        coalesce(title, '') || ' ' || coalesce(content_text, '')
                    )
                )
            """
        )
