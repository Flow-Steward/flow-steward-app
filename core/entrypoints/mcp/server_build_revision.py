"""Build-provenance guard for the standalone MCP deployment."""

from __future__ import annotations

from core.infrastructure.runtime_env import runtime_env


class McpBuildRevisionMismatch(RuntimeError):
    """The image selected for deployment is not the image that was started."""


def verify_embedded_build_revision() -> None:
    """Reject an MCP image whose baked revision differs from its deployment.

    ``FS_MCP_IMAGE_REVISION`` is written by ``infra/mcp/Dockerfile`` at build
    time. Production Compose supplies ``FS_DEPLOYMENT_REVISION`` from the
    release environment. Local development leaves that value empty and bypasses
    this comparison intentionally.
    """
    deployment_revision = str(runtime_env.get("FS_DEPLOYMENT_REVISION") or "").strip()
    if not deployment_revision:
        return

    image_revision = str(runtime_env.get("FS_MCP_IMAGE_REVISION") or "").strip()
    if not image_revision:
        raise McpBuildRevisionMismatch(
            "MCP deployment revision is declared but the image has no embedded revision"
        )
    if image_revision != deployment_revision:
        raise McpBuildRevisionMismatch(
            "MCP image revision does not match the declared deployment revision "
            f"(image={image_revision!r}, deployment={deployment_revision!r})"
        )
