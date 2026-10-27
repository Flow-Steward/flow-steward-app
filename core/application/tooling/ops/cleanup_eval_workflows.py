#!/usr/bin/env python3
"""Safely soft-delete retired AI Builder eval workflows and optionally refresh knowledge.

Usage:
  python -m core.application.tooling.ops.cleanup_eval_workflows -- --dry-run
  python -m core.application.tooling.ops.cleanup_eval_workflows -- --apply
  python -m core.application.tooling.ops.cleanup_eval_workflows -- --apply \
    --sync-workflow-knowledge --account-id <account-id>
  python -m core.application.tooling.ops.cleanup_eval_workflows -- \
    --prune-soft-deleted --apply --account-id <account-id>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

EVAL_WORKFLOW_PREFIX = "eval_"
EVAL_WORKFLOW_LIKE = r"eval\_%"


def _escape_like_prefix(prefix: str) -> str:
    normalized = str(prefix or EVAL_WORKFLOW_PREFIX).strip()
    if not normalized.startswith(EVAL_WORKFLOW_PREFIX):
        raise ValueError(f"Refusing to cleanup non-eval workflow prefix: {normalized}")
    return normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Soft-delete active registry workflows whose workflow_id starts with "
            "'eval_'. Dry-run is the default."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually soft-delete matching eval workflows. Omit for dry-run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show matching workflows without deleting anything. This is the default.",
    )
    parser.add_argument(
        "--author",
        default="eval_cleanup",
        help="deleted_by author recorded for soft-deleted workflows.",
    )
    parser.add_argument(
        "--sync-workflow-knowledge",
        action="store_true",
        help="Rebuild the workflow knowledge projection after an applied cleanup.",
    )
    parser.add_argument(
        "--prune-soft-deleted",
        action="store_true",
        help=(
            "Hard-delete already soft-deleted eval_* registry rows. Dry-run by default; "
            "requires --apply to delete."
        ),
    )
    parser.add_argument(
        "--account-id",
        default="",
        help="Account scope for --sync-workflow-knowledge.",
    )
    parser.add_argument(
        "--tenant-id",
        default="",
        help="Tenant scope for --sync-workflow-knowledge. Defaults to the canonical tenant.",
    )
    parser.add_argument(
        "--project-id",
        default="",
        help="Optional project scope for --sync-workflow-knowledge.",
    )
    return parser.parse_args()


def _select_eval_workflow_ids_sql(
    *,
    account_id: str = "",
    tenant_id: str = "",
    project_id: str = "",
    workflow_id_prefix: str = EVAL_WORKFLOW_PREFIX,
    only_deleted: bool = False,
) -> tuple[str, tuple[Any, ...]]:
    clauses = [
        "w.workflow_id LIKE %s ESCAPE '\\'",
        "w.deleted_at IS NOT NULL" if only_deleted else "w.deleted_at IS NULL",
    ]
    params: list[Any] = [_escape_like_prefix(workflow_id_prefix)]
    normalized_account_id = str(account_id or "").strip()
    if normalized_account_id:
        clauses.append("w.account_id::text = %s")
        params.append(normalized_account_id)
    normalized_tenant_id = str(tenant_id or "").strip()
    normalized_project_id = str(project_id or "").strip()
    if normalized_project_id or normalized_tenant_id:
        clauses.append(
            """EXISTS (
                SELECT 1
                FROM project_workflows pw
                JOIN projects p ON p.id = pw.project_id
                WHERE pw.workflow_id = w.workflow_id
                  AND (%s = '' OR pw.project_id::text = %s)
                  AND (%s = '' OR p.tenant_id::text = %s)
            )"""
        )
        params.extend(
            [
                normalized_project_id,
                normalized_project_id,
                normalized_tenant_id,
                normalized_tenant_id,
            ]
        )
    sql = (
        "SELECT DISTINCT w.workflow_id "
        "FROM registry_workflows w "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY w.workflow_id"
    )
    return sql, tuple(params)


def _rows_to_workflow_ids(rows: list[Any]) -> list[str]:
    ids: list[str] = []
    for row in rows:
        if isinstance(row, dict):
            workflow_id = str(row.get("workflow_id") or "").strip()
        elif isinstance(row, (list, tuple)) and row:
            workflow_id = str(row[0] or "").strip()
        else:
            workflow_id = str(getattr(row, "workflow_id", "") or "").strip()
        if workflow_id.startswith(EVAL_WORKFLOW_PREFIX):
            ids.append(workflow_id)
    return sorted(dict.fromkeys(ids))


def _eval_workflow_ids(
    db: Any,
    *,
    account_id: str = "",
    tenant_id: str = "",
    project_id: str = "",
    workflow_id_prefix: str = EVAL_WORKFLOW_PREFIX,
    only_deleted: bool = False,
) -> list[str]:
    sql, params = _select_eval_workflow_ids_sql(
        account_id=account_id,
        tenant_id=tenant_id,
        project_id=project_id,
        workflow_id_prefix=workflow_id_prefix,
        only_deleted=only_deleted,
    )
    selector = getattr(db, "select_eval_workflow_ids", None)
    if callable(selector):
        return _rows_to_workflow_ids(list(selector(sql, params) or []))
    cursor_factory = getattr(db, "_cursor", None)
    if not callable(cursor_factory):
        raise RuntimeError("Registry DB backend does not expose a SQL cursor for eval cleanup")
    with cursor_factory() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return _rows_to_workflow_ids(list(rows or []))


def _hard_delete_eval_workflow_ids(db: Any, workflow_ids: list[str]) -> int:
    if not workflow_ids:
        return 0
    pruner = getattr(db, "prune_eval_workflows", None)
    if callable(pruner):
        return int(pruner(workflow_ids) or 0)
    transaction_factory = getattr(db, "_transaction", None)
    if not callable(transaction_factory):
        raise RuntimeError("Registry DB backend does not expose a SQL transaction for eval prune")
    with transaction_factory() as cur:
        from core.application.workflows.workflow_deletion_guard import (
            assert_workflow_has_no_nonterminal_jobs,
        )

        for workflow_id in workflow_ids:
            assert_workflow_has_no_nonterminal_jobs(
                db,
                workflow_id=workflow_id,
                cursor=cur,
            )
        cur.execute("DELETE FROM project_workflows WHERE workflow_id = ANY(%s)", (workflow_ids,))
        cur.execute("DELETE FROM registry_workflows WHERE workflow_id = ANY(%s)", (workflow_ids,))
        return int(getattr(cur, "rowcount", 0) or 0)


def _prune_soft_deleted_eval_workflows(
    db: Any,
    *,
    apply: bool,
    account_id: str = "",
    tenant_id: str = "",
    project_id: str = "",
    workflow_id_prefix: str = EVAL_WORKFLOW_PREFIX,
) -> dict[str, Any]:
    workflow_ids = _eval_workflow_ids(
        db,
        account_id=account_id,
        tenant_id=tenant_id,
        project_id=project_id,
        workflow_id_prefix=workflow_id_prefix,
        only_deleted=True,
    )
    pruned_count = _hard_delete_eval_workflow_ids(db, workflow_ids) if apply else 0
    return {
        "dry_run": not apply,
        "prune_soft_deleted": True,
        "matched_count": len(workflow_ids),
        "pruned_count": pruned_count,
        "matched_workflow_ids": workflow_ids,
        "pruned_workflow_ids": workflow_ids if apply else [],
        "account_id": str(account_id or "").strip(),
        "tenant_id": str(tenant_id or "").strip(),
        "project_id": str(project_id or "").strip(),
        "workflow_id_prefix": str(workflow_id_prefix or EVAL_WORKFLOW_PREFIX).strip(),
    }


def _cleanup_eval_workflows(
    db: Any,
    *,
    apply: bool,
    author: str,
    account_id: str = "",
    tenant_id: str = "",
    project_id: str = "",
    workflow_id_prefix: str = EVAL_WORKFLOW_PREFIX,
) -> dict[str, Any]:
    workflow_ids = _eval_workflow_ids(
        db,
        account_id=account_id,
        tenant_id=tenant_id,
        project_id=project_id,
        workflow_id_prefix=workflow_id_prefix,
    )
    deleted: list[str] = []
    if apply:
        for workflow_id in workflow_ids:
            db.delete_workflow(workflow_id, author=author)
            deleted.append(workflow_id)
    return {
        "dry_run": not apply,
        "matched_count": len(workflow_ids),
        "deleted_count": len(deleted),
        "matched_workflow_ids": workflow_ids,
        "deleted_workflow_ids": deleted,
        "account_id": str(account_id or "").strip(),
        "tenant_id": str(tenant_id or "").strip(),
        "project_id": str(project_id or "").strip(),
        "workflow_id_prefix": str(workflow_id_prefix or EVAL_WORKFLOW_PREFIX).strip(),
    }


def _sync_workflow_knowledge(
    *,
    db: Any,
    account_id: str,
    tenant_id: str,
    project_id: str,
) -> dict[str, Any]:
    from core.application.auth.config import DEFAULT_TENANT_ID
    from core.application.workflows.workflow_knowledge_sync import (
        sync_workflow_knowledge_catalog,
    )
    from core.infrastructure.db.workflow_knowledge_store import PostgresAgeWorkflowKnowledgeStore

    normalized_account_id = str(account_id or "").strip()
    if not normalized_account_id:
        raise ValueError("--account-id is required with --sync-workflow-knowledge")
    normalized_tenant_id = str(tenant_id or "").strip() or DEFAULT_TENANT_ID
    store = PostgresAgeWorkflowKnowledgeStore(dsn=getattr(db, "dsn", None))
    return sync_workflow_knowledge_catalog(
        store=store,
        db=db,
        account_id=normalized_account_id,
        tenant_id=normalized_tenant_id,
        project_id=str(project_id or "").strip(),
        dry_run=False,
        include_capability_catalog=True,
        ensure_indexes=True,
    )


def main() -> int:
    args = _parse_args()
    from core.application.registry.registry_manager import RegistryManager

    rm = RegistryManager.instance()
    if not rm.db_store:
        raise RuntimeError("Registry DB backend not configured")

    apply_cleanup = bool(args.apply) and not bool(args.dry_run)
    if bool(args.prune_soft_deleted):
        payload = _prune_soft_deleted_eval_workflows(
            rm.db_store,
            apply=apply_cleanup,
            account_id=str(args.account_id or ""),
            tenant_id=str(args.tenant_id or ""),
            project_id=str(args.project_id or ""),
        )
    else:
        payload = _cleanup_eval_workflows(
            rm.db_store,
            apply=apply_cleanup,
            author=str(args.author or "eval_cleanup").strip() or "eval_cleanup",
            account_id=str(args.account_id or ""),
            tenant_id=str(args.tenant_id or ""),
            project_id=str(args.project_id or ""),
        )
    if apply_cleanup and bool(args.sync_workflow_knowledge):
        payload["workflow_knowledge_sync"] = _sync_workflow_knowledge(
            db=rm.db_store,
            account_id=str(args.account_id or ""),
            tenant_id=str(args.tenant_id or ""),
            project_id=str(args.project_id or ""),
        )
    elif bool(args.sync_workflow_knowledge):
        payload["workflow_knowledge_sync"] = {
            "skipped": True,
            "reason": "cleanup_dry_run",
        }
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
