#!/usr/bin/env python3
"""Soft-delete throwaway audit / e2e / probe workflows that clutter the catalog.

Release environments accumulate generated workflows from audit and e2e runs
(``audit_*``, ``postdev_*``, ``e2e_ai_builder_*``, ``ui_live_*`` …). This tool
soft-deletes workflows whose id starts with a curated allowlist of throwaway
prefixes, and removes their webhook entrypoints so no dangling public trigger
survives the cleanup. Anything outside the allowlist is refused.

Dry-run is the DEFAULT — nothing is changed until ``--apply`` is passed.

Usage:
  python -m core.application.tooling.ops.cleanup_probe_workflows -- --dry-run
  python -m core.application.tooling.ops.cleanup_probe_workflows -- --apply
  python -m core.application.tooling.ops.cleanup_probe_workflows -- \
    --apply --prefix audit_ --prefix postdev_
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

from core.application.tooling.ops.account_option import (  # noqa: E402
    add_account_argument,
    report_command_account,
    resolve_command_account,
)

#: Curated allowlist of throwaway workflow-id prefixes produced by audit / e2e /
#: probe runs. ONLY these prefixes are ever eligible for cleanup — a prefix outside
#: this set is refused so a real/built-in workflow (demo_*, …) can
#: never be deleted by this tool.
PROBE_WORKFLOW_PREFIXES: tuple[str, ...] = (
    "audit_",
    "postdev_",
    "e2e_ai_builder_",
    "e2e_wf_",
    "ui_live_",
    "live_audit_",
    "release_audit_",
    "test_wf_",
    "test_workflow_",
    "nightfire_clone_",
)


def _like_pattern(prefix: str) -> str:
    """Escape LIKE metacharacters and anchor the prefix match."""
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _account(account_id: str = "") -> str:
    """The one account a cleanup works on, as the command passes it.

    A workflow id names one workflow per account (ADR 0020), so the cleanup never
    selects or deletes across accounts, and never picks an account by itself (C5):
    ``main`` resolves ``--account`` (the installation account when omitted, stated
    in the output).
    """
    from core.application.tooling.ops.account_option import require_account

    return require_account(account_id, operation="probe workflow cleanup")


def _select_sql(prefixes: tuple[str, ...], *, account_id: str = "") -> tuple[str, tuple[Any, ...]]:
    like_clauses = " OR ".join("w.workflow_id LIKE %s ESCAPE '\\'" for _ in prefixes)
    sql = (
        "SELECT DISTINCT w.workflow_id "
        "FROM registry_workflows w "
        f"WHERE ({like_clauses}) AND w.deleted_at IS NULL "
        "AND w.account_id::text = %s "
        "ORDER BY w.workflow_id"
    )
    return sql, (*(_like_pattern(prefix) for prefix in prefixes), _account(account_id))


def _matched_workflow_ids(db: Any, prefixes: tuple[str, ...], *, account_id: str = "") -> list[str]:
    sql, params = _select_sql(prefixes, account_id=account_id)
    cursor_factory = getattr(db, "_cursor", None)
    if not callable(cursor_factory):
        raise RuntimeError("Registry DB backend does not expose a SQL cursor for probe cleanup")
    with cursor_factory() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    ids: list[str] = []
    for row in rows or []:
        if isinstance(row, dict):
            workflow_id = str(row.get("workflow_id") or "").strip()
        elif isinstance(row, (list, tuple)) and row:
            workflow_id = str(row[0] or "").strip()
        else:
            workflow_id = str(getattr(row, "workflow_id", "") or "").strip()
        # Defense in depth: only accept ids that actually start with an allowed prefix.
        if workflow_id and any(workflow_id.startswith(prefix) for prefix in prefixes):
            ids.append(workflow_id)
    return sorted(dict.fromkeys(ids))


def _delete_entrypoints(db: Any, workflow_ids: list[str], *, account_id: str = "") -> int:
    """Remove webhook entrypoints for the cleaned-up workflows so no dangling public
    trigger remains (mirrors the safety concern behind the PD-2 default-binding fix)."""
    if not workflow_ids:
        return 0
    transaction_factory = getattr(db, "_transaction", None)
    if not callable(transaction_factory):
        return 0
    with transaction_factory() as cur:
        cur.execute(
            "DELETE FROM workflow_entrypoints WHERE account_id = %s AND workflow_id = ANY(%s)",
            (_account(account_id), workflow_ids),
        )
        return int(getattr(cur, "rowcount", 0) or 0)


def cleanup_probe_workflows(
    db: Any,
    *,
    apply: bool,
    author: str,
    prefixes: tuple[str, ...] = PROBE_WORKFLOW_PREFIXES,
    account_id: str = "",
) -> dict[str, Any]:
    account = _account(account_id)
    workflow_ids = _matched_workflow_ids(db, prefixes, account_id=account)
    deleted: list[str] = []
    entrypoints_removed = 0
    if apply:
        for workflow_id in workflow_ids:
            db.delete_workflow(workflow_id, author=author, account_id=account)
            deleted.append(workflow_id)
        entrypoints_removed = _delete_entrypoints(db, workflow_ids, account_id=account)
    return {
        "dry_run": not apply,
        "matched_count": len(workflow_ids),
        "deleted_count": len(deleted),
        "entrypoints_removed": entrypoints_removed,
        "matched_workflow_ids": workflow_ids,
        "prefixes": list(prefixes),
        "account_id": account,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Soft-delete audit/e2e/probe workflows (curated prefix allowlist) and "
            "remove their entrypoints. Dry-run is the default."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually soft-delete matching probe workflows. Omit for dry-run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show matching workflows without deleting anything. This is the default.",
    )
    parser.add_argument(
        "--author",
        default="probe_cleanup",
        help="deleted_by author recorded for soft-deleted workflows.",
    )
    parser.add_argument(
        "--prefix",
        action="append",
        default=[],
        metavar="PREFIX",
        help=(
            "Restrict cleanup to specific allowlisted prefix(es). Repeatable. "
            f"Allowed: {', '.join(PROBE_WORKFLOW_PREFIXES)}"
        ),
    )
    add_account_argument(parser, purpose="whose probe workflows are cleaned up")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    from core.application.registry.registry_manager import RegistryManager

    rm = RegistryManager.instance()
    if not rm.db_store:
        raise RuntimeError("Registry DB backend not configured")

    if args.prefix:
        invalid = [prefix for prefix in args.prefix if prefix not in PROBE_WORKFLOW_PREFIXES]
        if invalid:
            raise ValueError(f"Refusing non-allowlisted prefix(es): {invalid}")
        prefixes: tuple[str, ...] = tuple(args.prefix)
    else:
        prefixes = PROBE_WORKFLOW_PREFIXES

    account = resolve_command_account(args)
    report_command_account(account)
    apply_cleanup = bool(args.apply) and not bool(args.dry_run)
    payload = cleanup_probe_workflows(
        rm.db_store,
        apply=apply_cleanup,
        author=str(args.author or "probe_cleanup").strip() or "probe_cleanup",
        prefixes=prefixes,
        account_id=account.account_id,
    )
    payload.update(account.as_payload())
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
