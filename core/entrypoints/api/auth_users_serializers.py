from __future__ import annotations

from typing import Any


def serialize_auth_user_rows(
    rows: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
    membership_map: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    users: list[dict[str, Any]] = []
    for row in rows or []:
        payload = dict(row or {})
        memberships = membership_map.get(str(payload.get("id") or "").strip(), [])
        if not memberships and str(payload.get("account_id") or "").strip():
            memberships = [
                {
                    "account_id": str(payload.get("account_id") or "").strip(),
                    "display_name": "",
                    "is_active": True,
                    "is_default": True,
                }
            ]
        payload["roles"] = [
            str(role_name)
            for role_name in (payload.get("roles") or [])
            if str(role_name or "").strip()
        ]
        payload["email"] = str(payload.get("username") or "")
        payload["account_memberships"] = memberships
        payload["account_ids"] = [
            str(item.get("account_id") or "").strip()
            for item in memberships
            if str(item.get("account_id") or "").strip()
        ]
        users.append(payload)
    return users
