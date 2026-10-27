from __future__ import annotations

import pytest

from core.application.search.search_postgres_catalog import _catalog_query

CATALOG_GROUPS = (
    "agents",
    "tools",
    "skills",
    "mcp_connectors",
    "projects",
    "extensions",
    "guardrails",
    "data_contracts",
    "fields",
    "published_blocks",
    "datasets",
    "saved_queries",
    "data_connections",
    "model_providers",
    "model_routes",
    "users",
    "workspaces",
)


@pytest.mark.parametrize("group", CATALOG_GROUPS)
def test_release_catalog_search_queries_compile_against_canonical_schema(
    disposable_db_conn,
    group: str,
) -> None:
    sql, params, _route = _catalog_query(
        group=group,
        query="guaranteed-miss",
        account_id="00000000-0000-0000-0000-000000000002",
        tenant_id="00000000-0000-0000-0000-000000000001",
        workspace_id=None,
        project_ids=["00000000-0000-0000-0000-000000000007"],
    )

    with disposable_db_conn.cursor() as cur:
        cur.execute(f"{sql} ORDER BY title, entity_id LIMIT %s", [*params, 1])  # nosec B608
        assert cur.fetchall() == []
