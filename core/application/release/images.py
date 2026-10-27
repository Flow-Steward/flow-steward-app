"""Canonical Flow Steward product-image roles for official releases."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProductImageRole:
    """One published first-party image and its production environment binding."""

    role: str
    repository: str
    environment_variable: str


_ROLE_BY_NAME = {
    role.role: role
    for role in (
        ProductImageRole("compact-runtime", "flow-steward-compact", "FS_COMPACT_IMAGE"),
        ProductImageRole("fs-app", "flow-steward-app", "FS_APP_IMAGE"),
        ProductImageRole("fs-worker", "flow-steward-worker", "FS_WORKER_IMAGE"),
        ProductImageRole("control-plane", "flow-steward-control", "FS_CONTROL_IMAGE"),
        ProductImageRole("fs-cli", "flow-steward-cli", "FS_CLI_IMAGE"),
        ProductImageRole("fs-mcp", "flow-steward-mcp", "MCP_IMAGE"),
    )
}

_PROFILE_ROLE_NAMES = {
    "compact": ("compact-runtime", "fs-cli", "fs-mcp"),
    "scalable": ("fs-app", "fs-worker", "control-plane", "fs-cli", "fs-mcp"),
}


def required_product_roles(profile: str) -> tuple[ProductImageRole, ...]:
    """Return the complete ordered official role set for a deployment profile."""

    normalized = str(profile or "").strip().lower()
    try:
        names = _PROFILE_ROLE_NAMES[normalized]
    except KeyError as exc:
        raise ValueError("unsupported release deployment profile") from exc
    return tuple(_ROLE_BY_NAME[name] for name in names)


def all_product_roles() -> tuple[ProductImageRole, ...]:
    """Return every independently published first-party image role once."""

    return tuple(_ROLE_BY_NAME.values())


__all__ = ["ProductImageRole", "all_product_roles", "required_product_roles"]
