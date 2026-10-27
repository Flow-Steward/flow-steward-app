"""
AgentRegistry — manages agent manifests loaded from PostgreSQL.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from core.application.registry.agent_manifest import AgentManifest

if TYPE_CHECKING:
    from core.application.registry.db_store import RegistryDBStore

logger = logging.getLogger(__name__)


class AgentRegistry:
    def __init__(self, db_store: RegistryDBStore):
        self._db_store = db_store
        self._agents: dict[str, AgentManifest] = {}
        self._load_from_db()

    def _load_from_db(self) -> None:
        self._agents.clear()
        try:
            rows = self._db_store.list_agents()
            for row in rows:
                try:
                    manifest = AgentManifest.from_db_row(row)
                    self._agents[manifest.agent_id] = manifest
                except Exception as e:
                    logger.exception(
                        "Failed to load agent from DB row %s: %s", row.get("agent_id"), e
                    )
            from core.application.registry.projected_artifact_rows import projected_artifact_rows

            for row in projected_artifact_rows(self._db_store, "agent", "agent_id"):
                try:
                    manifest = AgentManifest.from_db_row(row)
                    self._agents[manifest.agent_id] = manifest
                except Exception as e:
                    logger.exception(
                        "Failed to load projected agent %s: %s", row.get("agent_id"), e
                    )
            logger.debug("Loaded %d agents from DB", len(self._agents))
        except Exception as e:
            logger.exception("Failed to load agents from DB: %s", e)

    def get_agent(self, agent_id: str) -> AgentManifest | None:
        return self._agents.get(agent_id)

    def get(self, agent_id: str) -> AgentManifest | None:
        """Convenience alias for dict-like access."""
        return self.get_agent(agent_id)

    def get_agent_or_raise(self, agent_id: str) -> AgentManifest:
        agent = self._agents.get(agent_id)
        if not agent:
            raise KeyError(f"Agent '{agent_id}' not found in registry")
        return agent

    def find_agents_for_skill(self, skill_id: str) -> list[AgentManifest]:
        return [a for a in self._agents.values() if skill_id in a.skills]

    def get_dependency_order(self, agent_ids: list[str]) -> list[str]:
        from collections import deque

        in_degree = dict.fromkeys(agent_ids, 0)
        adj: dict[str, list[str]] = {aid: [] for aid in agent_ids}
        id_set = set(agent_ids)

        for aid in agent_ids:
            agent = self._agents.get(aid)
            if not agent:
                continue
            for dep in agent.depends_on.get("hard", []):
                if dep in id_set:
                    adj[dep].append(aid)
                    in_degree[aid] += 1

        queue = deque([aid for aid in agent_ids if in_degree[aid] == 0])
        result = []
        while queue:
            node = queue.popleft()
            result.append(node)
            for neighbor in adj.get(node, []):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(result) != len(agent_ids):
            logger.warning("Circular dependency detected; using original order")
            return agent_ids
        return result

    def list_all(self) -> list[AgentManifest]:
        return list(self._agents.values())

    def list_ids(self) -> list[str]:
        return list(self._agents.keys())

    def reload(self) -> None:
        logger.info("Reloading agent registry from DB")
        self._load_from_db()
        logger.info("Agent registry reloaded: %d agents", len(self._agents))
