"""Canonical runtime environment projection for an exact official release."""

from __future__ import annotations

from core.application.release.contracts import ReleaseIdentity
from core.application.release.images import all_release_roles

_LEGACY_ROLE_NAMES = {
    "fs-app": "app",
    "fs-worker": "worker",
    "control-plane": "control",
    "fs-cli": "cli",
    "fs-mcp": "mcp",
}
_DIGEST_VARIABLES = {
    "app": "FS_APP_IMAGE_DIGEST",
    "worker": "FS_WORKER_IMAGE_DIGEST",
    "control": "FS_CONTROL_IMAGE_DIGEST",
    "mcp": "MCP_IMAGE_DIGEST",
}
_REVISION_VARIABLES = {
    "app": "FS_APP_IMAGE_REVISION",
    "worker": "FS_WORKER_IMAGE_REVISION",
    "control": "FS_CONTROL_IMAGE_REVISION",
    "cli": "FS_CLI_IMAGE_REVISION",
    "mcp": "MCP_IMAGE_REVISION",
}


def release_runtime_environment(release: ReleaseIdentity) -> dict[str, str]:
    """Project a verified release aggregate without re-deriving its identity."""
    role_variables = {role.role: role.environment_variable for role in all_release_roles()}
    values = {"FS_BUILD_SHA": release.commit_sha}
    for image in release.images:
        values[role_variables[image.role]] = image.registry_ref
        if image.role == "postgres":
            # POSTGRES_IMAGE alone: a dependency image has no runtime revision or
            # digest variables. A release without this role leaves POSTGRES_IMAGE
            # to the installation, which builds it locally as a source install does.
            continue
        if image.role == "compact-runtime":
            values.update(
                {
                    "FS_COMPACT_IMAGE_REGISTRY_REF": image.registry_ref,
                    "FS_COMPACT_IMAGE_INDEX_DIGEST": image.index_digest,
                    "FS_COMPACT_IMAGE_MANIFEST_DIGEST": image.manifest_digest,
                    "FS_COMPACT_IMAGE_CONFIG_DIGEST": image.config_digest,
                    "FS_COMPACT_IMAGE_REVISION": image.revision,
                    "FS_COMPACT_IMAGE_ROLE": image.role,
                    "FS_COMPACT_IMAGE_PLATFORM": image.platform,
                }
            )
            continue
        legacy_role = _LEGACY_ROLE_NAMES[image.role]
        digest_variable = _DIGEST_VARIABLES.get(legacy_role)
        if digest_variable is not None:
            values[digest_variable] = image.config_digest
        values[_REVISION_VARIABLES[legacy_role]] = image.revision
    return values


__all__ = ["release_runtime_environment"]
