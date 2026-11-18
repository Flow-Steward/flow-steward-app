"""The one writer of `projects.config.user_sections` (ADR 0075)."""

from __future__ import annotations

import json
from typing import Any

_SAVE_SECTIONS_SQL = """
UPDATE projects
   SET config = jsonb_set(COALESCE(config, '{}'::jsonb), '{user_sections}', %s::jsonb, true)
 WHERE id = %s
"""
_SAVE_SECTIONS_IN_ACCOUNT_SQL = _SAVE_SECTIONS_SQL + "   AND account_id = %s\n"


def save_project_user_sections(
    conn: Any, *, project_id: str, sections: list[dict[str, Any]], account_id: str | None = None
) -> None:
    """Replace only the sections key, from the stored row, in the caller's transaction.

    `jsonb_set` on the value in the database keeps every other config key as it is
    now, not as an earlier copy of the project had it.
    """
    payload = json.dumps(sections)
    with conn.cursor() as cur:
        if account_id:
            cur.execute(_SAVE_SECTIONS_IN_ACCOUNT_SQL, (payload, str(project_id), str(account_id)))
        else:
            cur.execute(_SAVE_SECTIONS_SQL, (payload, str(project_id)))


def stored_user_sections(project_row: Any) -> list[dict[str, Any]] | None:
    """The raw stored sections of a project row, or None when it never had any."""
    config = project_row.get("config") if isinstance(project_row, dict) else None
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except ValueError:
            return None
    if not isinstance(config, dict) or "user_sections" not in config:
        return None
    raw = config.get("user_sections")
    return [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


__all__ = ["save_project_user_sections", "stored_user_sections"]
