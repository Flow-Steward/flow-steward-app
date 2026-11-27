"""Canonical built-in platform identity defaults.

These values are deployment configuration shared by application policy and
infrastructure persistence adapters. Keeping them in infrastructure config
prevents database adapters from importing the application auth package.
"""

from __future__ import annotations

from core.infrastructure.config.runtime_env import runtime_env as _runtime_env

DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_ACCOUNT_ID = (
    str(
        _runtime_env.get("FS_DEFAULT_ACCOUNT_ID", "00000000-0000-0000-0000-000000000002") or ""
    ).strip()
    or "00000000-0000-0000-0000-000000000002"
)
DEFAULT_WORKSPACE_ID = "00000000-0000-0000-0000-000000000003"


def is_installation_account(account_id: object) -> bool:
    """Whether ``account_id`` is the installation account: the one that owns the
    installation-wide resources, such as installed extension code (ADR 0020, C6).

    Infrastructure's form of ``core.application.auth.installation_admin``'s rule. It
    only answers the question; it never supplies an account to a caller without one.
    Empty is never the installation account.
    """
    text = str(account_id or "").strip()
    return bool(text) and text == DEFAULT_ACCOUNT_ID


__all__ = [
    "DEFAULT_ACCOUNT_ID",
    "DEFAULT_TENANT_ID",
    "DEFAULT_WORKSPACE_ID",
    "is_installation_account",
]
