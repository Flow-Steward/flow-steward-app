"""Search the published catalogs used by the product UI."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Literal

from core.application.extensions.extension_catalog import (
    ExtensionCatalogError,
    extension_catalog_availability,
    load_extension_catalog,
    search_extension_catalog,
)
from core.application.extensions.extension_catalog_install import catalog_release_compatible
from core.application.management.mcp_server_catalog import (
    McpServerCatalogError,
    load_mcp_server_catalog,
    mcp_server_catalog_availability,
)
from core.application.workflows.workflow_template_catalog import (
    WorkflowTemplateCatalogError,
    load_workflow_template_catalog,
    workflow_template_catalog_availability,
)
from core.infrastructure.config.version import get_app_version

CatalogKind = Literal["extension", "mcp_server", "workflow_template"]
CatalogSelection = Literal["all", "extension", "mcp_server", "workflow_template"]
_KINDS: tuple[CatalogKind, ...] = ("extension", "mcp_server", "workflow_template")


def _matches(query: str, fields: Iterable[str]) -> bool:
    text = " ".join(fields).casefold()
    return all(term in text for term in query.casefold().split())


def _catalog_status(availability: dict[str, Any]) -> dict[str, Any]:
    return {
        key: availability[key]
        for key in ("available", "stale", "reason", "catalog_revision", "skipped_count")
        if key in availability and availability[key] is not None
    }


def _extensions(query: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    index = load_extension_catalog()
    version = get_app_version()
    entries = [
        {
            "kind": "extension",
            "id": item.extension_id,
            "name": item.name,
            "version": item.version,
            "summary": item.summary[:600],
            "capabilities": list(item.capabilities),
            "setup_summary": item.setup_summary[:600],
            "compatible": catalog_release_compatible(item, platform_version=version),
            "release_sha256": item.release_zip_sha256,
            "publication_status": item.publication_status,
            "verification_status": item.verification_status,
        }
        for item in search_extension_catalog(index, query)
    ]
    return entries, _catalog_status(extension_catalog_availability(index))


def _mcp_servers(query: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    index = load_mcp_server_catalog()
    entries = [
        {
            "kind": "mcp_server",
            "id": item.item_id,
            "name": item.title,
            "version": item.server_version,
            "summary": item.summary[:600],
            "auth_modes": sorted({remote.auth_mode for remote in item.remotes}),
            "operations_status": "requires_connection",
            "publication_status": item.status,
        }
        for item in index.items
        if _matches(
            query,
            (item.item_id, item.title, item.summary, item.description, *item.keywords),
        )
    ]
    return entries, _catalog_status(mcp_server_catalog_availability(index))


def _workflow_templates(query: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    index = load_workflow_template_catalog()
    entries = [
        {
            "kind": "workflow_template",
            "id": item.item_id,
            "name": item.name,
            "version": item.version,
            "summary": item.summary[:600],
            "integrations": list(item.integrations),
            "setup_requirements": [
                requirement.as_dict() for requirement in item.setup_requirements
            ],
            "publication_status": item.publication_status,
            "verification_status": item.verification_status,
            "importable": item.importable,
        }
        for item in index.items
        if _matches(
            query,
            (
                item.item_id,
                item.name,
                item.summary,
                item.description,
                *item.integrations,
                *item.tags,
            ),
        )
    ]
    return entries, _catalog_status(workflow_template_catalog_availability(index))


_SEARCHERS = {
    "extension": _extensions,
    "mcp_server": _mcp_servers,
    "workflow_template": _workflow_templates,
}


def _rank(item: dict[str, Any], query: str) -> tuple[bool, bool, bool, str, str, str]:
    name = item["name"].casefold()
    identity = item["id"].casefold()
    return (
        item["publication_status"] == "deprecated",
        query not in (identity, name),
        query not in name,
        name,
        item["kind"],
        identity,
    )


def search_catalogs(
    query: str, *, kind: CatalogSelection = "all", limit: int = 12
) -> dict[str, Any]:
    """Return published matches and report coverage independently for each source."""
    catalogs: dict[str, dict[str, Any]] = {}
    entries: list[dict[str, Any]] = []
    selected: Sequence[CatalogKind] = _KINDS if kind == "all" else (kind,)
    for catalog in selected:
        try:
            matches, catalogs[catalog] = _SEARCHERS[catalog](query)
        except (ExtensionCatalogError, McpServerCatalogError, WorkflowTemplateCatalogError) as exc:
            catalogs[catalog] = {"available": False, "reason": exc.code}
        else:
            entries.extend(matches)
    normalized = " ".join(query.casefold().split())
    entries.sort(key=lambda item: _rank(item, normalized))
    return {"items": entries[:limit], "match_count": len(entries), "catalogs": catalogs}
