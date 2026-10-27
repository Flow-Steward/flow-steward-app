"""Synchronise a project's profile document into canonical memory."""

from __future__ import annotations

from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID


def write_project_profile_to_kb(
    project_row: dict[str, Any],
    *,
    logger,
    write_project_profile,
) -> None:
    """Write/update a persistent project profile document in canonical memory."""
    try:
        raw_project_id = project_row.get("id")
        project_id = str(raw_project_id or "").strip() or None
        write_project_profile(
            project_name=project_row.get("name", ""),
            project_rules=project_row.get("project_rules") or "",
            assigned_data_connections=project_row.get("assigned_data_connections") or [],
            account_id=str(project_row.get("account_id") or DEFAULT_ACCOUNT_ID),
            project_id=project_id,
        )
    except Exception as exc:
        logger.warning("write_project_profile_to_kb failed (non-fatal): %s", exc)
