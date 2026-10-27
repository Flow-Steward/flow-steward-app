#!/usr/bin/env python3
"""Rebuild the derived workflow-knowledge AGE graph after database restore."""

from __future__ import annotations

import argparse
import json
from typing import Any


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--drop-only",
        action="store_true",
        help="Remove derived AGE state without rebuilding it (for a clean relational dump).",
    )
    return parser.parse_args()


def _stored_scopes(store: Any) -> list[dict[str, str]]:
    with store._connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT account_id::text, tenant_id::text, project_scope
              FROM public.platform_workflow_knowledge_documents
            UNION
            SELECT DISTINCT account_id::text, tenant_id::text, project_scope
              FROM public.platform_workflow_knowledge_nodes
            ORDER BY 1, 2, 3
            """
        )
        rows = cur.fetchall() or []
    return [
        {
            "account_id": str(row[0] or ""),
            "tenant_id": str(row[1] or ""),
            "project_id": str(row[2] or ""),
        }
        for row in rows
        if row and str(row[0] or "").strip()
    ]


def main() -> int:
    args = _parse_args()
    from core.application.registry.registry_manager import RegistryManager
    from core.application.workflows.workflow_knowledge_sync import (
        sync_workflow_knowledge_catalog,
    )
    from core.infrastructure.db.workflow_knowledge_store import (
        PostgresAgeWorkflowKnowledgeStore,
    )

    registry = RegistryManager.instance().db_store
    if registry is None:
        raise RuntimeError("Registry DB backend not configured")
    store = PostgresAgeWorkflowKnowledgeStore(dsn=getattr(registry, "dsn", None))
    scopes = _stored_scopes(store)
    store.reset_graph()
    if args.drop_only:
        # reset_graph creates a fresh empty graph; drop it once more so pg_dump
        # contains no AGE graph catalog/schema state at all.
        with store._age_cursor() as cur:
            if store._graph_exists(cur):
                from psycopg import sql

                cur.execute(
                    sql.SQL("SELECT ag_catalog.drop_graph({graph}, true)").format(
                        graph=sql.Literal(store.graph_name)
                    )
                )
        print(json.dumps({"dropped": True, "scope_count": len(scopes)}, sort_keys=True))
        return 0

    synced: list[dict[str, Any]] = []
    for scope in scopes:
        synced.append(
            sync_workflow_knowledge_catalog(
                store=store,
                db=registry,
                account_id=scope["account_id"],
                tenant_id=scope["tenant_id"],
                project_id=scope["project_id"] or None,
                include_capability_catalog=True,
                ensure_indexes=True,
            )
        )
    probe = store.semantic_read_write_probe()
    print(
        json.dumps(
            {"rebuilt": True, "scope_count": len(scopes), "probe": probe, "synced": synced},
            sort_keys=True,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
