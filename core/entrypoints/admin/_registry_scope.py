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
from core.application.management.command_services_agent_rules import (
    create_account_scope_allowed,
)
from core.application.management.command_services_agent_rules import (
    row_in_account_scope as _row_in_account_scope,
)
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

    Fails closed on anything that is not a mapping. The rule itself is owned by
    the application layer so services and routes cannot drift apart.
    """
    return _row_in_account_scope(row, scope)


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
    if not create_account_scope_allowed(payload, scope):
        raise HTTPException(status_code=403, detail="Access denied")


def registry_catalog_account(scope: ScopeContext | None) -> str:
    """The account whose registry fields and data contracts the caller works on.

    Fields and data contracts carry an owning account (ADR 0020, C4a), so every
    caller reaches only its own account's catalog. A scope without an account is
    refused (C5); it never falls back to the installation account.
    """
    account_id = str(getattr(scope, "account_id", "") or "").strip()
    if not account_id:
        raise HTTPException(status_code=403, detail="Access denied")
    return account_id


__all__ = [
    "assert_create_account_scope",
    "assert_row_in_account_scope",
    "registry_catalog_account",
    "request_scope",
    "row_in_account_scope",
]
