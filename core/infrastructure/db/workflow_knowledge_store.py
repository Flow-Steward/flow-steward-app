"""Postgres/AGE-backed workflow knowledge store."""

from __future__ import annotations

from .workflow_knowledge_store_common import (
    DEFAULT_AGE_GRAPH_NAME,
    PostgresAgeWorkflowKnowledgeStoreBase,
    _bind_cypher_params,
    _cypher_literal,
    _cypher_sql_literal,
    _decode_agtype,
    _json_dumps,
    _resolve_dsn,
    _scope_value,
    _tenant_value,
)
from .workflow_knowledge_store_projection import WorkflowKnowledgeProjectionMixin
from .workflow_knowledge_store_schema import WorkflowKnowledgeSchemaMixin
from .workflow_knowledge_store_search import WorkflowKnowledgeSearchMixin
from .workflow_knowledge_store_sync import WorkflowKnowledgeSyncMixin


class PostgresAgeWorkflowKnowledgeStore(
    WorkflowKnowledgeSyncMixin,
    WorkflowKnowledgeSchemaMixin,
    WorkflowKnowledgeProjectionMixin,
    WorkflowKnowledgeSearchMixin,
    PostgresAgeWorkflowKnowledgeStoreBase,
):
    pass


__all__ = [
    "DEFAULT_AGE_GRAPH_NAME",
    "PostgresAgeWorkflowKnowledgeStore",
    "_bind_cypher_params",
    "_cypher_literal",
    "_cypher_sql_literal",
    "_decode_agtype",
    "_json_dumps",
    "_resolve_dsn",
    "_scope_value",
    "_tenant_value",
]
