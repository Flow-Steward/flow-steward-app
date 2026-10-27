"""Canonical configuration projection and bounds for the static catalogs.

Application code never reads the environment for these values: the catalog
URLs are declared in the env manifest, resolved once into ``CONFIG`` by
``runtime_config``, and exposed here as plain accessors.
"""

from __future__ import annotations

from core.infrastructure.config.runtime_config import CONFIG

#: There is no freshness window: every catalog read revalidates the index with
#: its host (``index_cache``). After a failed read the last good index keeps
#: serving, marked stale, and reads within this many seconds do not ask again,
#: so a dead catalog host is dialled once per cooldown rather than once per
#: request. It is short on purpose: it only ever delays recovery, never a
#: successful read.
CATALOG_REFRESH_RETRY_COOLDOWN_SECONDS = 10

#: Outbound bounds applied to every catalog request.
CATALOG_HTTP_TIMEOUT_SECONDS = 15.0
CATALOG_MAX_REDIRECTS = 3

#: An index carries metadata only, so a few MB is already generous for the
#: 1,000-plus entries the catalog is sized for.
MAX_CATALOG_INDEX_BYTES = 8 * 1024 * 1024
MAX_CATALOG_ITEMS = 5000

#: One workflow bundle. Larger than any template we publish, small enough that a
#: hostile host cannot stream an unbounded body into the process.
MAX_WORKFLOW_TEMPLATE_YAML_BYTES = 2 * 1024 * 1024

#: The installer's own archive ceiling, restated here rather than imported so
#: infrastructure keeps pointing at configuration instead of at application
#: code. ``tests/unit/test_static_catalog_http.py`` pins it to
#: ``binding_constants.MAX_EXTENSION_ARCHIVE_BYTES`` so the two cannot drift: a
#: catalog limit above the installer's would only fail later, inside the
#: installer.
MAX_EXTENSION_RELEASE_ZIP_BYTES = 50 * 1024 * 1024

#: One catalog icon (an MCP server's, an extension's ``icon.png``): a square PNG
#: of 32-512 px on a side and at most 100 KB. 128x128 is the recommended size.
MAX_CATALOG_ICON_BYTES = 100 * 1024
MIN_CATALOG_ICON_PX = 32
MAX_CATALOG_ICON_PX = 512


def workflow_template_catalog_url() -> str:
    """Absolute HTTPS URL of the workflow template catalog index ('' disables it)."""
    return str(CONFIG.get("workflow_template_catalog_url") or "").strip()


def extension_catalog_url() -> str:
    """Absolute HTTPS URL of the extension catalog index ('' disables it)."""
    return str(CONFIG.get("extension_catalog_url") or "").strip()


def mcp_server_catalog_url() -> str:
    """Absolute HTTPS URL of the MCP server catalog index ('' disables it)."""
    return str(CONFIG.get("mcp_server_catalog_url") or "").strip()


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
