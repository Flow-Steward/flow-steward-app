"""
ToolRegistry — manages tool manifests loaded from PostgreSQL.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from core.application.registry.tool_manifest import ToolManifest

if TYPE_CHECKING:
    from core.application.registry.db_store import RegistryDBStore

logger = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self, db_store: RegistryDBStore):
        self._db_store = db_store
        self._tools: dict[str, ToolManifest] = {}
        self._load_from_db()

    def _load_from_db(self) -> None:
        self._tools.clear()
        try:
            rows = self._db_store.list_tools()
            for row in rows:
                try:
                    manifest = ToolManifest.from_db_row(row)
                    self._tools[manifest.tool_id] = manifest
                except Exception as e:
                    logger.exception(
                        "Failed to load tool from DB row %s: %s", row.get("tool_id"), e
                    )
            from core.application.registry.projected_artifact_rows import projected_artifact_rows

            for row in projected_artifact_rows(self._db_store, "tool", "tool_id"):
                try:
                    manifest = ToolManifest.from_db_row(row)
                    self._tools[manifest.tool_id] = manifest
                except Exception as e:
                    logger.exception("Failed to load projected tool %s: %s", row.get("tool_id"), e)
            logger.debug("Loaded %d tools from DB", len(self._tools))
        except Exception as e:
            logger.exception("Failed to load tools from DB: %s", e)

    def get_tool(self, tool_id: str) -> ToolManifest | None:
        return self._tools.get(tool_id)

    def get_enabled_tool(self, tool_id: str) -> ToolManifest | None:
        tool = self._tools.get(tool_id)
        return tool if (tool and tool.enabled) else None

    def list_all(self) -> list[ToolManifest]:
        return list(self._tools.values())

    def list_enabled(self) -> list[ToolManifest]:
        return [t for t in self._tools.values() if t.enabled]

    def list_by_category(self, category: str) -> list[ToolManifest]:
        return [t for t in self._tools.values() if t.category == category]

    def get_catalog_entry(self, tool_id: str) -> dict | None:
        manifest = self._tools.get(tool_id)
        return manifest.catalog_entry() if manifest else None

    def get_enabled_catalog_entry(self, tool_id: str) -> dict | None:
        manifest = self.get_enabled_tool(tool_id)
        return manifest.catalog_entry() if manifest else None

    def list_catalog(self, *, enabled_only: bool = False) -> list[dict]:
        manifests = self.list_enabled() if enabled_only else self.list_all()
        return [manifest.catalog_entry() for manifest in manifests]

    def get_effective_catalog(self, agent_id: str) -> list[dict]:
        if hasattr(self._db_store, "get_effective_tool_catalog"):
            return list(self._db_store.get_effective_tool_catalog(agent_id) or [])

        rows = list(
            getattr(self._db_store, "get_effective_tools", lambda _agent_id: [])(agent_id) or []
        )
        catalog: list[dict] = []
        for row in rows:
            tool_id = str((row or {}).get("tool_id") or "").strip()
            manifest = self._tools.get(tool_id)
            if not manifest:
                continue
            source = str((row or {}).get("source") or "").strip() or None
            catalog.append(manifest.catalog_entry(source=source))
        return catalog

    def find_tools_used_by_agents(self) -> dict[str, list[str]]:
        return {}

    def build_resolver(self):
        from core.application.orchestration.tools.tool_resolver import ToolResolver

        resolver = ToolResolver()
        for manifest in self._tools.values():
            resolver.register_from_manifest(manifest)
        return resolver

    def reload(self) -> None:
        logger.info("Reloading tool registry from DB")
        self._load_from_db()
        logger.info("Tool registry reloaded: %d tools", len(self._tools))
