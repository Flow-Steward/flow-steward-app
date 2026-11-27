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


def limit_view_to_accounts(
    view: dict[str, Any],
    visible_account_ids: list[str],
    *,
    role_account_id: str | None = None,
) -> dict[str, Any]:
    """The user view as an administrator of ``visible_account_ids`` may see it.

    Roles and memberships are per account (ADR 0085): an administrator learns which
    of the accounts they manage a person belongs to, never the person's other
    accounts. A home account outside them is replaced by the first visible one.

    ``roles`` is the person's role in one visible account: ``role_account_id`` when it
    is visible, else the view's (visible) account. A role of an account the
    administrator does not manage is never shown (ADR 0085, C3); a person without a
    membership role there has none.
    """
    visible = {str(item or "").strip() for item in visible_account_ids or ()} - {""}
    limited = dict(view or {})
    all_memberships = [
        dict(item) for item in limited.get("account_memberships") or [] if isinstance(item, dict)
    ]
    home_account = str(limited.get("account_id") or "").strip()
    memberships = [
        item for item in all_memberships if str(item.get("account_id") or "").strip() in visible
    ]
    limited["account_memberships"] = memberships
    limited["account_ids"] = [
        account_id
        for account_id in (str(item or "").strip() for item in limited.get("account_ids") or [])
        if account_id in visible
    ]
    if home_account not in visible:
        first = memberships[0] if memberships else {}
        limited["account_id"] = str(first.get("account_id") or "").strip()
        limited["account_display_name"] = str(
            first.get("display_name") or limited["account_id"]
        ).strip()
    role_account = str(role_account_id or "").strip()
    if role_account not in visible:
        role_account = str(limited.get("account_id") or "").strip()
    membership = next(
        (item for item in memberships if str(item.get("account_id") or "").strip() == role_account),
        None,
    )
    if membership is not None and "role" in membership:
        role = str(membership.get("role") or "").strip()
        limited["roles"] = [role] if role else []
    elif role_account and role_account == home_account and role_account in visible:
        # Storage without membership roles: the view's roles are those of its home
        # account, which the administrator manages.
        limited["roles"] = list(limited.get("roles") or [])
    else:
        limited["roles"] = []
    return limited
