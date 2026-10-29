"""Manual agents that retain a hard reference to an installed projected skill."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def collect_projected_skill_dependents(
    bl: Any, *, install_id: str, account_id: str
) -> list[dict[str, Any]]:
    table_exists = getattr(bl, "_table_exists", None)
    cursor_factory = getattr(bl, "cursor", None)
    if not callable(cursor_factory):
        cursor_factory = getattr(getattr(bl, "conn", None), "cursor", None)
    if not callable(table_exists) or not callable(cursor_factory):
        return []
    if not all(
        table_exists(name)
        for name in ("registry_skills", "registry_agent_skills", "registry_agents")
    ):
        return []
    with cursor_factory() as cur:
        cur.execute(
            """SELECT DISTINCT a.agent_id,
                      COALESCE(a.display_name, a.agent_id) AS display_name
                 FROM registry_skills s
                 JOIN registry_agent_skills r ON r.skill_id = s.skill_id
                 JOIN registry_agents a ON a.agent_id = r.agent_id
                WHERE s.account_id::text = %s AND s.source_install_id = %s
                  AND s.skill_id LIKE %s
                  AND a.account_id::text = %s AND a.project_id IS NULL
                  AND COALESCE(a.scope_kind, '') <> 'project'
                  AND COALESCE(a.source_install_id, '') = ''
                ORDER BY a.agent_id""",
            (account_id, install_id, "extension.%", account_id),
        )
        values = cur.fetchall() or []
        if not values:
            return []
        if isinstance(values[0], Mapping):
            rows = [dict(row) for row in values]
        else:
            columns = [str(column.name) for column in (cur.description or [])]
            rows = [dict(zip(columns, row, strict=False)) for row in values]
    return [
        {
            "kind": "agent",
            "id": str(row.get("agent_id") or "").strip(),
            "display_name": str(row.get("display_name") or row.get("agent_id") or "").strip(),
            "owner_install_id": "",
            "owner_account_id": account_id,
            "action": "preserve_and_disable",
            "reason": "Uses a skill provided by this extension; the agent will stop until the extension is reinstalled or the skill is replaced.",
        }
        for row in rows
        if str(row.get("agent_id") or "").strip()
    ]


__all__ = ["collect_projected_skill_dependents"]
