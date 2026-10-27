"""Pure wildcard matching for canonical application token scopes."""

from __future__ import annotations


def scope_granted(principal_scopes: set[str], required_scope: str) -> bool:
    required = str(required_scope or "").strip().lower()
    if not required:
        return True
    if required in principal_scopes or "admin:all" in principal_scopes:
        return True
    separator = ":" if ":" in required else ""
    if not separator and "." in required:
        separator = "."
    if not separator:
        return required in principal_scopes
    domain, _, action = required.partition(separator)
    return (
        f"{domain}:*" in principal_scopes
        or f"*:{action}" in principal_scopes
        or "*:*" in principal_scopes
    )


__all__ = ["scope_granted"]
