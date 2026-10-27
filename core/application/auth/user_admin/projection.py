"""Safe public projections for user-administration results."""

from __future__ import annotations

from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID


def normalize_user_payload(
    payload: dict[str, Any] | None,
    *,
    membership_map: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    normalized = dict(payload or {})
    normalized["roles"] = [
        str(role).strip() for role in (normalized.get("roles") or []) if str(role).strip()
    ]
    normalized["email"] = str(normalized.get("email") or normalized.get("username") or "").strip()

    user_id = str(normalized.get("id") or "").strip()
    memberships: list[dict[str, Any]] = []
    if membership_map is not None and user_id:
        memberships = [
            dict(item or {}) for item in membership_map.get(user_id, []) if isinstance(item, dict)
        ]
    if memberships:
        normalized["account_memberships"] = memberships
        normalized["account_ids"] = [
            str(item.get("account_id") or "").strip()
            for item in memberships
            if str(item.get("account_id") or "").strip()
        ]
        account_id = str(normalized.get("account_id") or "").strip()
        if not account_id:
            account_id = (
                next(
                    (
                        str(item.get("account_id") or "").strip()
                        for item in memberships
                        if bool(item.get("is_default"))
                    ),
                    "",
                )
                or str((memberships[0] or {}).get("account_id") or "").strip()
            )
            normalized["account_id"] = account_id
        active_membership = next(
            (
                item
                for item in memberships
                if str(item.get("account_id") or "").strip() == account_id
            ),
            memberships[0],
        )
        normalized["account_display_name"] = (
            str(
                active_membership.get("display_name")
                or normalized.get("account_id")
                or DEFAULT_ACCOUNT_ID
            ).strip()
            or DEFAULT_ACCOUNT_ID
        )
    else:
        normalized.setdefault("account_memberships", [])
        normalized.setdefault("account_ids", [])
        normalized.setdefault(
            "account_display_name",
            str(normalized.get("account_id") or DEFAULT_ACCOUNT_ID).strip() or DEFAULT_ACCOUNT_ID,
        )
    return normalized
