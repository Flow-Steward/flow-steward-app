"""Database composition used only by workflow API/query entrypoints."""

from __future__ import annotations

from typing import Any

from core.infrastructure.db.workflow_knowledge_store import PostgresAgeWorkflowKnowledgeStore
from core.infrastructure.db.workflow_secret_default_repository import (
    PostgresWorkflowSecretDefaultRepository,
)
from core.infrastructure.db.workflow_test_run_query_repository import (
    PostgresWorkflowTestRunJobRepository,
)


def create_workflow_knowledge_store(dsn: str) -> Any:
    return PostgresAgeWorkflowKnowledgeStore(dsn=dsn)


def create_workflow_test_run_repository(*, get_db=None) -> Any:
    return PostgresWorkflowTestRunJobRepository(get_db=get_db)


def create_workflow_secret_default_repository(registry_store: Any) -> Any:
    return PostgresWorkflowSecretDefaultRepository(registry_store)


__all__ = [
    "create_workflow_knowledge_store",
    "create_workflow_secret_default_repository",
    "create_workflow_test_run_repository",
]
