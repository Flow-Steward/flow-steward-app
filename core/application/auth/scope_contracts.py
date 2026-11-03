"""Pure wildcard matching for canonical application token scopes."""

from __future__ import annotations

#: Scopes only an exact grant satisfies. Choosing a stored secret hands its value to
#: whatever uses it, and replacing one changes what every consumer sends, so neither
#: may arrive implicitly through `admin:all`, `*:*`, `secrets:*`, `*:bind` or
#: `*:write` (ADR 0057). No existing token holds them.
EXACT_ONLY_SCOPES: frozenset[str] = frozenset({"secrets:bind", "secrets:write"})


def scope_granted(principal_scopes: set[str], required_scope: str) -> bool:
    required = str(required_scope or "").strip().lower()
    if not required:
        return True
    if required in EXACT_ONLY_SCOPES:
        return required in principal_scopes
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


__all__ = ["EXACT_ONLY_SCOPES", "scope_granted"]
