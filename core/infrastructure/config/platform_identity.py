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

__all__ = ["DEFAULT_ACCOUNT_ID", "DEFAULT_TENANT_ID", "DEFAULT_WORKSPACE_ID"]
