"""Row-level sync and dry-run diff for the public contract projection.

``public_contract_projection`` builds the desired projection state for an
account; this module compares it with the stored data contracts, fields and
published blocks, writes the changes (``_sync_*``) or only counts them for a
dry run (``_summarize_*``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.application.registry.public_contract_projection import ProjectionSummary


def _sync_data_contracts(
    db,
    current_rows: dict[str, dict],
    desired_rows: dict[str, dict],
    summary: ProjectionSummary,
    account_id: str,
) -> None:
    desired_keys = set(desired_rows)
    for contract_id, payload in desired_rows.items():
        existing = current_rows.get(contract_id)
        if existing and str(existing.get("projection_mode") or "manual") != "derived":
            summary.manual_preserved += 1
            continue
        if existing and _contract_row_matches(existing, payload):
            summary.unchanged += 1
            continue
        summary.created += 1 if not existing else 0
        summary.updated += 0 if not existing else 1
        db.save_data_contract(payload, author="projection_sync", account_id=account_id)

    for contract_id, row in current_rows.items():
        if str(row.get("projection_mode") or "manual") != "derived":
            continue
        if contract_id in desired_keys:
            continue
        orphaned = {
            **row,
            "enabled": False,
            "sync_status": "orphaned",
            "last_synced_at": datetime.now(UTC),
        }
        if str(row.get("sync_status") or "") == "orphaned" and row.get("enabled") is False:
            summary.unchanged += 1
            continue
        summary.orphaned += 1
        summary.disabled += 1
        db.save_data_contract(orphaned, author="projection_sync", account_id=account_id)


def _sync_fields(
    db,
    current_rows: dict[str, dict],
    desired_rows: dict[str, dict],
    summary: ProjectionSummary,
    account_id: str,
) -> None:
    desired_keys = set(desired_rows)
    for field_key, payload in desired_rows.items():
        existing = current_rows.get(field_key)
        if existing and str(existing.get("projection_mode") or "manual") != "derived":
            summary.manual_preserved += 1
            continue
        if existing and _field_row_matches(existing, payload):
            summary.unchanged += 1
            continue
        summary.created += 1 if not existing else 0
        summary.updated += 0 if not existing else 1
        db.save_field(payload, author="projection_sync", account_id=account_id)

    for field_key, row in current_rows.items():
        if str(row.get("projection_mode") or "manual") != "derived":
            continue
        if field_key in desired_keys:
            continue
        orphaned = {
            **row,
            "enabled": False,
            "sync_status": "orphaned",
            "last_synced_at": datetime.now(UTC),
        }
        if str(row.get("sync_status") or "") == "orphaned" and row.get("enabled") is False:
            summary.unchanged += 1
            continue
        summary.orphaned += 1
        summary.disabled += 1
        db.save_field(orphaned, author="projection_sync", account_id=account_id)


def _sync_blocks(
    db, current_rows: dict[str, dict], desired_rows: dict[str, dict], summary: ProjectionSummary
) -> None:
    desired_keys = set(desired_rows)
    for block_key, payload in desired_rows.items():
        existing = current_rows.get(block_key)
        if existing and str(existing.get("projection_mode") or "manual") != "derived":
            summary.manual_preserved += 1
            continue
        if existing and _block_row_matches(existing, payload):
            summary.unchanged += 1
            continue
        summary.created += 1 if not existing else 0
        summary.updated += 0 if not existing else 1
        db.save_published_block_contract(payload, author="projection_sync")

    for block_key, row in current_rows.items():
        if str(row.get("projection_mode") or "manual") != "derived":
            continue
        if block_key in desired_keys:
            continue
        if str(row.get("sync_status") or "") == "orphaned" and row.get("enabled") is False:
            summary.unchanged += 1
            continue
        full = db.get_published_block_contract(block_key) or row
        orphaned = {
            **full,
            "enabled": False,
            "sync_status": "orphaned",
            "last_synced_at": datetime.now(UTC),
            "inputs": list(full.get("inputs") or full.get("_inputs") or []),
            "outputs": list(full.get("outputs") or full.get("_outputs") or []),
        }
        summary.orphaned += 1
        summary.disabled += 1
        db.save_published_block_contract(orphaned, author="projection_sync")


def _summarize_data_contract_diff(
    current_rows: dict[str, dict], desired_rows: dict[str, dict], summary: ProjectionSummary
) -> None:
    for contract_id, payload in desired_rows.items():
        existing = current_rows.get(contract_id)
        if existing and str(existing.get("projection_mode") or "manual") != "derived":
            summary.manual_preserved += 1
        elif not existing:
            summary.created += 1
        elif _contract_row_matches(existing, payload):
            summary.unchanged += 1
        else:
            summary.updated += 1
    for contract_id, row in current_rows.items():
        if (
            str(row.get("projection_mode") or "manual") == "derived"
            and contract_id not in desired_rows
        ):
            summary.orphaned += 1
            summary.disabled += 1


def _summarize_field_diff(
    current_rows: dict[str, dict], desired_rows: dict[str, dict], summary: ProjectionSummary
) -> None:
    for field_key, payload in desired_rows.items():
        existing = current_rows.get(field_key)
        if existing and str(existing.get("projection_mode") or "manual") != "derived":
            summary.manual_preserved += 1
        elif not existing:
            summary.created += 1
        elif _field_row_matches(existing, payload):
            summary.unchanged += 1
        else:
            summary.updated += 1
    for field_key, row in current_rows.items():
        if (
            str(row.get("projection_mode") or "manual") == "derived"
            and field_key not in desired_rows
        ):
            summary.orphaned += 1
            summary.disabled += 1


def _summarize_block_diff(
    current_rows: dict[str, dict], desired_rows: dict[str, dict], summary: ProjectionSummary
) -> None:
    for block_key, payload in desired_rows.items():
        existing = current_rows.get(block_key)
        if existing and str(existing.get("projection_mode") or "manual") != "derived":
            summary.manual_preserved += 1
        elif not existing:
            summary.created += 1
        elif _block_row_matches(existing, payload):
            summary.unchanged += 1
        else:
            summary.updated += 1
    for block_key, row in current_rows.items():
        if (
            str(row.get("projection_mode") or "manual") == "derived"
            and block_key not in desired_rows
        ):
            summary.orphaned += 1
            summary.disabled += 1


def _field_row_matches(existing: dict[str, Any], payload: dict[str, Any]) -> bool:
    return (
        str(existing.get("projection_mode") or "manual") == "derived"
        and str(existing.get("sync_status") or "active")
        == str(payload.get("sync_status") or "active")
        and bool(existing.get("enabled", True)) == bool(payload.get("enabled", True))
        and str(existing.get("derived_hash") or "") == str(payload.get("derived_hash") or "")
        and str(existing.get("data_contract_id") or "")
        == str(payload.get("data_contract_id") or "")
    )


def _contract_row_matches(existing: dict[str, Any], payload: dict[str, Any]) -> bool:
    return (
        str(existing.get("projection_mode") or "manual") == "derived"
        and str(existing.get("sync_status") or "active")
        == str(payload.get("sync_status") or "active")
        and bool(existing.get("enabled", True)) == bool(payload.get("enabled", True))
        and str(existing.get("derived_hash") or "") == str(payload.get("derived_hash") or "")
        and str(existing.get("kind") or "json") == str(payload.get("kind") or "json")
    )


def _block_row_matches(existing: dict[str, Any], payload: dict[str, Any]) -> bool:
    return (
        str(existing.get("projection_mode") or "manual") == "derived"
        and str(existing.get("sync_status") or "active")
        == str(payload.get("sync_status") or "active")
        and bool(existing.get("enabled", True)) == bool(payload.get("enabled", True))
        and str(existing.get("derived_hash") or "") == str(payload.get("derived_hash") or "")
    )
