"""Existing applicable Agent block selection, shared by readiness and execution."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any


def in_account_scope(row: Mapping[str, Any], account_id: str | None) -> bool:
    """Keep global rows and rows owned by this account."""
    # Legacy preview callers have no request scope; authority callers always pass one.
    if account_id is None:
        return True
    owner = str(row.get("account_id") or "").strip()
    return not owner or bool(account_id and owner == account_id)


def select_agent_block_key(
    rows: Iterable[Mapping[str, Any]], *, agent_id: str, skill_id: str, account_id: str | None
) -> str:
    """Preserve IOResolver's scoring and first-in-order tie behavior."""
    best_key, best_score = "", -1
    full_ref = f"{agent_id}::{skill_id}" if agent_id and skill_id else ""
    for row in rows:
        if (
            not in_account_scope(row, account_id)
            or str(row.get("source_kind") or "").strip() != "agent"
        ):
            continue
        source_ref = str(row.get("source_ref") or "").strip()
        key = str(row.get("block_key") or "").strip()
        if not source_ref or not key:
            continue
        score = -1
        if full_ref and source_ref == full_ref:
            score = 100
        elif skill_id and source_ref == skill_id:
            score = 90
        elif agent_id and source_ref == agent_id and skill_id and skill_id in key:
            score = 80
        elif agent_id and source_ref == agent_id:
            score = 70
        if score > best_score:
            best_key, best_score = key, score
    return best_key


def applicable_agent_block(
    *,
    agent_id: str,
    skill_id: str,
    block_key: str,
    account_id: str | None,
    list_blocks: Callable[[], list[dict]],
    get_block: Callable[[str], dict | None],
) -> dict | None:
    """Resolve explicit or implicit selection without changing assignment authority."""
    key = block_key or select_agent_block_key(
        list_blocks(),
        agent_id=agent_id,
        skill_id=skill_id,
        account_id=account_id,
    )
    if not key:
        return None
    row = get_block(key)
    if not isinstance(row, dict) or not in_account_scope(row, account_id):
        return None
    return row
