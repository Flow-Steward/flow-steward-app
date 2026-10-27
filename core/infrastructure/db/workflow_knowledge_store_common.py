# mypy: ignore-errors
"""Shared primitives for the Postgres/AGE workflow knowledge store."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from psycopg import sql

from core.application.settings.embedding_credentials import (
    resolve_embedding_openai_api_key,
)
from core.application.workflows.knowledge_projection import WorkflowKnowledgeBackend
from core.infrastructure.config.runtime_env import runtime_env as _runtime_env
from core.infrastructure.db import psycopg_runtime
from core.infrastructure.db.db_pool import transaction_connection

logger = logging.getLogger(__name__)

DEFAULT_AGE_GRAPH_NAME = "platform_workflow_graph"
_CYPHER_PARAM_RE = re.compile(r"\$([A-Za-z_](?a:\w*))")
_CYPHER_KEY_RE = re.compile(r"[A-Za-z_](?a:\w*)")
_PATH_SUFFIX_RE = re.compile(r"::(vertex|edge|path)")
_VERTEX_LABELS = ("Entity",)
_EDGE_LABELS = ("RELATES_TO",)
_VERTEX_INDEX_KEYS = (
    "account_id",
    "tenant_id",
    "project_scope",
    "id",
    "kind",
    "label",
    "workflow_id",
)
_EDGE_INDEX_KEYS = (
    "account_id",
    "tenant_id",
    "project_scope",
    "edge_id",
    "source_id",
    "target_id",
    "kind",
)
_WRITE_BATCH_SIZE = 250
_ENTITY_KIND_SCORE_MULTIPLIERS = {
    "workflow": 1.0,
    "agent": 1.0,
    "skill": 1.0,
    "tool": 1.0,
    "tool_action": 0.98,
    "phase": 0.93,
    "reaction": 0.92,
    "step": 0.88,
    "io_field": 0.76,
    "step_kind": 0.7,
    "phase_category": 0.7,
    "agent_type": 0.68,
    "skill_category": 0.68,
    "tool_category": 0.68,
}
_SCOPE_SYNC_DIGEST_DOC_ID = "__catalog_sync_digest__"
_SCOPE_SYNC_DIGEST_ENTITY_KIND = "sync_state"
_SCHEMA_MIGRATION_ADVISORY_LOCK_KEY = 0x5746574B  # "WFWK"
_TENANT_PKEY_ID_COLUMNS: dict[str, str] = {
    "platform_workflow_knowledge_nodes": "entity_id",
    "platform_workflow_knowledge_edges": "edge_id",
    "platform_workflow_knowledge_documents": "doc_id",
}


def _resolve_dsn(explicit_dsn: str | None = None) -> str:
    fallback_db = str(_runtime_env.get("POSTGRES_DB") or "postgres").strip()
    fallback_user = str(_runtime_env.get("POSTGRES_USER") or "postgres").strip()
    fallback_password = str(_runtime_env.get("POSTGRES_PASSWORD") or "postgres").strip()
    fallback_host = str(_runtime_env.get("POSTGRES_HOST") or "localhost").strip()
    fallback_port = str(_runtime_env.get("POSTGRES_PORT") or "5432").strip()
    value = str(
        explicit_dsn
        or _runtime_env.get("FS_DB_DSN")
        or _runtime_env.get("DATABASE_URL")
        or (
            f"dbname={fallback_db} user={fallback_user} password={fallback_password} "
            f"host={fallback_host} port={fallback_port}"
        )
    ).strip()
    if not value:
        raise RuntimeError("Workflow knowledge store requires FS_DB_DSN or DATABASE_URL")
    return value


def _scope_value(project_id: str | None) -> str:
    return str(project_id or "").strip()


def _tenant_value(tenant_id: str | None) -> str:
    return str(tenant_id or "").strip()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _cypher_sql_literal(query: str) -> sql.SQL:
    tag = "age_cypher"
    while f"${tag}$" in query:
        tag = f"{tag}_x"
    return sql.SQL(f"${tag}$\n{query}\n${tag}$")


def _cypher_literal(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return json.dumps(value, ensure_ascii=True, allow_nan=False)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, (list, tuple, set)):
        return "[" + ", ".join(_cypher_literal(item) for item in value) + "]"
    if isinstance(value, dict):
        parts: list[str] = []
        for key, item in value.items():
            key_text = str(key or "").strip()
            if not _CYPHER_KEY_RE.fullmatch(key_text):
                raise ValueError(f"Unsupported Cypher map key: {key_text!r}")
            parts.append(f"{key_text}: {_cypher_literal(item)}")
        return "{" + ", ".join(parts) + "}"
    raise TypeError(f"Unsupported Cypher literal type: {type(value).__name__}")


def _bind_cypher_params(query: str, params: dict[str, Any] | None) -> str:
    if not params:
        return query

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in params:
            raise KeyError(f"Missing Cypher parameter: {name}")
        return _cypher_literal(params[name])

    return _CYPHER_PARAM_RE.sub(_replace, query)


def _decode_agtype(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (int, float, bool, dict, list)):
        return value
    text = str(value)
    if not text:
        return text
    clean = _PATH_SUFFIX_RE.sub("", text)
    try:
        return json.loads(clean)
    except Exception:
        return text


class PostgresAgeWorkflowKnowledgeStoreBase(WorkflowKnowledgeBackend):
    def __init__(
        self,
        dsn: str | None = None,
        *,
        graph_name: str | None = None,
        connection: Any | None = None,
    ) -> None:
        self._dsn = _resolve_dsn(dsn)
        self._connection = connection
        self._scope_connection: ContextVar[Any | None] = ContextVar(
            f"workflow_knowledge_scope_connection_{id(self)}",
            default=None,
        )
        self.graph_name = str(
            graph_name
            or _runtime_env.get("FS_WORKFLOW_KNOWLEDGE_AGE_GRAPH")
            or DEFAULT_AGE_GRAPH_NAME
        ).strip()
        if not self.graph_name:
            self.graph_name = DEFAULT_AGE_GRAPH_NAME
        self._schema_ensured = False
        self._document_vector_column_available_cache: bool | None = None

    @staticmethod
    def _installed_extensions(cur: Any, *extension_names: str) -> set[str]:
        normalized = [
            str(name or "").strip() for name in extension_names if str(name or "").strip()
        ]
        if not normalized:
            return set()
        cur.execute(
            """
            SELECT extname
              FROM pg_extension
             WHERE extname = ANY(%s)
            """,
            (normalized,),
        )
        return {str(row[0] or "").strip() for row in (cur.fetchall() or []) if row and row[0]}

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        bound = self._scope_connection.get() or self._connection
        if bound is not None:
            yield bound
            return
        conn = psycopg_runtime.connect(self._dsn)
        conn.autocommit = True
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _age_cursor(self) -> Iterator[Any]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("LOAD 'age'")
            cur.execute('SET search_path = ag_catalog, "$user", public')
            yield cur

    @contextmanager
    def _age_schema_cursor(self) -> Iterator[Any]:
        """Open an AGE cursor for explicit schema/bootstrap operations only."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS age")
            cur.execute("LOAD 'age'")
            cur.execute('SET search_path = ag_catalog, "$user", public')
            yield cur

    @contextmanager
    def scope_sync_lock(
        self,
        *,
        account_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
    ) -> Iterator[None]:
        """Serialize one scoped projection build without opening a transaction."""

        scope_key = "|".join(
            (
                self.graph_name,
                str(account_id or "").strip(),
                _tenant_value(tenant_id),
                _scope_value(project_id),
            )
        )
        connection = psycopg_runtime.connect(self._dsn)
        connection.autocommit = True
        try:
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
                    (scope_key,),
                )
            try:
                yield
            finally:
                with connection.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                        (scope_key,),
                    )
        finally:
            connection.close()

    @contextmanager
    def scope_sync_transaction(self) -> Iterator[None]:
        """Bind scoped projection writes to one short atomic transaction."""

        base_connection = self._scope_connection.get() or self._connection
        opened_here = base_connection is None
        connection = base_connection or psycopg_runtime.connect(self._dsn)
        if opened_here:
            connection.autocommit = True
        try:
            with transaction_connection(connection) as transaction_conn:
                token = self._scope_connection.set(transaction_conn)
                try:
                    yield
                finally:
                    self._scope_connection.reset(token)
        finally:
            if opened_here:
                connection.close()

    @staticmethod
    def _pg_extension_available(cur: Any, extension_name: str) -> bool:
        cur.execute(
            """
            SELECT 1
              FROM pg_available_extensions
             WHERE name = %s
            """,
            (extension_name,),
        )
        return cur.fetchone() is not None

    @staticmethod
    def _column_exists(cur: Any, *, table_name: str, column_name: str) -> bool:
        cur.execute(
            """
            SELECT 1
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name = %s
               AND column_name = %s
            """,
            (table_name, column_name),
        )
        return cur.fetchone() is not None

    def _document_vector_column_available(self) -> bool:
        cached = self._document_vector_column_available_cache
        if cached is not None:
            return cached
        with self._connect() as conn, conn.cursor() as cur:
            cached = self._column_exists(
                cur,
                table_name="platform_workflow_knowledge_documents",
                column_name="embedding",
            )
        self._document_vector_column_available_cache = cached
        return cached

    @staticmethod
    def _embedding_model() -> str:
        return str(
            _runtime_env.get("FS_WORKFLOW_KNOWLEDGE_EMBED_MODEL", "text-embedding-3-small")
        ).strip()

    def _embed_texts(self, texts: Sequence[str], *, account_id: str) -> list[list[float]] | None:
        normalized = [str(text or "").strip() for text in texts]
        if not normalized:
            return []
        with self._connect() as conn:
            api_key = resolve_embedding_openai_api_key(account_id=account_id, conn=conn)
        if not api_key:
            return None
        try:
            import openai

            client = openai.OpenAI(api_key=api_key)
            embeddings: list[list[float]] = []
            for offset in range(0, len(normalized), 64):
                batch = normalized[offset : offset + 64]
                response = client.embeddings.create(
                    input=batch,
                    model=self._embedding_model(),
                )
                embeddings.extend(
                    [[float(value) for value in item.embedding] for item in response.data]
                )
            return embeddings
        except Exception as exc:
            logger.debug("Workflow knowledge embeddings skipped: %s", exc)
            return None

    def _embed_query(self, query: str, *, account_id: str) -> list[float] | None:
        result = self._embed_texts([query], account_id=account_id)
        if not result:
            return None
        return list(result[0])

    def _graph_exists(self, cur: Any) -> bool:
        cur.execute(
            "SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s",
            (self.graph_name,),
        )
        return cur.fetchone() is not None

    def _label_exists(self, cur: Any, label: str) -> bool:
        cur.execute(
            """
            SELECT 1
              FROM information_schema.tables
             WHERE table_schema = %s
               AND table_name = %s
            """,
            (self.graph_name, label),
        )
        return cur.fetchone() is not None

    def _ensure_graph(self, cur: Any) -> None:
        if not self._graph_exists(cur):
            cur.execute(
                sql.SQL("SELECT ag_catalog.create_graph({graph})").format(
                    graph=sql.Literal(self.graph_name)
                )
            )

    def _ensure_labels(self, cur: Any) -> None:
        for label in _VERTEX_LABELS:
            if self._label_exists(cur, label):
                continue
            cur.execute(
                sql.SQL("SELECT ag_catalog.create_vlabel({graph}, {label})").format(
                    graph=sql.Literal(self.graph_name),
                    label=sql.Literal(label),
                )
            )
        for label in _EDGE_LABELS:
            if self._label_exists(cur, label):
                continue
            cur.execute(
                sql.SQL("SELECT ag_catalog.create_elabel({graph}, {label})").format(
                    graph=sql.Literal(self.graph_name),
                    label=sql.Literal(label),
                )
            )

    @staticmethod
    def _property_index_statement(
        *,
        graph_name: str,
        label: str,
        key: str,
    ) -> sql.Composed:
        return sql.SQL(
            """
            CREATE INDEX IF NOT EXISTS {index_name}
              ON {table_name} USING btree
              ((agtype_access_operator(properties, {prop_key}::agtype)))
            """
        ).format(
            index_name=sql.Identifier(f"idx_{label.lower()}_{key}"),
            table_name=sql.SQL(".").join((sql.Identifier(graph_name), sql.Identifier(label))),
            prop_key=sql.Literal(f'"{key}"'),
        )

    def _run_cypher(
        self,
        query: str,
        *,
        columns: list[str],
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if not columns:
            raise ValueError("columns are required for cypher execution")
        columns_sql = sql.SQL(", ").join(
            sql.SQL("{} agtype").format(sql.Identifier(column)) for column in columns
        )
        bound_query = _bind_cypher_params(query, params)
        statement = sql.SQL(
            "SELECT * FROM ag_catalog.cypher({graph}, {query}) AS ({columns})"
        ).format(
            graph=sql.Literal(self.graph_name),
            query=_cypher_sql_literal(bound_query),
            columns=columns_sql,
        )
        with self._age_cursor() as cur:
            cur.execute(statement)
            rows = cur.fetchall() or []
        return [
            {columns[index]: _decode_agtype(value) for index, value in enumerate(row)}
            for row in rows
        ]

    def _run_write_batch(self, query: str, *, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        total = 0
        for offset in range(0, len(rows), _WRITE_BATCH_SIZE):
            chunk = rows[offset : offset + _WRITE_BATCH_SIZE]
            result = self._run_cypher(query, columns=["count"], params={"rows": chunk})
            if result:
                total += int(result[0].get("count") or 0)
        return total
