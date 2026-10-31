"""Re-export of the shared public-identity derivation (ADR 0047 decision 3).

The derivation moved to :mod:`core.infrastructure.public_identity` so the
authorization server in ``fs-app`` and this MCP server compute the same issuer and
resource from one function. ``infra/mcp/Dockerfile`` copies that file into the MCP
image.
"""

from __future__ import annotations

from core.infrastructure.public_identity import mcp_auth_urls, public_origin

__all__ = ["mcp_auth_urls", "public_origin"]
