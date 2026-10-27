"""Shared read and mutation permission projection for application clients."""

from typing import Any


def resolve_effective_permissions(
    *,
    principal_type: str = "",
    effective_permissions: dict[str, Any] | None = None,
) -> dict[str, bool]:
    """Resolve active permissions without allowing viewers to gain mutation access."""
    payload = dict(effective_permissions or {})
    normalized_principal = str(principal_type or "").strip().lower()
    raw_roles = payload.get("roles")
    if isinstance(raw_roles, str):
        role_hints = {raw_roles.strip().lower()} if raw_roles.strip() else set()
    elif isinstance(raw_roles, (list, tuple, set)):
        role_hints = {
            str(role or "").strip().lower() for role in raw_roles if str(role or "").strip()
        }
    else:
        role_hints = set()
    is_read_only = normalized_principal in {"viewer", ""} or "viewer" in role_hints
    can_read = bool(payload.get("can_read")) if "can_read" in payload else True
    requested_can_mutate = (
        bool(payload.get("can_mutate")) if "can_mutate" in payload else not is_read_only
    )
    return {"can_read": can_read, "can_mutate": bool(requested_can_mutate and not is_read_only)}
