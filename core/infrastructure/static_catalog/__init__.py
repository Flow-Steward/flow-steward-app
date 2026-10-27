"""Configuration and bounds for the read-only static catalogs.

This package is deliberately narrow: it owns the canonical configuration
projection and the numeric limits for the Git-hosted static catalogs
(workflow templates, extensions and MCP servers), plus the one rule set every
catalog icon follows (``icons``) — no catalog item model, no catalog domain, no
source-adapter hierarchy.

The bounded outbound fetch that applies these limits is ``http_fetch`` in this
same package, and ``index_cache`` is the one revalidating index cache the three
catalogs share. The fetch reaches the SSRF guard through an injected
``CatalogUrlGuard`` rather than importing it, because the repository's
canonical guard currently lives in the application layer and infrastructure
must not depend on it.
"""

from __future__ import annotations

from core.infrastructure.static_catalog.settings import (
    CATALOG_HTTP_TIMEOUT_SECONDS,
    CATALOG_MAX_REDIRECTS,
    CATALOG_REFRESH_RETRY_COOLDOWN_SECONDS,
    MAX_CATALOG_ICON_BYTES,
    MAX_CATALOG_ICON_PX,
    MAX_CATALOG_INDEX_BYTES,
    MAX_CATALOG_ITEMS,
    MAX_EXTENSION_RELEASE_ZIP_BYTES,
    MAX_WORKFLOW_TEMPLATE_YAML_BYTES,
    MIN_CATALOG_ICON_PX,
    extension_catalog_url,
    mcp_server_catalog_url,
    workflow_template_catalog_url,
)

__all__ = [
    "CATALOG_HTTP_TIMEOUT_SECONDS",
    "CATALOG_MAX_REDIRECTS",
    "CATALOG_REFRESH_RETRY_COOLDOWN_SECONDS",
    "MAX_CATALOG_ICON_BYTES",
    "MAX_CATALOG_ICON_PX",
    "MAX_CATALOG_INDEX_BYTES",
    "MAX_CATALOG_ITEMS",
    "MAX_EXTENSION_RELEASE_ZIP_BYTES",
    "MAX_WORKFLOW_TEMPLATE_YAML_BYTES",
    "MIN_CATALOG_ICON_PX",
    "extension_catalog_url",
    "mcp_server_catalog_url",
    "workflow_template_catalog_url",
]
