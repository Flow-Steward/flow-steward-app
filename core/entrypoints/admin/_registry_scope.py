# mypy: ignore-errors
"""Shared account-scope helpers for registry admin routes.

Registry objects (agents, skills, published blocks) carry an optional
``account_id`` column. Rows with a NULL/empty ``account_id`` are platform-global
("system") registry objects that every scoped admin may read. Rows that DO carry
a concrete ``account_id`` are owned by a single account and must only be visible
to — and mutable by — a caller in that same account.

These helpers fail closed: a caller in account A is denied (404) when reading or
mutating a registry object owned by account B. They intentionally allow
NULL-account global rows so the established platform-wide registry semantics keep
working, mirroring ``datasets_helpers._scope_matches_row`` and the
``list_agents_summary`` nullable-scope filter.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from core.application.auth.config import DEFAULT_ACCOUNT_ID, DEFAULT_TENANT_ID
from core.application.management.scope import ScopeContext, normalize_scope


def request_scope(request: Request) -> ScopeContext:
    """Resolve the caller's ScopeContext from request state (auth middleware)."""
    state_scope = getattr(request.state, "scope_context", None)
    if state_scope is not None:
        return normalize_scope(state_scope)
    auth_user = getattr(request.state, "auth_user", None)
    if isinstance(auth_user, dict):
        return normalize_scope(
            auth_user,
            account_id=str(auth_user.get("account_id") or DEFAULT_ACCOUNT_ID).strip(),
            tenant_id=str(auth_user.get("tenant_id") or DEFAULT_TENANT_ID).strip(),
            actor_id=str(auth_user.get("username") or auth_user.get("id") or "").strip() or None,
            principal_type=str(auth_user.get("principal_type") or "").strip() or None,
        )
    return normalize_scope()


def row_in_account_scope(row: dict | None, scope: ScopeContext | None) -> bool:
    """True if *row* is global (no account_id) or owned by the caller's account.

    Fails closed on anything that is not a mapping. The registry stores return
    ``dict | None``; a non-dict value means the caller's assumption about the
    store is wrong, and an ownership check is the last place to guess.
    """
    if row is None:
        return False
    if not isinstance(row, dict):
        return False
    row_account_id = str(row.get("account_id") or "").strip()
    if not row_account_id:
        # Platform-global registry object — visible to every scoped admin.
        return True
    scope_account_id = str(getattr(scope, "account_id", "") or "").strip()
    if not scope_account_id:
        # Caller has no resolved account but the row is owned — fail closed.
        return False
    return row_account_id == scope_account_id


def assert_row_in_account_scope(
    row: dict | None,
    scope: ScopeContext | None,
    *,
    detail: str,
) -> dict:
    """Return *row* when it is in the caller's account scope, else raise 404."""
    if not row_in_account_scope(row, scope):
        raise HTTPException(status_code=404, detail=detail)
    return row


def assert_create_account_scope(
    payload: dict[str, Any],
    scope: ScopeContext | None,
) -> None:
    """Reject creating/owning a registry object under a foreign account.

    A scoped admin may create a global row (no account_id) or a row owned by their
    own account, but may not stamp another account's id onto a new object.
    """
    requested_account_id = str((payload or {}).get("account_id") or "").strip()
    if not requested_account_id:
        return
    scope_account_id = str(getattr(scope, "account_id", "") or "").strip()
    if scope_account_id and requested_account_id != scope_account_id:
        raise HTTPException(status_code=403, detail="Access denied")


def require_default_registry_account(scope: ScopeContext | None) -> None:
    """Fail closed for deployment-global catalogs in the OSS single-tenant build.

    Fields and data contracts intentionally remain one deployment-wide catalog
    in the open-source schema. Until a future account-qualified schema exists,
    only the authoritative default account may reach that global
    store; dormant foreign-account sessions must never read or mutate it.
    """

    account_id = str(getattr(scope, "account_id", "") or DEFAULT_ACCOUNT_ID).strip()
    if (account_id or DEFAULT_ACCOUNT_ID) != DEFAULT_ACCOUNT_ID:
        raise HTTPException(
            status_code=403,
            detail="Deployment-global registry catalogs are only supported for the default account",
        )


__all__ = [
    "assert_create_account_scope",
    "assert_row_in_account_scope",
    "request_scope",
    "require_default_registry_account",
    "row_in_account_scope",
]
