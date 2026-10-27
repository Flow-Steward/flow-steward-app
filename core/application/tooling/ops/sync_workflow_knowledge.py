#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync or inspect the workflow knowledge catalog index."
    )
    parser.add_argument("--account-id", required=True, help="Account scope to sync.")
    parser.add_argument(
        "--tenant-id",
        default="",
        help="Tenant scope to sync. Defaults to the canonical DEFAULT_TENANT_ID when omitted "
        "so the index lands in the same scope workflow discovery reads from.",
    )
    parser.add_argument("--project-id", default="", help="Optional project scope to sync.")
    parser.add_argument(
        "--status-only",
        action="store_true",
        help="Only report runtime/count/digest status; do not build or write a projection.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the catalog projection and show counts without writing it.",
    )
    parser.add_argument(
        "--if-stale",
        action="store_true",
        help="Sync only when the scoped workflow knowledge digest is empty or stale.",
    )
    parser.add_argument(
        "--no-capability-catalog",
        action="store_true",
        help="Sync workflows only; omit standalone agents, skills, tools, and step-kind profiles.",
    )
    parser.add_argument(
        "--ensure-indexes",
        action="store_true",
        help="Create graph/vector indexes before reporting status or syncing.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    from core.application.auth.config import DEFAULT_TENANT_ID
    from core.application.registry.registry_manager import RegistryManager
    from core.application.workflows.workflow_knowledge_sync import (
        summarize_scope_status,
        sync_workflow_knowledge_catalog,
        sync_workflow_knowledge_catalog_if_stale,
        workflow_knowledge_scope_status,
    )
    from core.infrastructure.db.workflow_knowledge_store import PostgresAgeWorkflowKnowledgeStore

    rm = RegistryManager.instance()
    if not rm.db_store:
        raise RuntimeError("Registry DB backend not configured")

    # A blank tenant writes/reads a scope that live discovery never queries; resolve
    # to the canonical tenant so status, sync, and discovery all agree on one scope.
    tenant_id = (args.tenant_id or "").strip() or DEFAULT_TENANT_ID

    store = PostgresAgeWorkflowKnowledgeStore(dsn=getattr(rm.db_store, "dsn", None))
    if args.status_only:
        payload = workflow_knowledge_scope_status(
            store=store,
            db=rm.db_store,
            account_id=args.account_id,
            tenant_id=tenant_id,
            project_id=args.project_id,
            ensure_indexes=bool(args.ensure_indexes),
        )
    elif args.if_stale:
        payload = sync_workflow_knowledge_catalog_if_stale(
            store=store,
            db=rm.db_store,
            account_id=args.account_id,
            tenant_id=tenant_id,
            project_id=args.project_id,
            dry_run=bool(args.dry_run),
            include_capability_catalog=not bool(args.no_capability_catalog),
            ensure_indexes=bool(args.ensure_indexes),
        )
    else:
        payload = sync_workflow_knowledge_catalog(
            store=store,
            db=rm.db_store,
            account_id=args.account_id,
            tenant_id=tenant_id,
            project_id=args.project_id,
            dry_run=bool(args.dry_run),
            include_capability_catalog=not bool(args.no_capability_catalog),
            ensure_indexes=bool(args.ensure_indexes),
        )
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    print(summarize_scope_status(payload), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
